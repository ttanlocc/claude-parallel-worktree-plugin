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
    assert doc["ts"] == 1788700000.0
    assert doc["session"] == "s1"
    assert doc["question"] == "OAuth expired again"
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
        [
            {
                "id": "e4",
                "kind": "red_tests",
                "question": "?",
                "status": "answered",
                "answer": "rerun",
                "answered_at": 1788700100.0,
            }
        ]
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


def test_escalation_docs_default_status_to_open_when_status_is_absent():
    """A record can reach here before a status key was ever written. It must still render as
    open — a live escalation waiting on an answer — rather than a blank state that hides it
    from the board."""
    docs = escalation_docs([{"id": "e6", "kind": "looping", "question": "?"}])

    assert docs["e6"]["status"] == "open"


from board_state import ticket_docs


def test_ticket_docs_key_on_ado_id_and_attach_a_known_pr():
    docs = ticket_docs(
        [
            {
                "id": "8311",
                "title": "Stabilise tool order",
                "state": "Active",
                "sprint": "Sprint 57",
                "url": "https://dev.azure.com/x/_workitems/edit/8311",
            }
        ],
        {"8311": {"number": 698, "state": "OPEN", "url": "https://github.com/o/r/pull/698"}},
    )

    doc = docs["8311"]
    assert doc["id"] == "8311"
    assert doc["title"] == "Stabilise tool order"
    assert doc["state"] == "Active"
    assert doc["sprint"] == "Sprint 57"
    assert doc["url"] == "https://dev.azure.com/x/_workitems/edit/8311"
    assert doc["pr"] == {"number": 698, "state": "OPEN", "url": "https://github.com/o/r/pull/698"}


def test_ticket_docs_leave_pr_null_when_no_pr_references_the_ticket():
    docs = ticket_docs(
        [{"id": "5061", "title": "F1 nondeterminism", "state": "New", "sprint": "Sprint 57", "url": "u"}], {}
    )

    assert docs["5061"]["pr"] is None


def test_ticket_docs_keep_an_empty_sprint_rather_than_inventing_one():
    """Work items parked at the project root genuinely have no sprint; the board filters on
    this field and a made-up value would mis-file them."""
    docs = ticket_docs([{"id": "1", "title": "t", "state": "New", "sprint": "", "url": "u"}], {})

    assert docs["1"]["sprint"] == ""


def test_ticket_docs_skip_a_ticket_with_no_id():
    assert ticket_docs([{"title": "orphan", "state": "New"}], {}) == {}


def test_ticket_docs_carry_type_and_default_it_to_empty_when_absent():
    """`type` distinguishes a Bug from a User Story on the board; a ticket with no type on the
    ADO side must render as unknown, not silently inherit some other ticket's type."""
    docs = ticket_docs(
        [
            {"id": "1", "title": "t1", "state": "New", "sprint": "S", "type": "Bug", "url": "u1"},
            {"id": "2", "title": "t2", "state": "New", "sprint": "S", "url": "u2"},
        ],
        {},
    )

    assert docs["1"]["type"] == "Bug"
    assert docs["2"]["type"] == ""


from board_state import meta_status


def test_meta_status_records_each_source_separately():
    """The cadences differ by design — sessions update on events, ADO on a slow cron. One
    combined timestamp would let a 30-minute-old ticket list look as fresh as a live session."""
    doc = meta_status(
        now=1000.0, ado_swept_at=400.0, sessions_scanned_at=990.0, manager={"session_id": "m1", "started_at": 100.0}
    )

    assert doc["last_ado_sweep"] == 400.0
    assert doc["last_session_scan"] == 990.0
    assert doc["manager_session_id"] == "m1"
    assert doc["manager_started_at"] == 100.0
    assert doc["written_at"] == 1000.0


def test_meta_status_reports_a_source_that_has_never_run_as_null_not_as_now():
    """A never-run sweep must not read as a fresh one. The page shows age from these fields,
    and `now` here would claim data that does not exist."""
    doc = meta_status(now=1000.0)

    assert doc["last_ado_sweep"] is None
    assert doc["last_session_scan"] is None
    assert doc["manager_session_id"] is None
    assert doc["manager_started_at"] is None
    assert doc["written_at"] == 1000.0


from board_state import build_writes


def _writes_for(writes, collection):
    return [w for w in writes if w["collection"] == collection]


def test_build_writes_emits_one_set_per_document_across_all_four_collections():
    writes = build_writes(
        agents=[{"name": "t1", "sessionId": "s1", "state": "running"}],
        registry={},
        escalations=[{"id": "e1", "kind": "credentials", "question": "?"}],
        tickets=[{"id": "8311", "title": "x", "state": "Active", "sprint": "Sprint 57", "url": "u"}],
        pr_by_ticket={},
        now=1000.0,
    )

    assert {w["collection"] for w in writes} == {"sessions", "escalations", "tickets", "meta"}
    assert all(w["op"] == "set" for w in writes)
    assert _writes_for(writes, "sessions")[0]["doc_id"] == "t1"
    assert _writes_for(writes, "escalations")[0]["doc_id"] == "e1"
    assert _writes_for(writes, "tickets")[0]["doc_id"] == "8311"
    assert _writes_for(writes, "meta")[0]["doc_id"] == "status"
    # Sessions, escalations AND tickets are all non-empty here — unlike the dedicated
    # "meta last" test below (which only populates sessions), this actually discriminates
    # "last overall" from "last among the only populated collection".
    assert writes[-1]["collection"] == "meta"


def test_build_writes_puts_meta_status_last():
    """`meta/status` claims the data alongside it is current. Written first, a batch that dies
    halfway would advertise a sweep whose rows never landed."""
    writes = build_writes(
        agents=[{"name": "t1", "sessionId": "s1", "state": "idle"}],
        registry={},
        escalations=[],
        tickets=[],
        pr_by_ticket={},
        now=1000.0,
    )

    assert writes[-1]["collection"] == "meta"
    assert writes[-1]["doc_id"] == "status"


def test_build_writes_stamps_the_session_scan_time_from_now():
    writes = build_writes(
        agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1234.0, ado_swept_at=999.0
    )

    meta = writes[-1]["data"]
    assert meta["last_session_scan"] == 1234.0
    assert meta["last_ado_sweep"] == 999.0


def test_build_writes_on_empty_sources_still_writes_meta():
    """An empty board is a real state — no workers, no escalations. It must be distinguishable
    from a sweep that never ran, and only meta/status can say which."""
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1000.0)

    assert len(writes) == 1
    assert writes[0]["doc_id"] == "status"


# --- Coverage added beyond the brief -----------------------------------------------------
#
# The four tests above are the brief's own, added verbatim. Everything below closes gaps the
# brief's tests leave open: none of them check the `data` body's provenance for any collection,
# whether `manager` or `written_at` actually reach `meta_status`, whether a never-run
# `ado_swept_at` survives as null rather than defaulting to `now`, or whether entry counts match
# document counts (a duplicate or a drop is invisible to a set-membership or first-element check).


def test_build_writes_on_empty_sources_the_sole_entry_has_meta_collection_and_set_op():
    """The brief's empty-sources test pins doc_id and count but not collection or op — a stray
    write with doc_id "status" filed under the wrong collection, or with the wrong op, would
    still pass it silently."""
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1000.0)

    assert writes[0]["collection"] == "meta"
    assert writes[0]["op"] == "set"


def test_build_writes_data_matches_the_underlying_transform_for_each_collection():
    """No brief test compares `data` against its source transform's own output — only doc_id is
    checked. A dropped `registry`/`pr_by_ticket`/`manager` argument, or `data` replaced by `{}`,
    would pass every existing assertion. Registry/pr_by_ticket/manager/ado_swept_at are all
    non-empty here so a dropped argument changes the result instead of coincidentally matching a
    default."""
    agents = [{"name": "t1", "sessionId": "s1", "state": "running"}]
    registry = {"t1": {"branch": "feature/x", "path": "/repo/wt", "short_id": "abc123", "ado_ids": ["9"]}}
    escalations = [{"id": "e1", "kind": "credentials", "question": "?"}]
    tickets = [{"id": "8311", "title": "x", "state": "Active", "sprint": "Sprint 57", "url": "u"}]
    pr_by_ticket = {"8311": {"number": 42, "state": "OPEN", "url": "https://example/42"}}
    manager = {"session_id": "m1", "started_at": 100.0}

    writes = build_writes(
        agents=agents,
        registry=registry,
        escalations=escalations,
        tickets=tickets,
        pr_by_ticket=pr_by_ticket,
        now=1234.0,
        ado_swept_at=999.0,
        manager=manager,
    )

    assert _writes_for(writes, "sessions")[0]["data"] == session_docs(agents, registry)["t1"]
    assert _writes_for(writes, "escalations")[0]["data"] == escalation_docs(escalations)["e1"]
    assert _writes_for(writes, "tickets")[0]["data"] == ticket_docs(tickets, pr_by_ticket)["8311"]
    assert _writes_for(writes, "meta")[0]["data"] == meta_status(
        now=1234.0, ado_swept_at=999.0, sessions_scanned_at=1234.0, manager=manager
    )


def test_build_writes_leaves_ado_swept_at_null_when_never_swept():
    """`ado_swept_at` defaults to None — a sweep that has never run. A default of `now` here
    would make a sweep that never ran look as fresh as the session scan happening in the same
    call, which is exactly the false freshness `meta_status` exists to prevent."""
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1234.0)

    assert writes[-1]["data"]["last_ado_sweep"] is None


def test_build_writes_emits_exactly_one_entry_per_document_with_no_duplicates_or_drops():
    """The brief's test name claims "one set per document" but only checks set membership and
    the first entry per collection — a duplicated or a dropped write would still pass. Two
    documents per collection means a duplicate-without-drop (caught by the length check) and a
    drop-with-substitution (caught by the doc_id-set check) are both visible."""
    writes = build_writes(
        agents=[
            {"name": "t1", "sessionId": "s1", "state": "running"},
            {"name": "t2", "sessionId": "s2", "state": "idle"},
        ],
        registry={},
        escalations=[
            {"id": "e1", "kind": "credentials", "question": "?"},
            {"id": "e2", "kind": "scope_question", "question": "?"},
        ],
        tickets=[
            {"id": "1", "title": "a", "state": "New", "sprint": "S", "url": "u1"},
            {"id": "2", "title": "b", "state": "New", "sprint": "S", "url": "u2"},
        ],
        pr_by_ticket={},
        now=1000.0,
    )

    assert len(writes) == 7
    assert len(_writes_for(writes, "sessions")) == 2
    assert len(_writes_for(writes, "escalations")) == 2
    assert len(_writes_for(writes, "tickets")) == 2
    assert len(_writes_for(writes, "meta")) == 1
    assert {w["doc_id"] for w in _writes_for(writes, "sessions")} == {"t1", "t2"}
    assert {w["doc_id"] for w in _writes_for(writes, "escalations")} == {"e1", "e2"}
    assert {w["doc_id"] for w in _writes_for(writes, "tickets")} == {"1", "2"}
