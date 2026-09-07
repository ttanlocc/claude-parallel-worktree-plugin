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


from board_state import escalation_docs


def test_escalation_docs_normalise_kind_and_keep_the_raw_spelling():
    """A worker wrote `blocked_on_credentials` where the vocabulary says `credentials`. The
    board must score it as the credentials outage it is, while still showing what was written
    so the drift gets fixed."""
    docs = escalation_docs(
        [
            {
                "id": "e1",
                "ts": 1788700000.0,
                "session_id": "s1",
                "kind": "blocked_on_credentials",
                "question": "OAuth expired again",
                "options": ["Renew the credential", "Switch to a service account"],
                "status": "open",
            }
        ]
    )

    doc = docs["e1"]
    assert doc["kind"] == "credentials"
    assert doc["kind_raw"] == "blocked_on_credentials"
    assert doc["severity"] == "P0"
    assert doc["options"] == ["Renew the credential", "Switch to a service account"]
    assert doc["status"] == "open"
    assert doc["answer"] is None


def test_escalation_docs_score_an_unrecognisable_kind_below_an_urgent_one_but_still_flag_it():
    """Widening what is recognised must not widen what looks urgent — an unknown kind is still
    a human's call (P1), never silently promoted."""
    docs = escalation_docs([{"id": "e2", "kind": "totally_made_up", "question": "?"}])

    assert docs["e2"]["kind"] is None
    assert docs["e2"]["kind_raw"] == "totally_made_up"
    assert docs["e2"]["severity"] == "P1"


def test_escalation_docs_mark_a_mechanical_kind_lowest():
    docs = escalation_docs([{"id": "e3", "kind": "scope_question", "question": "in or out?"}])

    assert docs["e3"]["severity"] == "P2"


def test_escalation_docs_carry_an_answer_once_one_exists():
    docs = escalation_docs(
        [{"id": "e4", "kind": "red_tests", "question": "?", "status": "answered",
          "answer": "rerun", "answered_at": 1788700100.0}]
    )

    assert docs["e4"]["status"] == "answered"
    assert docs["e4"]["answer"] == "rerun"
    assert docs["e4"]["answered_at"] == 1788700100.0


def test_escalation_docs_tolerate_a_string_options_field():
    """`options` is worker-authored against a prose schema; a bare string is one option, not a
    sequence of characters to iterate."""
    docs = escalation_docs([{"id": "e5", "kind": "looping", "question": "?", "options": "just this"}])

    assert docs["e5"]["options"] == ["just this"]


def test_escalation_docs_skip_a_record_with_no_id():
    assert escalation_docs([{"kind": "looping", "question": "?"}]) == {}
