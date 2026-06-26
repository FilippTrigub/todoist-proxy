"""Focused unit tests for deterministic Todoist semantic extraction."""

from __future__ import annotations

from interaction_extractor import SemanticInteraction, extract_interactions


def test_item_added_assignment_uses_creator_before_added_by() -> None:
    rows = extract_interactions(
        "item:added",
        {
            "id": "task-001",
            "creator_uid": "15611160",
            "added_by_uid": "59328091",
            "responsible_uid": "29584133",
        },
    )

    assert rows == (
        SemanticInteraction(
            actor="Filipp",
            target="Smith",
            interaction_kind="task_assigned",
            confidence="exact",
            todoist_task_id="task-001",
            reason="responsible_uid=29584133",
        ),
    )


def test_item_added_assignment_falls_back_to_added_by_uid() -> None:
    rows = extract_interactions(
        "item:added",
        {
            "id": "task-002",
            "added_by_uid": "59328091",
            "responsible_uid": "15795569",
        },
    )

    assert rows[0].actor == "Max"
    assert rows[0].target == "Abra"
    assert rows[0].confidence == "exact"


def test_item_added_without_target_records_no_rows() -> None:
    assert extract_interactions(
        "item:added",
        {"id": "task-003", "creator_uid": "15611160"},
    ) == ()


def test_item_added_unknown_present_uid_gets_uid_label_and_unknown_confidence() -> None:
    rows = extract_interactions(
        "item:added",
        {
            "id": "task-004",
            "creator_uid": "15611160",
            "responsible_uid": "99999999",
        },
    )

    assert rows[0].actor == "Filipp"
    assert rows[0].target == "uid:99999999"
    assert rows[0].confidence == "unknown_uid"


def test_note_added_extracts_one_row_per_explicit_mention_with_parent_task_id() -> None:
    rows = extract_interactions(
        "note:added",
        {
            "id": "comment-001",
            "content": "@Max @Abra can you both check this?",
            "item_id": "task-parent-001",
            "posted_uid": "29584133",
        },
    )

    assert rows == (
        SemanticInteraction(
            actor="Smith",
            target="Max",
            interaction_kind="comment_mentioned",
            confidence="exact",
            todoist_task_id="task-parent-001",
            reason="mention=@Max comment_id=comment-001",
        ),
        SemanticInteraction(
            actor="Smith",
            target="Abra",
            interaction_kind="comment_mentioned",
            confidence="exact",
            todoist_task_id="task-parent-001",
            reason="mention=@Abra comment_id=comment-001",
        ),
    )


def test_note_added_supports_role_aliases_and_hausmeister() -> None:
    rows = extract_interactions(
        "note:added",
        {
            "id": "comment-002",
            "content": "Max | CEO, Abra | CMO, Smith | DevOps, and Hausmeister should see this.",
            "item_id": "task-parent-002",
            "posted_uid": "15611160",
        },
    )

    assert [row.target for row in rows] == ["Max", "Abra", "Smith", "Hausmeister"]
    assert [row.reason for row in rows] == [
        "mention=Max | CEO comment_id=comment-002",
        "mention=Abra | CMO comment_id=comment-002",
        "mention=Smith | DevOps comment_id=comment-002",
        "mention=Hausmeister comment_id=comment-002",
    ]


def test_note_added_boundaries_do_not_match_substrings() -> None:
    rows = extract_interactions(
        "note:added",
        {
            "id": "comment-003",
            "content": "maximum smithing effort with Abracadabra, but no explicit mention",
            "item_id": "task-parent-003",
            "posted_uid": "29584133",
        },
    )

    assert rows == ()


def test_note_added_unknown_poster_keeps_known_target_and_unknown_confidence() -> None:
    rows = extract_interactions(
        "note:added",
        {
            "id": "comment-004",
            "content": "Max should see this handoff",
            "item_id": "task-parent-004",
            "posted_uid": "99999999",
        },
    )

    assert rows == (
        SemanticInteraction(
            actor="uid:99999999",
            target="Max",
            interaction_kind="comment_mentioned",
            confidence="unknown_uid",
            todoist_task_id="task-parent-004",
            reason="mention=Max comment_id=comment-004",
        ),
    )


def test_unsupported_event_records_no_rows() -> None:
    assert extract_interactions("item:updated", {"id": "task-005"}) == ()
