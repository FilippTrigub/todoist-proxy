"""Pure Todoist route matching helpers.

The proxy and due poller own I/O. This module only decides which configured
subscriptions should receive an already-parsed Todoist event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


SUBSCRIPTION_AGENT_MAP: dict[str, str] = {
    "max-lowkeycodes": "max",
    "abra-lowkeycodes": "abra",
    "smith-lowkeycodes": "smith",
    "hausmeister-inbox": "hausmeister",
}

AGENT_UID_MAP: dict[str, str] = {
    "max": "59328091",
    "abra": "15795569",
    "smith": "29584133",
    "hausmeister": "59138424",
}

LIFECYCLE_TASK_EVENTS: frozenset[str] = frozenset(
    {"item:updated", "item:completed", "item:uncompleted"}
)

_WORD_CHAR = r"A-Za-z0-9_"


@dataclass(frozen=True)
class MatchedRoute:
    subscription: str
    agent: str
    reason: str
    legacy: bool


def opaque_id(value: Any) -> str:
    """Normalize Todoist IDs as opaque strings.

    ``None`` and an empty string mean absent. Everything else, including
    integer-like values and the string ``"0"``, is preserved as a string.
    """

    if value is None:
        return ""
    text = str(value)
    return text if text != "" else ""


def _first_opaque_id(data: Mapping[str, Any], *names: str) -> tuple[str, str]:
    for name in names:
        value = opaque_id(data.get(name))
        if value:
            return value, name
    return "", ""


def event_project_id(event_data: Mapping[str, Any]) -> str:
    return _first_opaque_id(event_data, "project_id")[0]


def task_id(event_data: Mapping[str, Any]) -> str:
    return _first_opaque_id(event_data, "id", "task_id")[0]


def note_parent_id(event_data: Mapping[str, Any]) -> str:
    return _first_opaque_id(event_data, "item_id")[0]


def note_author_id(event_data: Mapping[str, Any]) -> str:
    return _first_opaque_id(event_data, "posted_uid")[0]


def _responsible_id(event_data: Mapping[str, Any]) -> tuple[str, str]:
    return _first_opaque_id(event_data, "responsible_uid", "assignee_id")


def _creator_id(event_data: Mapping[str, Any]) -> tuple[str, str]:
    return _first_opaque_id(event_data, "added_by_uid", "creator_uid", "creator_id")


def _section_id(event_data: Mapping[str, Any]) -> tuple[str, str]:
    return _first_opaque_id(event_data, "section_id")


def _string_set(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {text for value in values if (text := opaque_id(value))}


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return [text for value in values if (text := opaque_id(value))]


def mention_alias_in_text(text: Any, aliases: Any) -> str:
    """Return the first configured alias found as a standalone mention.

    Boundary checks prevent substring matches such as ``Max`` inside
    ``Maximum`` while still allowing punctuation-wrapped mentions.
    """

    if not isinstance(text, str) or not text:
        return ""
    for alias in _string_list(aliases):
        pattern = rf"(?<![{_WORD_CHAR}]){re.escape(alias)}(?![{_WORD_CHAR}])"
        if re.search(pattern, text):
            return alias
    return ""


def match_routes(
    routes: Mapping[str, Any],
    event_name: str,
    event_data: Mapping[str, Any],
) -> list[MatchedRoute]:
    """Return matching subscriptions for a Todoist event.

    Supports legacy ``project_id -> [subscription, ...]`` routes as
    project-wide broadcasts and conditional ``project_id -> {subscription:
    rule}`` routes as fail-closed per-target match rules.
    """

    project_id = event_project_id(event_data)
    if not project_id:
        return []
    project_routes = routes.get(project_id)
    return match_project_routes(project_routes, event_name, event_data)


def match_project_routes(
    project_routes: Any,
    event_name: str,
    event_data: Mapping[str, Any],
) -> list[MatchedRoute]:
    if isinstance(project_routes, list):
        return [
            MatchedRoute(
                subscription=subscription,
                agent=SUBSCRIPTION_AGENT_MAP.get(subscription, ""),
                reason="legacy_match_all",
                legacy=True,
            )
            for subscription in project_routes
            if isinstance(subscription, str) and subscription
        ]

    if not isinstance(project_routes, Mapping):
        return []

    if event_name == "note:added":
        mention_matches: list[MatchedRoute] = []
        parent_matches: list[MatchedRoute] = []
        for subscription, rule in project_routes.items():
            if not isinstance(subscription, str) or not subscription:
                continue
            mention_match = match_note_mention_route(subscription, rule, event_data)
            if mention_match is not None:
                mention_matches.append(mention_match)
                continue
            parent_match = match_task_relevance_route(
                subscription,
                rule,
                event_name,
                event_data,
                allow_creator=True,
            )
            if parent_match is not None:
                parent_matches.append(parent_match)
        if mention_matches:
            return mention_matches
        return parent_matches

    matches: list[MatchedRoute] = []
    for subscription, rule in project_routes.items():
        if not isinstance(subscription, str) or not subscription:
            continue
        match = match_task_relevance_route(
            subscription,
            rule,
            event_name,
            event_data,
            allow_creator=event_name in LIFECYCLE_TASK_EVENTS,
        )
        if match is not None:
            matches.append(match)
    return matches


def match_note_mention_route(
    subscription: str,
    rule: Any,
    event_data: Mapping[str, Any],
) -> MatchedRoute | None:
    if not isinstance(rule, Mapping):
        return None

    alias = mention_alias_in_text(event_data.get("content"), rule.get("mention_aliases"))
    if not alias:
        return None
    agent = opaque_id(rule.get("agent")) or SUBSCRIPTION_AGENT_MAP.get(subscription, "")
    return MatchedRoute(subscription, agent, f"mention_alias={alias}", False)


def match_task_relevance_route(
    subscription: str,
    rule: Any,
    event_name: str,
    event_data: Mapping[str, Any],
    *,
    allow_creator: bool,
) -> MatchedRoute | None:
    if not isinstance(rule, Mapping):
        return None

    agent = opaque_id(rule.get("agent")) or SUBSCRIPTION_AGENT_MAP.get(subscription, "")

    responsible, responsible_field = _responsible_id(event_data)
    responsible_uids = _string_set(rule.get("responsible_uids"))
    if responsible and responsible in responsible_uids:
        return MatchedRoute(
            subscription,
            agent,
            f"{responsible_field}={responsible}",
            False,
        )

    section, section_field = _section_id(event_data)
    section_ids = _string_set(rule.get("section_ids"))
    if not responsible and section and section in section_ids:
        return MatchedRoute(subscription, agent, f"{section_field}={section}", False)

    if allow_creator:
        creator, creator_field = _creator_id(event_data)
        creator_uids = _string_set(rule.get("creator_uids"))
        if creator and creator in creator_uids:
            return MatchedRoute(subscription, agent, f"{creator_field}={creator}", False)

    return None


def match_conditional_route(
    subscription: str,
    rule: Any,
    event_name: str,
    event_data: Mapping[str, Any],
) -> MatchedRoute | None:
    """Evaluate a single conditional route rule.

    Malformed rules fail closed by returning ``None``.
    """

    if event_name == "note:added":
        return match_note_mention_route(subscription, rule, event_data) or match_task_relevance_route(
            subscription,
            rule,
            event_name,
            event_data,
            allow_creator=True,
        )

    return match_task_relevance_route(
        subscription,
        rule,
        event_name,
        event_data,
        allow_creator=event_name in LIFECYCLE_TASK_EVENTS,
    )
