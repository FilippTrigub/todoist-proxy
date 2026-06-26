#!/usr/bin/env python3
"""
Todoist → Hermes webhook router proxy.

Flow:
  1. Validate X-Todoist-Hmac-SHA256 (base64 HMAC-SHA256 using the OAuth
     app's client_secret). Reject with 401 on mismatch or missing header.
  2. For item:added with a due date in the future, drop it here (200, no
     forward) — due_poller.py will deliver an equivalent event once the
     task is actually due. Without this, every agent reacts to a task the
     moment it's created, regardless of when it's actually meant to start.
  3. Extract event_data.project_id from the payload.
  4. Load ~/.hermes/todoist-routing.json and look up which Hermes
     subscriptions handle that project.
  5. Forward the request to each matching subscription in parallel.
  6. Return 200 to Todoist if validation passed (prevents spurious retries).
     Return 502 only if every target failed with a server error.

Routing config (~/.hermes/todoist-routing.json) maps project IDs to lists
of Hermes subscription names. It is re-read on every request so adding or
changing routes takes effect immediately without a proxy restart:

  {
    "6ggFh66x4WXVVqGH": ["hausmeister-inbox"],
    "6gmpjVFv2wVG7XJQ": ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
  }

The Todoist webhook URL path does not affect routing — only the project_id
in the payload does. A single Todoist webhook registration is sufficient.

Also serves GET /oauth/callback for the one-time OAuth authorization that
activates webhook delivery for an account.

Required env var : TODOIST_CLIENT_SECRET
Optional env vars: TODOIST_CLIENT_ID     (needed for /oauth/callback)
                   TODOIST_ROUTING_FILE  (default: ~/.hermes/todoist-routing.json)
                   PROXY_PORT            (default: 8645)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from aiohttp import ClientSession, web

from control_ledger import ControlLedger, LedgerResult, evaluate_forwarding
from due_utils import due_status
from interaction_extractor import extract_interactions

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8645"))
MAX_BODY = 1 * 1024 * 1024  # 1 MB
TODOIST_SIG_HEADER = "X-Todoist-Hmac-SHA256"
OAUTH_TOKEN_URL = "https://todoist.com/oauth/access_token"
TODOIST_TASKS_URL = "https://api.todoist.com/api/v1/tasks"
PUBLIC_BASE = "https://the-data-server.tailf73bbe.ts.net"
ROUTING_FILE = Path(
    os.environ.get(
        "TODOIST_ROUTING_FILE",
        Path.home() / ".hermes" / "todoist-routing.json",
    )
)
DISABLE_FILE = Path(
    os.environ.get(
        "TODOIST_DISABLE_FILE",
        Path.home() / ".hermes" / "todoist-proxy.disabled",
    )
)

SUBSCRIPTION_AGENT_MAP = {
    "max-lowkeycodes": "max",
    "abra-lowkeycodes": "abra",
    "smith-lowkeycodes": "smith",
    "hausmeister-inbox": "hausmeister",
}


async def _resolve_project_id(session: ClientSession, item_id: str, api_key: str) -> str:
    """Look up a task by item_id to get its project_id (needed for note:* events)."""
    try:
        async with session.get(
            f"{TODOIST_TASKS_URL}/{item_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as resp:
            if resp.status == 200:
                return (await resp.json()).get("project_id", "")
            log.warning("task lookup %s returned %d", item_id, resp.status)
            return ""
    except Exception as exc:
        log.warning("task lookup %s error: %s", item_id, exc)
        return ""


def _load_secret() -> bytes:
    secret = os.environ.get("TODOIST_CLIENT_SECRET", "")
    if not secret:
        log.error("TODOIST_CLIENT_SECRET is not set — refusing to start")
        sys.exit(1)
    return secret.encode()


def _verify(secret: bytes, body: bytes, header_value: str) -> bool:
    expected = base64.b64encode(
        hmac.new(secret, body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, header_value)


def _load_routing() -> tuple[dict[str, list[str]], dict[str, str]]:
    """
    Load routing config. Returns (routes, upstreams) where:
      routes:    project_id → [subscription_name, ...]
      upstreams: subscription_name → upstream base URL
    Falls back to empty dicts on any error.
    """
    try:
        cfg = json.loads(ROUTING_FILE.read_text())
        routes: dict[str, list[str]] = cfg.get("routes", {})
        upstreams: dict[str, str] = cfg.get("upstreams", {})
        return routes, upstreams
    except FileNotFoundError:
        log.warning("routing file not found: %s", ROUTING_FILE)
        return {}, {}
    except Exception as exc:
        log.warning("failed to load routing file: %s", exc)
        return {}, {}


async def _forward(
    session: ClientSession,
    subscription: str,
    upstream: str,
    body: bytes,
    headers: dict[str, str],
    req_id: str,
) -> int:
    url = f"{upstream}/webhooks/{subscription}"
    try:
        async with session.post(
            url,
            data=body,
            headers=headers,
            allow_redirects=False,
        ) as resp:
            log.info("[%s] → %s %d", req_id, url, resp.status)
            return resp.status
    except TimeoutError:
        log.error("[%s] → %s timeout", req_id, url)
        return 504
    except Exception as exc:
        log.error("[%s] → %s error: %s", req_id, url, exc)
        return 502


def _agent_for_subscription(subscription: str) -> str:
    """Map known subscription names to control-ledger agent scopes."""

    return SUBSCRIPTION_AGENT_MAP.get(subscription, "")


def _todoist_task_id(event_data: dict) -> str:
    """Return the stable task/comment parent identifier used in ledger rows."""

    return str(event_data.get("id", "") or event_data.get("item_id", ""))


def _log_ledger_failure(action: str, result: LedgerResult) -> None:
    if not result.success:
        log.warning("ledger %s failed: %s %s", action, result.reason, result.error or "")


def _record_interaction(
    ledger: ControlLedger,
    *,
    interaction_type: str,
    agent: str,
    project_id: str,
    event_data: dict,
    status: str,
    reason: str,
    event_row_id: int | None,
) -> None:
    result = ledger.record_interaction(
        interaction_type=interaction_type,
        agent=agent,
        project_id=project_id,
        todoist_task_id=_todoist_task_id(event_data),
        status=status,
        reason=reason,
        payload=event_data,
        event_row_id=event_row_id,
    )
    _log_ledger_failure("record_interaction", result)


def _record_semantic_interactions(
    ledger: ControlLedger,
    *,
    event_name: str,
    event_data: dict,
    project_id: str,
    event_row_id: int | None,
) -> None:
    for interaction in extract_interactions(event_name, event_data):
        result = ledger.record_interaction(
            interaction_type=interaction.interaction_kind,
            actor=interaction.actor,
            agent=interaction.target.lower(),
            target=interaction.target,
            interaction_kind=interaction.interaction_kind,
            confidence=interaction.confidence,
            project_id=project_id,
            todoist_task_id=interaction.todoist_task_id,
            status="recorded",
            reason=interaction.reason,
            payload=event_data,
            event_row_id=event_row_id,
        )
        _log_ledger_failure("record_semantic_interaction", result)


async def _forward_and_record(
    session: ClientSession,
    ledger: ControlLedger,
    *,
    subscription: str,
    upstream: str,
    agent: str,
    project_id: str,
    event_data: dict,
    body: bytes,
    headers: dict[str, str],
    req_id: str,
    event_row_id: int | None,
) -> int:
    status = await _forward(session, subscription, upstream, body, headers, req_id)
    _record_interaction(
        ledger,
        interaction_type="forward",
        agent=agent,
        project_id=project_id,
        event_data=event_data,
        status=f"http_{status}",
        reason="forward_failed" if status >= 500 else "forwarded",
        event_row_id=event_row_id,
    )
    return status


async def handle(request: web.Request) -> web.Response:
    req_id = str(uuid.uuid4())[:8]

    if request.method != "POST" or not request.path.startswith("/webhooks/"):
        return web.Response(status=404, text="not found")

    try:
        body = await request.read()
    except Exception:
        log.warning("[%s] failed to read body", req_id)
        return web.Response(status=400, text="bad request")

    if len(body) > MAX_BODY:
        log.warning("[%s] body too large (%d bytes)", req_id, len(body))
        return web.Response(status=413, text="payload too large")

    sig = request.headers.get(TODOIST_SIG_HEADER, "")
    if not sig:
        log.warning("[%s] %s missing", req_id, TODOIST_SIG_HEADER)
        return web.Response(status=401, text="missing signature")

    if not _verify(request.app["secret"], body, sig):
        log.warning("[%s] signature mismatch", req_id)
        return web.Response(status=401, text="invalid signature")

    if DISABLE_FILE.exists():
        log.info("[%s] proxy disabled (%s present) — dropping", req_id, DISABLE_FILE)
        return web.Response(status=200, text="proxy disabled")

    try:
        payload = json.loads(body)
        event_name = payload.get("event_name", "")
        event_data = payload.get("event_data", {})
        project_id = event_data.get("project_id", "")
    except (json.JSONDecodeError, AttributeError):
        log.warning("[%s] unparseable payload", req_id)
        return web.Response(status=400, text="invalid JSON")

    ledger = ControlLedger()
    _log_ledger_failure("initialize_schema", ledger.initialize_schema())
    event_result = ledger.record_event(
        event_name=event_name,
        event_data=event_data,
        source="proxy",
    )
    _log_ledger_failure("record_event", event_result)
    event_row_id = event_result.row_id if event_result.success else None

    if event_name == "item:added":
        _record_semantic_interactions(
            ledger,
            event_name=event_name,
            event_data=event_data,
            project_id=project_id,
            event_row_id=event_row_id,
        )

    if event_name == "item:added":
        due = event_data.get("due")
        if due and due.get("date"):
            is_due, _ = due_status(due, datetime.now(), date.today())
            if not is_due:
                _record_interaction(
                    ledger,
                    interaction_type="routing",
                    agent="",
                    project_id=project_id,
                    event_data=event_data,
                    status="deferred",
                    reason="due_in_future",
                    event_row_id=event_row_id,
                )
                log.info(
                    "[%s] item:added task %s due in the future (%s) — not forwarding, "
                    "due_poller will deliver it when it's actually due",
                    req_id, event_data.get("id", "?"), due.get("date"),
                )
                return web.Response(status=200, text="deferred: due in future")

    if not project_id:
        item_id = event_data.get("item_id", "")
        api_key = request.app.get("todoist_api_key", "")
        if item_id and api_key:
            project_id = await _resolve_project_id(request.app["session"], item_id, api_key)
            if project_id:
                log.info("[%s] resolved %s item_id %s → project %s", req_id, event_name, item_id, project_id)

    if event_name != "item:added":
        _record_semantic_interactions(
            ledger,
            event_name=event_name,
            event_data=event_data,
            project_id=project_id,
            event_row_id=event_row_id,
        )

    routes, upstreams = _load_routing()
    subscriptions = routes.get(project_id, [])
    if not subscriptions:
        _record_interaction(
            ledger,
            interaction_type="routing",
            agent="",
            project_id=project_id,
            event_data=event_data,
            status="unrouted",
            reason="no_route",
            event_row_id=event_row_id,
        )
        log.info("[%s] no route for project %s — ignored", req_id, project_id or "(missing)")
        return web.Response(status=200, text="no route")

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "transfer-encoding", "connection"}
    }
    if event_name:
        forward_headers["X-GitHub-Event"] = event_name

    log.info(
        "[%s] project %s → %s",
        req_id, project_id, ", ".join(subscriptions),
    )

    forward_targets: list[tuple[str, str, str]] = []
    for subscription in subscriptions:
        agent = _agent_for_subscription(subscription)
        decision = evaluate_forwarding(
            event_name=event_name,
            project_id=project_id,
            agent=agent,
            source="proxy",
            sentinel_path=DISABLE_FILE,
        )
        _log_ledger_failure(
            "record_routing_decision",
            ledger.record_routing_decision(
                decision=decision,
                target=subscription,
                event_row_id=event_row_id,
            ),
        )
        if decision.enabled:
            forward_targets.append((subscription, upstreams.get(subscription, "http://127.0.0.1:8644"), agent))
        else:
            log.info(
                "[%s] forwarding suppressed for %s (%s)",
                req_id, subscription, decision.reason,
            )
            _record_interaction(
                ledger,
                interaction_type="forward",
                agent=agent,
                project_id=project_id,
                event_data=event_data,
                status="suppressed",
                reason=decision.reason,
                event_row_id=event_row_id,
            )

    if not forward_targets:
        return web.Response(status=200, text="ok")

    session: ClientSession = request.app["session"]
    statuses = await asyncio.gather(
        *[
            _forward_and_record(
                session,
                ledger,
                subscription=subscription,
                upstream=upstream,
                agent=agent,
                project_id=project_id,
                event_data=event_data,
                body=body,
                headers=forward_headers,
                req_id=req_id,
                event_row_id=event_row_id,
            )
            for subscription, upstream, agent in forward_targets
        ]
    )

    # Return 502 only when every target failed — Todoist will retry.
    # If at least one succeeded (or the project is simply unrouted), return 200.
    if all(s >= 500 for s in statuses):
        return web.Response(status=502, text="all upstream targets failed")
    return web.Response(status=200, text="ok")


async def oauth_callback(request: web.Request) -> web.Response:
    """
    One-time OAuth callback. Todoist redirects here after the user approves
    the app. We exchange the code for an access token, which activates
    webhook delivery for that account.

    Register https://the-data-server.tailf73bbe.ts.net/oauth/callback as the
    redirect URI in the Todoist app console, then visit the authorization URL.
    """
    code = request.rel_url.query.get("code", "")
    error = request.rel_url.query.get("error", "")

    if error:
        log.warning("OAuth error from Todoist: %s", error)
        return web.Response(status=400, text=f"OAuth error: {error}")

    if not code:
        return web.Response(status=400, text="Missing code parameter")

    client_id = os.environ.get("TODOIST_CLIENT_ID", "")
    client_secret = os.environ.get("TODOIST_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return web.Response(
            status=500, text="TODOIST_CLIENT_ID or TODOIST_CLIENT_SECRET not set"
        )

    session: ClientSession = request.app["session"]
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": f"{PUBLIC_BASE}/oauth/callback",
            },
        ) as resp:
            resp_body = await resp.json()
            if resp.status == 200 and "access_token" in resp_body:
                token = resp_body["access_token"]
                log.info("OAuth complete — access_token obtained (%.8s…)", token)
                return web.Response(
                    content_type="text/html",
                    text=(
                        "<h2>✓ Todoist OAuth complete</h2>"
                        "<p>Webhook delivery is now active for this account.</p>"
                        f"<p><code>access_token: {token[:8]}…</code></p>"
                    ),
                )
            log.error("Token exchange failed: %s %s", resp.status, resp_body)
            return web.Response(status=502, text=f"Token exchange failed: {resp_body}")
    except Exception as exc:
        log.error("Token exchange error: %s", exc)
        return web.Response(status=502, text=f"Token exchange error: {exc}")


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession()


async def on_shutdown(app: web.Application) -> None:
    await app["session"].close()


if __name__ == "__main__":
    secret = _load_secret()
    api_key = os.environ.get("TODOIST_API_KEY", "")
    if not api_key:
        log.warning("TODOIST_API_KEY not set — note:* events cannot be resolved to a project")
    log.info(
        "todoist-proxy starting on %s:%d (routing: %s)",
        LISTEN_HOST, LISTEN_PORT, ROUTING_FILE,
    )
    app = web.Application()
    app["secret"] = secret
    app["todoist_api_key"] = api_key
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_route("*", "/{path_info:.*}", handle)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None)
