from __future__ import annotations

import inspect

import route_matcher
from route_matcher import MatchedRoute
from conftest import LOWKEYCODES_PROJECT_ID


SECTION_MAX = "6gpFcCwF29V6QXxx"
SECTION_ABRA = "6gpFcCvfqGxWcqwx"
SECTION_SMITH = "6gpFcCxmc39r8MrQ"


def conditional_routes() -> dict[str, dict[str, dict[str, object]]]:
    return {
        LOWKEYCODES_PROJECT_ID: {
            "max-lowkeycodes": {
                "agent": "max",
                "responsible_uids": ["59328091"],
                "section_ids": [SECTION_MAX],
                "creator_uids": ["59328091"],
                "mention_aliases": ["@Max", "Max", "Max | CEO"],
            },
            "abra-lowkeycodes": {
                "agent": "abra",
                "responsible_uids": ["15795569"],
                "section_ids": [SECTION_ABRA],
                "creator_uids": ["15795569"],
                "mention_aliases": ["@Abra", "Abra", "Abra | CMO"],
            },
            "smith-lowkeycodes": {
                "agent": "smith",
                "responsible_uids": ["29584133"],
                "section_ids": [SECTION_SMITH],
                "creator_uids": ["29584133"],
                "mention_aliases": ["@Smith", "Smith", "Smith | DevOps"],
            },
        }
    }


def test_legacy_flat_list_routes_match_all_subscriptions() -> None:
    routes = {
        LOWKEYCODES_PROJECT_ID: [
            "max-lowkeycodes",
            "abra-lowkeycodes",
            "smith-lowkeycodes",
        ]
    }

    matches = route_matcher.match_routes(
        routes,
        "item:added",
        {"project_id": LOWKEYCODES_PROJECT_ID, "responsible_uid": "nobody"},
    )

    assert matches == [
        MatchedRoute("max-lowkeycodes", "max", "legacy_match_all", True),
        MatchedRoute("abra-lowkeycodes", "abra", "legacy_match_all", True),
        MatchedRoute("smith-lowkeycodes", "smith", "legacy_match_all", True),
    ]


def test_responsible_uid_match_wins_before_other_fallbacks_for_same_rule() -> None:
    routes = {
        LOWKEYCODES_PROJECT_ID: {
            "max-lowkeycodes": {
                "agent": "max",
                "responsible_uids": ["59328091"],
                "section_ids": [SECTION_MAX],
                "creator_uids": ["59328091"],
            }
        }
    }

    matches = route_matcher.match_routes(
        routes,
        "item:updated",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "responsible_uid": "59328091",
            "section_id": SECTION_MAX,
            "creator_uid": "59328091",
        },
    )

    assert matches == [MatchedRoute("max-lowkeycodes", "max", "responsible_uid=59328091", False)]


def test_assignee_id_is_used_when_responsible_uid_is_absent() -> None:
    routes = conditional_routes()

    matches = route_matcher.match_routes(
        routes,
        "item:added",
        {"project_id": LOWKEYCODES_PROJECT_ID, "assignee_id": "15795569"},
    )

    assert matches == [MatchedRoute("abra-lowkeycodes", "abra", "assignee_id=15795569", False)]


def test_unassigned_section_fallback_matches_when_responsible_missing_none_or_empty() -> None:
    routes = conditional_routes()

    for event_data in (
        {"project_id": LOWKEYCODES_PROJECT_ID, "section_id": SECTION_SMITH},
        {"project_id": LOWKEYCODES_PROJECT_ID, "responsible_uid": None, "section_id": SECTION_SMITH},
        {"project_id": LOWKEYCODES_PROJECT_ID, "responsible_uid": "", "section_id": SECTION_SMITH},
    ):
        assert route_matcher.match_routes(routes, "item:added", event_data) == [
            MatchedRoute("smith-lowkeycodes", "smith", "section_id=6gpFcCxmc39r8MrQ", False)
        ]


def test_section_fallback_does_not_match_when_assigned_to_unknown_user() -> None:
    matches = route_matcher.match_routes(
        conditional_routes(),
        "item:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "responsible_uid": "99999999",
            "section_id": SECTION_SMITH,
        },
    )

    assert matches == []


def test_string_zero_is_not_empty_and_blocks_section_fallback() -> None:
    routes = {
        LOWKEYCODES_PROJECT_ID: {
            "zero-agent": {
                "agent": "zero",
                "responsible_uids": ["0"],
                "section_ids": [SECTION_MAX],
            },
            "max-lowkeycodes": {
                "agent": "max",
                "responsible_uids": ["59328091"],
                "section_ids": [SECTION_MAX],
            },
        }
    }

    assert route_matcher.opaque_id("0") == "0"
    assert route_matcher.opaque_id(0) == "0"
    matches = route_matcher.match_routes(
        routes,
        "item:added",
        {"project_id": LOWKEYCODES_PROJECT_ID, "responsible_uid": "0", "section_id": SECTION_MAX},
    )

    assert matches == [MatchedRoute("zero-agent", "zero", "responsible_uid=0", False)]


def test_lifecycle_creator_fallback_applies_only_to_update_complete_uncomplete() -> None:
    routes = conditional_routes()
    event_data = {
        "project_id": LOWKEYCODES_PROJECT_ID,
        "responsible_uid": "99999999",
        "creator_uid": "29584133",
    }

    for event_name in ("item:updated", "item:completed", "item:uncompleted"):
        assert route_matcher.match_routes(routes, event_name, event_data) == [
            MatchedRoute("smith-lowkeycodes", "smith", "creator_uid=29584133", False)
        ]

    assert route_matcher.match_routes(routes, "item:added", event_data) == []


def test_creator_field_normalization_prefers_added_by_uid_then_creator_uid_then_creator_id() -> None:
    routes = {
        LOWKEYCODES_PROJECT_ID: {
            "max-lowkeycodes": {"agent": "max", "creator_uids": ["59328091"]},
            "smith-lowkeycodes": {"agent": "smith", "creator_uids": ["29584133"]},
        }
    }

    matches = route_matcher.match_routes(
        routes,
        "item:updated",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "added_by_uid": "59328091",
            "creator_uid": "29584133",
        },
    )

    assert matches == [MatchedRoute("max-lowkeycodes", "max", "added_by_uid=59328091", False)]

    matches = route_matcher.match_routes(
        routes,
        "item:completed",
        {"project_id": LOWKEYCODES_PROJECT_ID, "creator_id": "29584133"},
    )

    assert matches == [MatchedRoute("smith-lowkeycodes", "smith", "creator_id=29584133", False)]


def test_note_added_matches_configured_mention_alias_with_boundaries() -> None:
    routes = conditional_routes()

    matches = route_matcher.match_routes(
        routes,
        "note:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "id": "note-1",
            "item_id": "task-1",
            "posted_uid": "15611160",
            "content": "Can @Max review this?",
        },
    )

    assert matches == [MatchedRoute("max-lowkeycodes", "max", "mention_alias=@Max", False)]
    assert route_matcher.mention_alias_in_text("Maximum value changed", ["Max"]) == ""
    assert route_matcher.mention_alias_in_text("Please ask Max.", ["Max"]) == "Max"


def test_note_added_explicit_mention_wins_over_parent_task_relevance() -> None:
    matches = route_matcher.match_routes(
        conditional_routes(),
        "note:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "item_id": "task-1",
            "content": "@Max can you check this task?",
            "responsible_uid": "29584133",
            "section_id": SECTION_SMITH,
        },
    )

    assert matches == [MatchedRoute("max-lowkeycodes", "max", "mention_alias=@Max", False)]


def test_note_added_without_mention_falls_back_to_parent_task_relevance() -> None:
    matches = route_matcher.match_routes(
        conditional_routes(),
        "note:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "item_id": "task-1",
            "content": "Deployment notes are ready",
            "responsible_uid": "59328091",
            "section_id": SECTION_SMITH,
        },
    )

    assert matches == [MatchedRoute("max-lowkeycodes", "max", "responsible_uid=59328091", False)]


def test_note_added_without_mention_can_fall_back_to_parent_creator() -> None:
    matches = route_matcher.match_routes(
        conditional_routes(),
        "note:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "item_id": "task-1",
            "content": "Creator should keep visibility on this thread",
            "responsible_uid": "99999999",
            "creator_uid": "15795569",
        },
    )

    assert matches == [MatchedRoute("abra-lowkeycodes", "abra", "creator_uid=15795569", False)]


def test_note_helpers_keep_ids_as_opaque_strings() -> None:
    event_data = {"id": 123, "task_id": 456, "item_id": 789, "posted_uid": "0"}

    assert route_matcher.task_id(event_data) == "123"
    assert route_matcher.note_parent_id(event_data) == "789"
    assert route_matcher.note_author_id(event_data) == "0"


def test_malformed_conditional_routes_fail_closed_without_broadcasting() -> None:
    routes = {
        LOWKEYCODES_PROJECT_ID: {
            "max-lowkeycodes": ["not", "a", "rule"],
            "abra-lowkeycodes": "also-not-a-rule",
            "smith-lowkeycodes": None,
        }
    }

    matches = route_matcher.match_routes(
        routes,
        "item:added",
        {
            "project_id": LOWKEYCODES_PROJECT_ID,
            "responsible_uid": "59328091",
            "section_id": SECTION_ABRA,
        },
    )

    assert matches == []


def test_invalid_project_route_shape_fails_closed() -> None:
    assert route_matcher.match_project_routes("max-lowkeycodes", "item:added", {}) == []
    assert route_matcher.match_project_routes(None, "item:added", {}) == []


def test_route_matcher_keeps_forbidden_imports_out() -> None:
    source = inspect.getsource(route_matcher)

    forbidden = ("aiohttp", "urllib", "sqlite3", "subprocess")
    for name in forbidden:
        assert name not in source
