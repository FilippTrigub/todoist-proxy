"""Deterministic semantic interaction extraction for Todoist webhook events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


UID_LABELS: dict[str, str] = {
    "15611160": "Filipp",
    "15795569": "Abra",
    "29584133": "Smith",
    "59138424": "Hausmeister",
    "59328091": "Max",
}

MENTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Max": ("@Max", "Max", "Max | CEO"),
    "Abra": ("@Abra", "Abra", "Abra | CMO"),
    "Smith": ("@Smith", "Smith", "Smith | DevOps"),
    "Hausmeister": ("@Hausmeister", "Hausmeister"),
}


@dataclass(frozen=True)
class SemanticInteraction:
    actor: str
    target: str
    interaction_kind: str
    confidence: str
    todoist_task_id: str
    reason: str
    parent_task_id: str = ""


def extract_interactions(
    event_name: str,
    event_data: Mapping[str, Any],
) -> tuple[SemanticInteraction, ...]:
    """Return semantic actor-target interactions for supported Todoist events.

    The extractor is intentionally small and deterministic: it does not fetch
    Todoist data, perform fuzzy/NLP matching, or store raw payload text in the
    reason field. Unsupported events and incomplete events return no rows.
    """

    if not isinstance(event_data, Mapping):
        return ()

    if event_name == "item:added":
        return _extract_item_added(event_data)
    if event_name == "note:added":
        return _extract_note_added(event_data)
    return ()


def _extract_item_added(event_data: Mapping[str, Any]) -> tuple[SemanticInteraction, ...]:
    target_uid = _string_value(event_data.get("responsible_uid"))
    if not target_uid:
        return ()

    actor_uid = _string_value(event_data.get("creator_uid")) or _string_value(
        event_data.get("added_by_uid")
    )
    if not actor_uid:
        return ()

    actor, actor_known = _label_for_uid(actor_uid)
    target, target_known = _label_for_uid(target_uid)
    task_id = _string_value(event_data.get("id"))
    parent_task_id = _string_value(event_data.get("parent_id")) or _string_value(
        event_data.get("parentId")
    )
    confidence = "exact" if actor_known and target_known else "unknown_uid"

    return (
        SemanticInteraction(
            actor=actor,
            target=target,
            interaction_kind="task_assigned",
            confidence=confidence,
            todoist_task_id=task_id,
            reason=f"responsible_uid={target_uid}",
            parent_task_id=parent_task_id,
        ),
    )


def _extract_note_added(event_data: Mapping[str, Any]) -> tuple[SemanticInteraction, ...]:
    actor_uid = _string_value(event_data.get("posted_uid"))
    if not actor_uid:
        return ()

    content = _string_value(event_data.get("content"))
    if not content:
        return ()

    actor, actor_known = _label_for_uid(actor_uid)
    comment_id = _string_value(event_data.get("id"))
    task_id = _string_value(event_data.get("item_id"))
    confidence = "exact" if actor_known else "unknown_uid"

    interactions: list[SemanticInteraction] = []
    for target, aliases in MENTION_ALIASES.items():
        mention = _first_mention(content, aliases)
        if mention is None:
            continue
        interactions.append(
            SemanticInteraction(
                actor=actor,
                target=target,
                interaction_kind="comment_mentioned",
                confidence=confidence,
                todoist_task_id=task_id,
                reason=f"mention={mention} comment_id={comment_id}",
            )
        )
    return tuple(interactions)


def _label_for_uid(uid: str) -> tuple[str, bool]:
    label = UID_LABELS.get(uid)
    if label is not None:
        return label, True
    return f"uid:{uid}", False


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _first_mention(content: str, aliases: tuple[str, ...]) -> str | None:
    matches: list[tuple[int, int, str]] = []
    for alias in aliases:
        match = _mention_pattern(alias).search(content)
        if match is not None:
            matches.append((match.start(), len(alias), alias))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], -item[1], item[2]))
    return matches[0][2]


def _mention_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_@]){re.escape(alias)}(?![A-Za-z0-9_])")
