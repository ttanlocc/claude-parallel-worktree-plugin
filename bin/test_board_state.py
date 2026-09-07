#!/usr/bin/env python3
"""assert-based checks for board_state, the pure transform behind the artifact board."""

from board_state import session_docs


def test_session_docs_key_on_task_name_and_carry_registry_fields():
    agents = [
        {
            "name": "bms-auto-match",
            "sessionId": "aaaabbbb-cccc-dddd-eeee-ffff00001111",
            "state": "running",
            "startedAt": 1788762621779,
        }
    ]
    registry = {
        "bms-auto-match": {
            "branch": "feature/bms-auto-match",
            "path": "/repo/.claude/worktrees/bms-auto-match",
            "short_id": "bac537a1",
            "ado_ids": ["7763"],
        }
    }

    docs = session_docs(agents, registry)

    assert list(docs) == ["bms-auto-match"]
    doc = docs["bms-auto-match"]
    assert doc["task"] == "bms-auto-match"
    assert doc["session_id"] == "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    assert doc["short_id"] == "bac537a1"
    assert doc["state"] == "running"
    assert doc["branch"] == "feature/bms-auto-match"
    assert doc["worktree"] == "/repo/.claude/worktrees/bms-auto-match"
    assert doc["ado_refs"] == ["7763"]
    assert doc["started_at"] == 1788762621779


def test_session_docs_survive_an_agent_the_registry_never_heard_of():
    """A session dispatched by any other means still belongs on the board — the registry only
    adds branch/worktree once parallel-task.sh provisioned it."""
    docs = session_docs([{"name": "ad-hoc", "sessionId": "s1", "state": "idle"}], {})

    assert docs["ad-hoc"]["state"] == "idle"
    assert docs["ad-hoc"]["branch"] is None
    assert docs["ad-hoc"]["ado_refs"] == []


def test_session_docs_read_state_from_either_field_name():
    """`claude agents --json` has used both `state` and `status`; the dashboard already reads
    whichever is present and this must not disagree with it."""
    assert session_docs([{"name": "a", "sessionId": "s", "status": "waiting"}], {})["a"]["state"] == "waiting"


def test_session_docs_skip_an_entry_with_no_name():
    """A document id cannot be empty. An unnamed agent is dropped rather than written to a
    garbage key that nothing can address later."""
    assert session_docs([{"sessionId": "s1", "state": "idle"}], {}) == {}
