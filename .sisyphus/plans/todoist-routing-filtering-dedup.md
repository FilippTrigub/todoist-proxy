# Todoist Routing Filtering and Per-Target Dedup

## TL;DR
> **Summary**: Replace project-wide fanout with deterministic per-subscription route matching, while preserving legacy broadcast routes and adding per-target delivery dedup so retries do not resend already-successful deliveries.
> **Deliverables**:
> - Shared pure routing matcher for proxy and due poller
> - Conditional route schema with backward-compatible legacy list support
> - Per-target successful-delivery ledger and retry semantics
> - Two-phase `note:added` routing: explicit mention first, parent-task relevance second
> - Updated pytest coverage and README docs
> **Effort**: Medium
> **Parallel**: YES - 4 waves
> **Critical Path**: Task 1 → Tasks 2/3/4 → Tasks 5/6 → Task 7 → Final Verification

## Context

### Original Request

User asked after reading `state.md`:

> "ok, plan the improvements. The explicite goal is to have clear and direct rules for when an event is forwarded to whom + to avoid sending the same even to multiple agents as much as we can + to dedup sucessfully"

### Interview Summary

No further user questions are blocking. The target behavior is explicit:

- Make routing rules deterministic and visible in config/code.
- Stop broadcasting LowKeyCodes events to Max/Abra/Smith when only one agent is relevant.
- Preserve intentional multi-target cases, especially lifecycle follow-up where creator/delegator should see updates.
- Dedup delivery successfully so retries do not resend to already-successful targets and do not lose failed targets.

### Research Summary

- `state.md:5-18` identifies the bug: `routes` maps project IDs to flat subscription lists, so every LowKeyCodes event fans out to all three agents.
- `state.md:20-43` proposes per-subscription route rules and explicitly requires backward compatibility with old flat-list routes.
- `proxy.py:294-299` parses `event_name`, `event_data`, and `project_id` before route fanout.
- `proxy.py:322-342` globally defers future-due `item:added` before forwarding; this must stay before route matching.
- `proxy.py:361-409` currently loads flat routes and applies control gates to every subscription in the project.
- `due_poller.py:255-273` builds synthetic `item:added` events with `project_id`, `section_id`, `responsible_uid`, `creator_uid`, `due`, `_synthetic`, and `_trigger`.
- `due_poller.py:447-511` currently fans each due task to every project subscription and records the due occurrence as fired if any target succeeds.
- `tests/conftest.py:114-129` defines the current route fixture as project → list of subscriptions.
- `tests/test_proxy_webhook.py:155-188` confirms current webhook fanout to all LowKeyCodes subscriptions.
- `tests/test_due_poller_ledger.py:86-140` confirms current due-poller fanout and “any success marks fired” behavior.
- `control_ledger.py:145-157` describes forwarding gate semantics; route relevance must remain separate from runtime enable/disable gates.

### Oracle Review (gaps addressed)

Oracle recommended:

- Create one shared routing/matching module used by both `proxy.py` and `due_poller.py`.
- Treat legacy flat-list routes as explicit `match_all` broadcast.
- Use deterministic rule order: responsible/assignee first, then unassigned section fallback, then creator fallback for lifecycle events.
- Route `note:added` in two phases: explicit mentions first, then parent-task relevance if no mention exists.
- Apply control gates after route matching.
- Dedup per target, not per event globally.

### Metis Review (gaps addressed)

Metis identified hidden decisions and defaults now fixed in this plan:

- Legacy flat routes remain broadcast/match-all forever unless explicitly migrated.
- New conditional route rules fail closed when malformed; they must not accidentally broadcast.
- Todoist IDs are normalized to strings and never coerced through integers.
- Section fallback is allowed only when responsible/assignee is missing, `None`, or `""`; string `"0"` is not empty.
- Proxy partial failure changes from “200 if any target succeeds” to “retry if any matched enabled target fails,” with per-target success dedup preventing duplicate sends to already-successful targets.
- Due-poller must retry failed targets only and must not mark a whole task/due occurrence complete just because one target succeeded.
- Route-filtered targets do not create control-gate rows; only matched targets proceed to `evaluate_forwarding()`.

## Work Objectives

### Core Objective

Move structural routing relevance out of agent prompts and into deterministic proxy/poller code while preserving legacy behavior until conditional routes are configured.

### Deliverables

1. Shared route matching module, likely `route_matcher.py`, with pure functions and no network/file writes.
2. Conditional route schema support in both proxy and due poller:
   ```json
   {
     "routes": {
       "6gmpjVFv2wVG7XJQ": {
         "max-lowkeycodes": {
           "agent": "max",
           "responsible_uids": ["59328091"],
           "section_ids": ["6gpFcCwF29V6QXxx"],
           "creator_uids": ["59328091"],
           "mention_aliases": ["@Max", "Max", "Max | CEO"]
         }
       }
     }
   }
   ```
3. Backward-compatible legacy route support:
   ```json
   {
     "routes": {
       "6gmpjVFv2wVG7XJQ": ["max-lowkeycodes", "abra-lowkeycodes", "smith-lowkeycodes"]
     }
   }
   ```
4. Per-target successful-delivery ledger used by proxy and due poller.
5. Proxy retry semantics for partial failures.
6. Due-poller retry semantics for failed targets only.
7. `note:added` routing with explicit mention priority and parent-task relevance fallback.
8. Tests and README updates documenting route rules and dedup/retry behavior.

### Definition of Done (verifiable conditions with commands)

- `python -m pytest tests/test_route_matcher.py -q` exits `0`, output contains `passed`, output does not contain `FAILED`.
- `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` exits `0`, output contains `passed`, output does not contain `FAILED`.
- `python -m pytest tests/test_due_poller_ledger.py -q` exits `0`, output contains `passed`, output does not contain `FAILED`.
- `python -m pytest -q` exits `0`, output contains `passed`, output does not contain `FAILED`.
- `python -m compileall /home/filipp/Projects/todoist-proxy` exits `0`.
- `git diff --check` exits `0`.

### Must Have

- Legacy flat-list routes still fan out exactly as before.
- Conditional route rules fail closed when malformed.
- Route matching is shared between `proxy.py` and `due_poller.py`.
- New conditional routes minimize fanout by matching only relevant subscriptions.
- Control gates apply only after route relevance chooses candidate targets.
- Per-target dedup is persistent and skips already-successful targets on retry.
- Failed targets remain retryable.
- Future-due deferral remains global and runs before route matching.
- Prompt-level `handled_task_ids` and recurring cooldown remain untouched.

### Must NOT Have

- Do not edit live Hermes prompts/config outside this repo.
- Do not add a route editing UI.
- Do not remove prompt-level dedup/cooldown safeguards.
- Do not use task ID alone as webhook dedup key.
- Do not coerce Todoist IDs to integers.
- Do not section-fallback when `responsible_uid` / assignee is populated.
- Do not broadcast conditional `note:added` routes when there is no explicit mention and parent task context cannot be resolved.
- Do not refactor unrelated HMAC/OAuth/UI behavior.

## Verification Strategy

> ZERO HUMAN INTERVENTION - all verification is agent-executed.

- Test decision: TDD / tests-first for new matcher and dedup semantics, pytest framework.
- QA policy: Every task has agent-executed scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`.
- Required commands:
  - `python -m pytest tests/test_route_matcher.py -q`
  - `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q`
  - `python -m pytest tests/test_due_poller_ledger.py -q`
  - `python -m pytest -q`
  - `python -m compileall /home/filipp/Projects/todoist-proxy`
  - `git diff --check`

## Execution Strategy

### Parallel Execution Waves

> Target: 5-8 tasks per wave. This plan has fewer tasks because shared routing/dedup is a tight critical path; under-splitting is intentional to keep behavior changes atomic and reviewable.

Wave 1: Task 1 foundation matcher and tests.

Wave 2: Tasks 2, 3, and 4 can proceed after Task 1; Task 2 adds delivery ledger, Task 3 integrates proxy task-event routing, Task 4 integrates due-poller routing.

Wave 3: Tasks 5 and 6 can proceed after Tasks 2-4; Task 5 adds note routing, Task 6 updates docs and sample config.

Wave 4: Task 7 full regression/hardening.

### Dependency Matrix (full, all tasks)

| Task | Depends On | Blocks |
|---|---|---|
| 1. Shared route matcher | None | 3, 4, 5, 6 |
| 2. Per-target delivery ledger | None | 3, 4, 7 |
| 3. Proxy task-event integration | 1, 2 | 5, 7 |
| 4. Due-poller integration | 1, 2 | 7 |
| 5. Note/comment two-phase routing | 1, 2, 3 | 7 |
| 6. README/config docs | 1 | 7 |
| 7. Regression and hardening | 3, 4, 5, 6 | Final Verification |

### Agent Dispatch Summary (wave → task count → categories)

- Wave 1 → 1 task → `unspecified-high`
- Wave 2 → 3 tasks → `unspecified-high`, `deep`, `unspecified-high`
- Wave 3 → 2 tasks → `deep`, `writing`
- Wave 4 → 1 task → `unspecified-high`

## TODOs

> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [x] 1. Create shared route matcher and tests

  **What to do**:
  - Add a pure shared module, recommended name `route_matcher.py`.
  - Implement route config normalization for both formats:
    - legacy list route → per-target rule with reason `legacy_match_all`.
    - conditional object route → per-subscription rule object.
  - Implement a result DTO, e.g. frozen dataclass `MatchedRoute(subscription: str, agent: str, reason: str, legacy: bool)`.
  - Move or centralize `SUBSCRIPTION_AGENT_MAP` and `AGENT_UID_MAP` so proxy and due poller do not drift.
  - Normalize Todoist IDs as opaque strings:
    - responsible/assignee: `responsible_uid`, fallback `assignee_id`.
    - creator/added-by: `added_by_uid`, `creator_uid`, `creator_id`.
    - section: `section_id`.
    - project: `project_id`.
    - task id: `id`, fallback `task_id` only for synthetic/local payloads.
    - note parent task: `item_id`.
    - note author: `posted_uid`.
  - Implement task event matching:
    - `item:added` and synthetic due-poller `item:added`: responsible match first; if responsible is empty/missing/`None`/`""`, section fallback; no creator fallback.
    - `item:updated`, `item:completed`, `item:uncompleted`: responsible match; empty-responsible section fallback; creator fallback.
  - Implement mention alias detection helper for note/comment content, case-sensitive enough to avoid substring mistakes; use configured aliases.
  - Add `tests/test_route_matcher.py` covering the required matcher cases from Metis.

  **Must NOT do**:
  - Do not import `aiohttp`, `urllib`, SQLite, or network code into `route_matcher.py`.
  - Do not read config files inside matcher tests; pass dicts directly.
  - Do not make invalid conditional routes broadcast.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: pure Python module plus behavioral test matrix.
  - Skills: [`everything-claude-code:python-patterns`, `everything-claude-code:python-testing`] - needed for dataclasses and pytest style.
  - Omitted: [`playwright`] - no browser/UI work.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 3, 4, 5, 6 | Blocked By: none

  **References**:
  - Requirement: `state.md:20-43` - proposed conditional routes and backward compatibility.
  - Current maps: `proxy.py:85-90` and `due_poller.py:101-112` - duplicated subscription/UID maps to centralize.
  - Current route loader shape: `proxy.py:124-135`, `due_poller.py:115-117` - legacy list format.
  - Fixtures: `tests/conftest.py:114-129` - existing route fixture to preserve.

  **Acceptance Criteria**:
  - [ ] `tests/test_route_matcher.py` exists and covers legacy match-all, responsible match, unassigned section fallback, no section fallback when assigned to unknown, lifecycle creator fallback, ID string normalization, and string `"0"` not empty.
  - [ ] `python -m pytest tests/test_route_matcher.py -q` exits `0`, output contains `passed`, output does not contain `FAILED`.
  - [ ] `route_matcher.py` has no network, subprocess, filesystem write, or SQLite imports.

  **QA Scenarios**:
  ```
  Scenario: Legacy routes still broadcast
    Tool: Bash
    Steps: Run `python -m pytest tests/test_route_matcher.py -q`.
    Expected: Exit code 0; test asserting legacy config `project-1: [sub-max, sub-smith]` returns both with reason `legacy_match_all` passes.
    Evidence: .sisyphus/evidence/task-1-route-matcher.txt

  Scenario: Assigned task does not section-fallback to another agent
    Tool: Bash
    Steps: Run `python -m pytest tests/test_route_matcher.py -q`.
    Expected: Exit code 0; test with `responsible_uid: "999"` and matching `section_id` returns no section-based match unless rule has responsible `"999"`.
    Evidence: .sisyphus/evidence/task-1-route-matcher-no-section-fallback.txt
  ```

  **Commit**: YES | Message: `feat(routing): add shared route matcher` | Files: [`route_matcher.py`, `tests/test_route_matcher.py`]

- [x] 2. Add persistent per-target delivery dedup ledger

  **What to do**:
  - Extend `control_ledger.py` with a dedicated delivery table, e.g. `delivery_dedup`.
  - Store successful deliveries per target, not just event rows or semantic interactions.
  - Use a key shaped by source, event identity, payload hash/due value, and subscription.
  - Recommended columns:
    - `source TEXT NOT NULL`
    - `event_name TEXT NOT NULL`
    - `entity_id TEXT NOT NULL`
    - `parent_task_id TEXT NOT NULL DEFAULT ''`
    - `due_value TEXT NOT NULL DEFAULT ''`
    - `payload_hash TEXT NOT NULL`
    - `subscription TEXT NOT NULL`
    - `delivered_at TEXT NOT NULL`
    - Unique index across all identity columns plus subscription.
  - Add helper functions:
    - `delivery_identity(event_name, event_data, source, headers_or_due_value)` or equivalent.
    - `has_successful_delivery(...) -> bool`.
    - `record_successful_delivery(...) -> LedgerResult`.
  - For webhooks, prefer `X-Todoist-Delivery-ID` when present; fallback to `event_name + task/comment id + stable payload hash + subscription`.
  - For due poller, use `due_poll + task_id + due_value + subscription` so each recurrence occurrence can refire.
  - Add focused tests either in `tests/test_ledger.py` or a new `tests/test_delivery_dedup.py`.

  **Must NOT do**:
  - Do not use task ID alone for webhook dedup.
  - Do not record dedup before successful downstream response.
  - Do not treat a success for one subscription as a success for another subscription.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: persistent idempotency and retry semantics require careful state design.
  - Skills: [`everything-claude-code:python-patterns`, `everything-claude-code:python-testing`] - needed for SQLite helper tests.
  - Omitted: [`supabase-postgres-best-practices`] - local SQLite only, not Postgres.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 3, 4, 7 | Blocked By: none

  **References**:
  - Current ledger initialization: `control_ledger.py:145-157` - existing gate semantics must stay separate.
  - Current proxy recording: `proxy.py:303-311`, `proxy.py:400-424`, `proxy.py:432-447` - event and delivery audit points.
  - Current due-poller recording: `due_poller.py:457-465`, `due_poller.py:503-514` - event and fired-state points.

  **Acceptance Criteria**:
  - [ ] Delivery dedup table is created by existing schema initialization flow.
  - [ ] Tests prove duplicate success for same subscription is skipped but a different subscription is not skipped.
  - [ ] Tests prove different due value for same task/subscription is not skipped.
  - [ ] Tests prove different payload hash for same webhook task/comment is not skipped unless `X-Todoist-Delivery-ID` explicitly matches.
  - [ ] `python -m pytest tests/test_ledger.py -q` or `python -m pytest tests/test_delivery_dedup.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Same delivery target skips after success
    Tool: Bash
    Steps: Run the focused delivery dedup pytest file.
    Expected: Exit code 0; duplicate identity for `sub-max` is reported as already delivered after recording success.
    Evidence: .sisyphus/evidence/task-2-dedup-success.txt

  Scenario: Failed/other target remains retryable
    Tool: Bash
    Steps: Run the focused delivery dedup pytest file.
    Expected: Exit code 0; `sub-smith` is not considered delivered when only `sub-max` succeeded.
    Evidence: .sisyphus/evidence/task-2-dedup-other-target.txt
  ```

  **Commit**: YES | Message: `feat(ledger): track per-target delivery success` | Files: [`control_ledger.py`, `tests/test_ledger.py` or `tests/test_delivery_dedup.py`]

- [x] 3. Integrate conditional matcher and delivery dedup into proxy task events

  **What to do**:
  - Replace `subscriptions = routes.get(project_id, [])` fanout in `proxy.py` with shared matcher calls.
  - Preserve current route loader behavior, but route loading can delegate to shared normalization.
  - Preserve HMAC validation and raw body forwarding unchanged.
  - Preserve global future-due `item:added` deferral before route matching.
  - For matched targets only:
    - apply `evaluate_forwarding()`.
    - skip delivery if per-target dedup says already successful.
    - deliver if enabled and not already delivered.
    - record successful delivery only after downstream status `< 300`; 3xx/4xx/5xx responses are not recorded as successful dedup hits.
  - Change proxy partial-failure response: if any matched enabled not-already-delivered target fails with 5xx/timeout/error, return `502` so Todoist retries; successful targets are skipped on retry via delivery ledger.
  - Ensure route-filtered targets do not create control-gate rows.
  - Update `tests/test_proxy_webhook.py` and `tests/test_forwarding_controls.py`.

  **Must NOT do**:
  - Do not alter HMAC verification or OAuth callback behavior.
  - Do not let malformed conditional routes become match-all.
  - Do not apply control gates to every subscription in project under conditional routes.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: async proxy integration with behavior-sensitive tests.
  - Skills: [`everything-claude-code:python-patterns`, `everything-claude-code:python-testing`, `everything-claude-code:security-review`] - HMAC/raw body path must remain safe.
  - Omitted: [`playwright`] - no browser/UI work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 5, 7 | Blocked By: 1, 2

  **References**:
  - Parse point: `proxy.py:294-299` - event fields available before matching.
  - Deferral: `proxy.py:322-342` - preserve before matching.
  - Current fanout/gates: `proxy.py:361-409` - replace subscription selection and candidate loop.
  - Forwarding: `proxy.py:429-447` - insert per-target dedup and success recording.
  - Existing fanout test: `tests/test_proxy_webhook.py:155-188` - legacy config must still pass.

  **Acceptance Criteria**:
  - [ ] Legacy flat route test still forwards LowKeyCodes `item:added` to all three subscriptions.
  - [ ] Conditional route test sends assigned Max task only to Max.
  - [ ] Conditional route test sends unassigned Smith-section task only to Smith.
  - [ ] Conditional route test sends assigned Abra task in Smith section to Abra, not Smith.
  - [ ] Lifecycle event test sends to creator and assignee when different.
  - [ ] Relevant but disabled target records existing gate reason such as `agent_disabled:max` and does not deliver.
  - [ ] Duplicate webhook delivery ID skips already-successful target.
  - [ ] Partial failure retry test: first request success+failure returns retryable non-200, retry skips success and retries failed target.
  - [ ] `python -m pytest tests/test_route_matcher.py tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Conditional proxy route avoids unnecessary multi-agent fanout
    Tool: Bash
    Steps: Run `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q`.
    Expected: Exit code 0; conditional assigned-task test records exactly one target URL for the owning subscription.
    Evidence: .sisyphus/evidence/task-3-proxy-conditional.txt

  Scenario: Proxy retry does not resend already-successful target
    Tool: Bash
    Steps: Run `python -m pytest tests/test_proxy_webhook.py -q`.
    Expected: Exit code 0; partial failure retry test shows first successful subscription is not posted again on retry.
    Evidence: .sisyphus/evidence/task-3-proxy-dedup-retry.txt
  ```

  **Commit**: YES | Message: `feat(proxy): route events by conditional target rules` | Files: [`proxy.py`, `route_matcher.py`, `control_ledger.py`, `tests/test_proxy_webhook.py`, `tests/test_forwarding_controls.py`]

- [x] 4. Integrate matcher and per-target dedup into due_poller

  **What to do**:
  - Use shared matcher for `due_poller.py` instead of `subscriptions = routes.get(task["project_id"], [])`.
  - Preserve first-run bootstrap seeding with no delivery and no delivery dedup writes.
  - Preserve dry-run with no deliveries, no fired-state mutations, no delivery ledger writes, and no unblock file writes.
  - Preserve due calculation and recurrence interval logic.
  - Use due-poller synthetic event and `due_value` to build per-target delivery identity.
  - For each matched and enabled target:
    - skip if target already successfully delivered for that due occurrence.
    - call `_unblock()` only immediately before an actual delivery attempt.
    - record successful target delivery only after success.
  - Do not mark a task/due occurrence globally fired until every matched enabled target is either already successfully delivered or newly delivered successfully.
  - If a target fails, leave it retryable for next poll without resending already-successful targets.
  - Update `tests/test_due_poller_ledger.py`.

  **Must NOT do**:
  - Do not change `due_utils.py` semantics.
  - Do not mutate unblock state for skipped already-successful targets.
  - Do not let one target success suppress retry for another failed target.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: stateful poller behavior and recurrence/idempotency tests.
  - Skills: [`everything-claude-code:python-patterns`, `everything-claude-code:python-testing`] - pytest and SQLite state checks.
  - Omitted: [`playwright`] - no UI work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 7 | Blocked By: 1, 2

  **References**:
  - Synthetic event fields: `due_poller.py:255-273` - full task context for matching.
  - Current due task filtering: `due_poller.py:417-423` - keep unchanged.
  - Current fanout: `due_poller.py:447-451` - replace with matcher.
  - Current delivery/fired behavior: `due_poller.py:498-517` - replace any-success fired logic.
  - Current tests: `tests/test_due_poller_ledger.py:86-140` - legacy broadcast should still pass.

  **Acceptance Criteria**:
  - [ ] Legacy flat route still broadcasts due event to all configured subscriptions.
  - [ ] Conditional due route with `responsible_uid` sends only to owner subscription.
  - [ ] Conditional due route with no responsible and matching `section_id` sends only to section owner.
  - [ ] First run bootstrap performs no delivery and writes no delivery dedup rows.
  - [ ] Dry-run performs no delivery, no fired-state mutation, no delivery dedup mutation, and no unblock mutation.
  - [ ] Partial failure retry test shows successful target skipped and failed target retried on next poll.
  - [ ] Recurring due refire test shows new `due_value` permits a new delivery occurrence.
  - [ ] `python -m pytest tests/test_route_matcher.py tests/test_due_poller_ledger.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Due-poller conditional route sends only responsible agent
    Tool: Bash
    Steps: Run `python -m pytest tests/test_due_poller_ledger.py -q`.
    Expected: Exit code 0; conditional due task assigned to Smith delivers only to `smith-lowkeycodes`.
    Evidence: .sisyphus/evidence/task-4-due-poller-conditional.txt

  Scenario: Due-poller retries failed target only
    Tool: Bash
    Steps: Run `python -m pytest tests/test_due_poller_ledger.py -q`.
    Expected: Exit code 0; two-poll test proves previously successful target is skipped and failed target is retried.
    Evidence: .sisyphus/evidence/task-4-due-poller-retry.txt
  ```

  **Commit**: YES | Message: `feat(poller): dedup due deliveries per target` | Files: [`due_poller.py`, `route_matcher.py`, `control_ledger.py`, `tests/test_due_poller_ledger.py`]

- [x] 5. Implement two-phase `note:added` routing

  **What to do**:
  - Extend proxy note handling so `note:added` can route by explicit mention or parent task relevance.
  - Current `_resolve_project_id()` returns only project ID; replace or supplement it with a task-context lookup that can return `project_id`, `section_id`, `responsible_uid`, and creator fields when needed.
  - For `note:added` with explicit mention aliases:
    - route only to mentioned subscriptions within the relevant project when project is known.
    - if project is not in payload, use parent task lookup to determine project when `item_id` exists.
    - for project-level note with `project_id` and no `item_id`, route explicit mentions without task lookup.
  - For `note:added` with no explicit mentions:
    - route by parent task relevance if parent task context resolves.
    - under legacy flat routes, preserve broadcast.
    - under conditional routes, do not broadcast when parent context is unavailable.
  - Distinguish transient lookup errors from 404/deleted task if possible:
    - timeout/5xx should produce retryable proxy failure if no route can be computed.
    - 404/deleted task should log and ignore conditional routes.
  - Update tests in `tests/test_proxy_webhook.py` or create `tests/test_note_routing.py`.

  **Must NOT do**:
  - Do not use LLM/prompt text for note relevance decisions.
  - Do not broadcast conditional routes merely because a note belongs to a routed project.
  - Do not drop legacy flat-route behavior.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: note routing has lookup failures, mention precedence, and legacy compatibility edge cases.
  - Skills: [`everything-claude-code:python-patterns`, `everything-claude-code:python-testing`, `everything-claude-code:security-review`] - webhook behavior and API failure paths.
  - Omitted: [`playwright`] - no UI/browser work.

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: 7 | Blocked By: 1, 2, 3

  **References**:
  - Current note project resolver: `proxy.py:93-102`, `proxy.py:344-350` - currently returns only project_id.
  - Note payload docs: `/home/filipp/Repos/HQ1/Obsidian/HQ1/notes/agents/hermes/current-setup/todoist-webhooks.md:238-245` - raw note fields.
  - Current semantic interaction extraction: `proxy.py:352-359` - semantic rows should still be recorded.
  - Note tests: `tests/test_proxy_webhook.py:241-260` and following tests - preserve existing mention semantic behavior.

  **Acceptance Criteria**:
  - [ ] `note:added` with `@Max` routes only to Max under conditional config.
  - [ ] `note:added` with no mention and parent task assigned to Max routes to Max.
  - [ ] `note:added` with no mention and parent lookup failure does not broadcast conditional routes.
  - [ ] `note:added` with legacy flat routes still broadcasts as before.
  - [ ] Project-level note with `project_id` and `@Smith` routes to Smith without parent lookup.
  - [ ] Existing semantic rows for comment mentions still pass.
  - [ ] `python -m pytest tests/test_proxy_webhook.py tests/test_route_matcher.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Explicit note mention wins over parent-task fanout
    Tool: Bash
    Steps: Run `python -m pytest tests/test_proxy_webhook.py -q`.
    Expected: Exit code 0; `@Max` comment delivers to Max only under conditional routes.
    Evidence: .sisyphus/evidence/task-5-note-explicit-mention.txt

  Scenario: Missing parent context fails closed for conditional routes
    Tool: Bash
    Steps: Run `python -m pytest tests/test_proxy_webhook.py -q`.
    Expected: Exit code 0; no-mention note with failed task lookup causes no conditional deliveries and no crash.
    Evidence: .sisyphus/evidence/task-5-note-fail-closed.txt
  ```

  **Commit**: YES | Message: `feat(proxy): route notes by mentions and task context` | Files: [`proxy.py`, `route_matcher.py`, `tests/test_proxy_webhook.py` or `tests/test_note_routing.py`]

- [x] 6. Update README and operational examples

  **What to do**:
  - Update `README.md` routing section to document both formats.
  - Include a LowKeyCodes conditional route example with Max/Abra/Smith:
    - Max UID `59328091`, CEO section `6gpFcCwF29V6QXxx`.
    - Abra UID `15795569`, Marketing section `6gpFcCvfqGxWcqwx`.
    - Smith UID `29584133`, Development section `6gpFcCxmc39r8MrQ`.
  - Document matching rules by event:
    - `item:added` and due-poller synthetic events: responsible/assignee or unassigned section fallback.
    - lifecycle events: responsible/assignee, unassigned section fallback, or creator/added-by.
    - `note:added`: explicit mention first, parent-task relevance second.
  - Document per-target dedup behavior and proxy retry behavior.
  - Clarify route-filtered targets are not control-gate suppressions.
  - Clarify out of scope: route UI, prompt edits, removing prompt-level cooldown/dedup.
  - Update `state.md` pending issue with implementation direction or completion checklist as appropriate after code lands.

  **Must NOT do**:
  - Do not document unimplemented behavior.
  - Do not claim live Hermes config was migrated.
  - Do not remove historical notes from `state.md` unless replacing with accurate completion summary.

  **Recommended Agent Profile**:
  - Category: `writing` - Reason: documentation and operational examples.
  - Skills: [`stop-slop`] - concise docs without AI-ish verbosity.
  - Omitted: [`copywriting`] - technical docs, not marketing.

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: 7 | Blocked By: 1

  **References**:
  - Current README route docs: `README.md:118-143` - update current flat-list-only description.
  - Current control gate docs: `README.md:171-189` - preserve distinction between route topology and control gates.
  - Current ledger docs: `README.md:195-204` - add delivery dedup table if implemented.
  - Operational note IDs: `/home/filipp/Repos/HQ1/Obsidian/HQ1/notes/agents/hermes/current-setup/todoist-webhooks.md:15-34`, `/home/filipp/Repos/HQ1/Obsidian/HQ1/notes/agents/hermes/current-setup/todoist-webhooks.md:161-187`.
  - Pending issue: `state.md:3-43`.

  **Acceptance Criteria**:
  - [ ] README includes legacy flat broadcast route example.
  - [ ] README includes new conditional route example.
  - [ ] README documents field normalization and event-specific matching rules.
  - [ ] README documents note/comment routing behavior.
  - [ ] README documents per-target dedup and partial retry behavior.
  - [ ] README states route UI and prompt-level dedup removal are out of scope.
  - [ ] `git diff --check` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Documentation contains both route formats
    Tool: Bash
    Steps: Run `git diff --check`; inspect README diff for both `"routes": { "project": [ ... ] }` and object-rule examples.
    Expected: Exit code 0; README includes both examples and no whitespace errors.
    Evidence: .sisyphus/evidence/task-6-docs-diff-check.txt

  Scenario: Docs do not overclaim prompt/live-config changes
    Tool: Bash
    Steps: Search README/state diff for claims that prompts or live Hermes config were edited.
    Expected: No claim that live Hermes prompts/config were modified; out-of-scope section remains explicit.
    Evidence: .sisyphus/evidence/task-6-docs-scope.txt
  ```

  **Commit**: YES | Message: `docs: document conditional Todoist routing` | Files: [`README.md`, `state.md`]

- [x] 7. Full regression, hardening, and cleanup

  **What to do**:
  - Run the full required verification suite.
  - Run Python compile check.
  - Run `git diff --check`.
  - Review diff for accidental unrelated edits.
  - Check that route-filtered targets do not create misleading control-suppression rows.
  - Check that legacy flat route tests still prove existing behavior.
  - Check that conditional route tests prove minimized fanout.
  - Check that dedup tests prove both no duplicate sends and no lost failed-target retries.
  - Save command outputs to `.sisyphus/evidence/`.

  **Must NOT do**:
  - Do not mark implementation complete if any verification command fails.
  - Do not weaken tests to pass.
  - Do not commit generated caches or local runtime DB files.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: whole-repo verification and behavioral audit.
  - Skills: [`everything-claude-code:verification-loop`, `everything-claude-code:python-testing`] - systematic verification.
  - Omitted: [`playwright`] - no UI path changed unless unexpected UI regressions appear.

  **Parallelization**: Can Parallel: NO | Wave 4 | Blocks: Final Verification | Blocked By: 3, 4, 5, 6

  **References**:
  - Test command docs: README Testing section in project read result.
  - Full test set: `tests/` directory.
  - Required final commands from Metis.

  **Acceptance Criteria**:
  - [ ] `python -m pytest tests/test_route_matcher.py -q` exits `0`.
  - [ ] `python -m pytest tests/test_proxy_webhook.py tests/test_forwarding_controls.py -q` exits `0`.
  - [ ] `python -m pytest tests/test_due_poller_ledger.py -q` exits `0`.
  - [ ] `python -m pytest -q` exits `0`.
  - [ ] `python -m compileall /home/filipp/Projects/todoist-proxy` exits `0`.
  - [ ] `git diff --check` exits `0`.
  - [ ] Evidence files exist for all command outputs.

  **QA Scenarios**:
  ```
  Scenario: Full automated regression passes
    Tool: Bash
    Steps: Run `python -m pytest -q`.
    Expected: Exit code 0; output contains `passed`; output does not contain `FAILED`.
    Evidence: .sisyphus/evidence/task-7-full-pytest.txt

  Scenario: Source tree has no syntax or diff hygiene issues
    Tool: Bash
    Steps: Run `python -m compileall /home/filipp/Projects/todoist-proxy` and `git diff --check`.
    Expected: Both exit 0; no syntax errors, whitespace errors, or conflict markers.
    Evidence: .sisyphus/evidence/task-7-compile-diff-check.txt
  ```

  **Commit**: YES | Message: `test: verify Todoist routing and dedup behavior` | Files: [all changed tests/docs/code]

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.

- [x] F1. Plan Compliance Audit — oracle
  - Verify every implemented behavior maps to this plan.
  - Verify no out-of-scope route UI/live Hermes prompt edits were made.
  - Verify legacy route compatibility is proven by tests.

- [x] F2. Code Quality Review — unspecified-high
  - Review `route_matcher.py`, `proxy.py`, `due_poller.py`, and `control_ledger.py` for clear boundaries, duplicated logic, error handling, and maintainability.
  - Confirm Todoist IDs remain opaque strings.

- [x] F3. Real Manual QA — unspecified-high
  - Execute realistic synthetic webhook/due-poller flows through tests or local stubs:
    - assigned task to one agent.
    - unassigned task by section fallback.
    - lifecycle event to assignee and creator.
    - note mention to one agent.
    - partial failure then retry.
  - Save evidence under `.sisyphus/evidence/final-qa-*`.

- [x] F4. Scope Fidelity Check — deep
  - Confirm no unrelated files or behavior were changed.
  - Confirm dedup changes solve “do not resend successes / do retry failures.”
  - Confirm prompt-level stateful dedup/cooldown remains untouched.

## Commit Strategy

Use atomic commits after passing each task’s acceptance checks:

1. `feat(routing): add shared route matcher`
2. `feat(ledger): track per-target delivery success`
3. `feat(proxy): route events by conditional target rules`
4. `feat(poller): dedup due deliveries per target`
5. `feat(proxy): route notes by mentions and task context`
6. `docs: document conditional Todoist routing`
7. `test: verify Todoist routing and dedup behavior`

If hooks or tests fail, fix in a new working diff before committing. Do not amend failed commits unless explicitly requested by user.

## Success Criteria

- Conditional route config can express direct ownership rules for Max/Abra/Smith.
- Legacy route config remains supported and still broadcasts.
- Assigned task events go only to the responsible agent under conditional routes.
- Unassigned task events route by section owner under conditional routes.
- Lifecycle events can intentionally go to both assignee and creator/delegator.
- Note events route to explicit mentions first and otherwise by parent task relevance.
- Proxy and due-poller share the same route matching semantics.
- Per-target dedup prevents duplicate sends to successful targets.
- Failed targets remain retryable and are not lost because another target succeeded.
- Full pytest suite, compile check, and diff check pass.
