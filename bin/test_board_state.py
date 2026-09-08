#!/usr/bin/env python3
"""assert-based checks for board_state, the pure transform behind the artifact board."""

import re

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


def test_session_docs_mark_a_registered_task_as_managed_even_with_no_branch_field():
    """`managed` must come from the registry ENTRY EXISTING, not from any field on it happening
    to be non-empty. parallel-task.sh can record a row before branch resolution lands in it, and
    that row is still work the manager dispatched — `bool(branch)` (or `bool(reg)`, which is
    false too for an entry with no fields yet) would both wrongly call it ad-hoc."""
    agents = [{"name": "t1", "sessionId": "s1", "state": "running"}]
    registry = {"t1": {}}  # entry exists, but carries no fields at all yet

    doc = session_docs(agents, registry)["t1"]

    assert doc["managed"] is True
    assert doc["branch"] is None


def test_session_docs_mark_an_unregistered_task_as_not_managed():
    """A session dispatched by any other means — a spike, a smoke test, someone's terminal — has
    no registry row and must not be counted as work the manager dispatched."""
    doc = session_docs([{"name": "ad-hoc", "sessionId": "s1", "state": "idle"}], {})["ad-hoc"]

    assert doc["managed"] is False


def test_session_docs_read_state_from_either_field_name():
    """`claude agents --json` has used both `state` and `status`; the dashboard already reads
    whichever is present and this must not disagree with it."""
    assert session_docs([{"name": "a", "sessionId": "s", "status": "waiting"}], {})["a"]["state"] == "waiting"


def test_session_docs_skip_an_entry_with_no_name():
    """A document id cannot be empty. An unnamed agent is dropped rather than written to a
    garbage key that nothing can address later."""
    assert session_docs([{"sessionId": "s1", "state": "idle"}], {}) == {}


from board_state import normalize_state


def test_normalize_state_maps_the_real_claude_agents_vocabulary_onto_the_pages_four_words():
    """`claude agents --json --all` verified live (2026-09-07): working/blocked/stopped are
    real values the page's running/waiting/idle/done vocabulary has never heard of."""
    assert normalize_state("working") == "running"
    assert normalize_state("blocked") == "waiting"
    assert normalize_state("stopped") == "done"


def test_normalize_state_leaves_the_pages_own_four_words_unchanged():
    for canonical in ("running", "idle", "waiting", "done"):
        assert normalize_state(canonical) == canonical


def test_normalize_state_marks_an_unrecognised_value_as_unknown_never_idle():
    """Folding an unrecognised state into "idle" IS the bug: a blocked worker would show the
    badge "Rảnh" (free) — the opposite of the truth. An unrecognised state must look
    unrecognised, not free."""
    assert normalize_state(None) == "unknown"
    assert normalize_state("some_future_cli_word") == "unknown"
    assert normalize_state(["not", "a", "string"]) == "unknown"


def test_session_docs_normalise_state_using_a_captured_real_claude_agents_payload():
    """Captured live from `claude agents --json --all` (2026-09-07) across 33 real sessions:
    blocked(4)/done(21)/stopped(5)/None(8) — zero running, zero idle, zero waiting. Every
    fixture before this one was hand-written to the plan's assumed vocabulary, which is
    exactly why nothing caught this until a human ran the real CLI."""
    agents = [
        {"name": "t-blocked", "sessionId": "s1", "state": "blocked"},
        {"name": "t-done", "sessionId": "s2", "state": "done"},
        {"name": "t-stopped", "sessionId": "s3", "state": "stopped"},
        {"name": "t-unstated", "sessionId": "s4"},  # neither `state` nor `status` present
    ]

    docs = session_docs(agents, {})

    assert docs["t-blocked"]["state"] == "waiting"
    assert docs["t-done"]["state"] == "done"
    assert docs["t-stopped"]["state"] == "done"
    assert docs["t-unstated"]["state"] == "unknown"


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


def test_escalation_docs_carry_evidence_as_a_plain_string_dict():
    """`evidence` is what tells a manager what is and is not affected — a card that drops it
    silently is no better off than before this field existed."""
    docs = escalation_docs(
        [
            {
                "id": "e7",
                "kind": "credentials",
                "question": "?",
                "evidence": {
                    "workers_dead": "c3a98127, aa9dc7d3",
                    "git_push": "khong anh huong - gh token rieng",
                },
            }
        ]
    )

    assert docs["e7"]["evidence"] == {
        "workers_dead": "c3a98127, aa9dc7d3",
        "git_push": "khong anh huong - gh token rieng",
    }


def test_escalation_docs_default_evidence_to_an_empty_dict_when_absent():
    docs = escalation_docs([{"id": "e8", "kind": "credentials", "question": "?"}])

    assert docs["e8"]["evidence"] == {}


def test_escalation_docs_stringify_a_non_string_evidence_value_instead_of_dropping_it():
    """`evidence` is worker-authored against a prose schema — the same tolerance classify() gives
    `deps_added`/`changed_files`, which land as lists in practice. A caller walking `evidence` as
    str->str must never receive a list, but the list is real information and must survive, not
    vanish."""
    docs = escalation_docs(
        [{"id": "e9", "kind": "credentials", "question": "?", "evidence": {"deps_added": ["left-pad", "chalk"]}}]
    )

    value = docs["e9"]["evidence"]["deps_added"]
    assert isinstance(value, str)
    assert "left-pad" in value and "chalk" in value


def test_escalation_docs_drop_evidence_entirely_when_it_is_not_even_a_dict():
    """Live drift: evidence has arrived as something other than a dict. A caller doing
    `for k, v in evidence.items()` must never receive something it cannot walk."""
    docs = escalation_docs([{"id": "e10", "kind": "credentials", "question": "?", "evidence": "not a dict"}])

    assert docs["e10"]["evidence"] == {}


def test_escalation_docs_carry_the_classifier_reason():
    docs = escalation_docs(
        [
            {
                "id": "e11",
                "kind": "blocked_on_credentials",
                "question": "?",
                "reason": "unknown kind 'blocked_on_credentials' — defaulting to a human",
            }
        ]
    )

    assert docs["e11"]["reason"] == "unknown kind 'blocked_on_credentials' — defaulting to a human"


def test_escalation_docs_default_reason_to_none_when_absent():
    docs = escalation_docs([{"id": "e12", "kind": "credentials", "question": "?"}])

    assert docs["e12"]["reason"] is None


def test_escalation_docs_coerce_a_non_string_reason_to_none():
    """`reason` is daemon-stamped, not user-typed, but the contract is still string-or-None —
    a future producer writing something else must not leak an unwalkable shape to the page."""
    docs = escalation_docs([{"id": "e13", "kind": "credentials", "question": "?", "reason": 42}])

    assert docs["e13"]["reason"] is None


def test_escalation_docs_carry_the_tier():
    docs = escalation_docs([{"id": "e14", "kind": "credentials", "question": "?", "tier": "tier3"}])

    assert docs["e14"]["tier"] == "tier3"


def test_escalation_docs_default_tier_to_none_when_absent():
    docs = escalation_docs([{"id": "e15", "kind": "credentials", "question": "?"}])

    assert docs["e15"]["tier"] is None


def test_escalation_docs_coerce_a_non_string_tier_to_none():
    docs = escalation_docs([{"id": "e16", "kind": "credentials", "question": "?", "tier": ["tier3"]}])

    assert docs["e16"]["tier"] is None


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


# ---------------------------------------------------------------------------
# Handoff tracking — a ticket the owner activated but no longer holds must stay on the board,
# marked as handed off, instead of vanishing the moment AssignedTo changes. See dashboard.py's
# _ado_assignee_clause()/_ado_identities()/_ado_identity_ref() for the ADO side of this.
# ---------------------------------------------------------------------------


def _ident(email, name=None):
    return {"email": email, "name": name}


def test_ado_assignee_clause_ors_assignedto_and_activatedby_over_one_shared_identity_source():
    """The filter clause must match EITHER field, and both sides must read the exact same
    identity list PWR_ADO_ASSIGNED_TO supplies — a second, separately typed copy of the emails
    is exactly the drift this ticket exists to prevent."""
    import os

    import dashboard

    original = os.environ.get("PWR_ADO_ASSIGNED_TO")
    os.environ["PWR_ADO_ASSIGNED_TO"] = "loc.tran@nois.vn, loc.tran@aiquinta.ai"
    try:
        clause = dashboard._ado_assignee_clause()
        identities = dashboard._ado_identities()
    finally:
        if original is None:
            os.environ.pop("PWR_ADO_ASSIGNED_TO", None)
        else:
            os.environ["PWR_ADO_ASSIGNED_TO"] = original

    assert identities == ["loc.tran@nois.vn", "loc.tran@aiquinta.ai"]
    assert "[System.AssignedTo]" in clause
    assert "[Microsoft.VSTS.Common.ActivatedBy]" in clause
    for email in identities:
        assert email in clause, f"{email!r} (from _ado_identities()) missing from the clause"


def test_ado_backlog_wiql_selects_assignedto_and_activatedby_for_the_classifier():
    """ticket_docs()'s handoff classifier reads these two fields off each raw ticket; dropping
    either column from the SELECT would silently blank the classification with no error."""
    import dashboard

    wiql = dashboard._ado_backlog_wiql()
    assert "[System.AssignedTo]" in wiql
    assert "[Microsoft.VSTS.Common.ActivatedBy]" in wiql


def test_ticket_docs_a_ticket_still_assigned_to_the_owner_is_never_handed_off():
    """AssignedTo wins: whoever also activated it, the owner still holds this ticket."""
    docs = ticket_docs(
        [
            {
                "id": "1", "title": "t", "state": "Active", "sprint": "S", "url": "u",
                "assigned_to": _ident("me@x.com"), "activated_by": _ident("someone-else@x.com"),
            }
        ],
        {},
        owners=["me@x.com"],
    )
    assert docs["1"]["handed_off"] is False
    assert docs["1"]["handed_off_to"] is None


def test_ticket_docs_activated_by_the_owner_but_assigned_elsewhere_is_handed_off_with_the_holders_name():
    docs = ticket_docs(
        [
            {
                "id": "1", "title": "t", "state": "Resolved", "sprint": "S", "url": "u",
                "assigned_to": _ident("qc@x.com", "QC Person"), "activated_by": _ident("me@x.com"),
            }
        ],
        {},
        owners=["me@x.com"],
    )
    assert docs["1"]["handed_off"] is True
    assert docs["1"]["handed_off_to"] == "QC Person"


def test_ticket_docs_a_ticket_matching_both_fields_produces_exactly_one_undupped_document():
    """The overlap case — AssignedTo and ActivatedBy both the owner — must land only in the
    "mine" branch, never also in the handed-off one, and there is exactly one doc per ticket id
    regardless of how many of its fields matched the owner."""
    docs = ticket_docs(
        [
            {
                "id": "1", "title": "t", "state": "Active", "sprint": "S", "url": "u",
                "assigned_to": _ident("me@x.com"), "activated_by": _ident("me@x.com"),
            }
        ],
        {},
        owners=["me@x.com"],
    )
    assert list(docs) == ["1"]
    assert docs["1"]["handed_off"] is False


def test_ticket_docs_an_empty_activated_by_never_falsely_matches_the_owner():
    """A ticket never moved to Active has no ActivatedBy at all — dashboard._ado_identity_ref()
    reports that as {"email": None, ...}, and None must never compare equal to a real identity."""
    docs = ticket_docs(
        [
            {
                "id": "1", "title": "t", "state": "New", "sprint": "S", "url": "u",
                "assigned_to": _ident("someone-else@x.com"), "activated_by": _ident(None),
            }
        ],
        {},
        owners=["me@x.com"],
    )
    assert docs["1"]["handed_off"] is False
    assert docs["1"]["handed_off_to"] is None


def test_ticket_docs_with_no_owners_configured_reads_every_ticket_as_the_owners_own():
    """PWR_ADO_ASSIGNED_TO unset means the classifier has no identity to compare against — the
    behaviour before handoff tracking existed, not a false positive."""
    docs = ticket_docs(
        [
            {
                "id": "1", "title": "t", "state": "Resolved", "sprint": "S", "url": "u",
                "assigned_to": _ident("someone-else@x.com"), "activated_by": _ident("me@x.com"),
            }
        ],
        {},
    )
    assert docs["1"]["handed_off"] is False


def test_board_handed_off_group_shows_a_count_and_is_collapsed_by_default():
    """collapsedGroup(label, rows, openHint) collapses by default when openHint is omitted.
    Pinning the array literal's closing `]` straight to collapsedGroup's own closing `)` proves
    there is no third argument slipped in — a stray truthy openHint would open this group on
    every load, defeating the whole point of a group for work that already left the owner's
    hands."""
    script = _board_html_script()
    pattern = re.compile(
        r"collapsedGroup\(\s*handedOff\.length\s*\+[^,]*,\s*"
        r"\[\s*ticketTable\(handedOff,\s*\{\s*showHolder:\s*true\s*\}\)\s*\]\s*\)",
        re.S,
    )
    assert pattern.search(script), "handed-off collapsedGroup() must show a count and take no third (openHint) argument"


def test_board_reads_handed_off_fields_under_the_same_names_ticket_docs_writes():
    """A typo in either place (board_state.py's dict keys vs board.html's `t.xxx` reads) fails
    silently — the field just reads undefined and the group is always empty."""
    script = _board_html_script()
    assert "t.handed_off_to" in script
    assert re.search(r"\bt\.handed_off\b(?!_to)", script), "board.html never reads t.handed_off"


from board_state import prs_by_ticket


def test_prs_by_ticket_maps_a_single_ab_ref_in_the_title():
    prs = [
        {
            "number": 720,
            "title": "fix: derive pack fact and skill file names from list position (AB#5061)",
            "url": "https://github.com/o/r/pull/720",
            "state": "MERGED",
            "isDraft": False,
        }
    ]

    assert prs_by_ticket(prs) == {"5061": {"number": 720, "state": "MERGED", "url": "https://github.com/o/r/pull/720"}}


def test_prs_by_ticket_maps_a_title_naming_two_tickets_to_both():
    """"(AB#8196, AB#8197)" is a real title shape — one PR can close more than one ticket."""
    prs = [
        {
            "number": 100,
            "title": "fix: shared cleanup (AB#8196, AB#8197)",
            "url": "https://github.com/o/r/pull/100",
            "state": "OPEN",
            "isDraft": False,
        }
    ]

    docs = prs_by_ticket(prs)

    assert set(docs) == {"8196", "8197"}
    assert docs["8196"] == {"number": 100, "state": "OPEN", "url": "https://github.com/o/r/pull/100"}
    assert docs["8197"] == docs["8196"]


def test_prs_by_ticket_ignores_a_title_with_no_ab_ref():
    prs = [{"number": 5, "title": "chore: tidy imports", "url": "u", "state": "OPEN", "isDraft": False}]

    assert prs_by_ticket(prs) == {}


def test_prs_by_ticket_reflects_a_merged_pr_state():
    prs = [{"number": 720, "title": "fix: x (AB#5061)", "url": "u", "state": "MERGED", "isDraft": False}]

    assert prs_by_ticket(prs)["5061"]["state"] == "MERGED"


def test_prs_by_ticket_prefers_the_open_pr_over_a_merged_one_for_the_same_ticket():
    """An OPEN PR still needs a human decision; a MERGED one is already done. The one still
    asking for attention is what a reviewer scanning the board cares about most."""
    prs = [
        {"number": 10, "title": "fix: old attempt (AB#9000)", "url": "u10", "state": "MERGED", "isDraft": False},
        {"number": 20, "title": "fix: current attempt (AB#9000)", "url": "u20", "state": "OPEN", "isDraft": False},
    ]

    assert prs_by_ticket(prs)["9000"]["number"] == 20


def test_prs_by_ticket_breaks_a_same_state_tie_with_the_higher_pr_number():
    prs = [
        {"number": 20, "title": "fix: first (AB#9001)", "url": "u20", "state": "OPEN", "isDraft": False},
        {"number": 21, "title": "fix: second (AB#9001)", "url": "u21", "state": "OPEN", "isDraft": False},
    ]

    assert prs_by_ticket(prs)["9001"]["number"] == 21


def test_prs_by_ticket_prefers_a_non_draft_pr_over_a_draft_one_in_the_same_state():
    prs = [
        {"number": 30, "title": "fix: draft (AB#9002)", "url": "u30", "state": "OPEN", "isDraft": True},
        {"number": 31, "title": "fix: ready (AB#9002)", "url": "u31", "state": "OPEN", "isDraft": False},
    ]

    assert prs_by_ticket(prs)["9002"]["number"] == 31


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


def test_meta_status_carries_the_default_sprint_field():
    doc = meta_status(now=1000.0, default_sprint="Sprint 58")
    assert doc["default_sprint"] == "Sprint 58"


def test_meta_status_default_sprint_is_null_by_default():
    """Every call site written before this parameter existed must keep working unchanged."""
    doc = meta_status(now=1000.0)
    assert doc["default_sprint"] is None


# --- current_sprint_name / resolve_default_sprint --------------------------------------------
#
# board.html cannot know which ADO sprint is "today" — the boundary lives in ADO, not in the
# sandbox's Date object — so board_state.py must compute it and stamp it onto meta/status for
# the page to read as the backlog filter's starting value.

from datetime import UTC, datetime

from board_state import current_sprint_name, resolve_default_sprint


def _ts(year, month, day):
    return datetime(year, month, day, 12, tzinfo=UTC).timestamp()


# Real dates from this project's own iteration list (verified live 2026-09-08): Sprint 57 runs
# 08-31..09-04, Sprint 58 runs 09-07..09-11 — a real two-day gap (09-05/09-06) sits between them.
ITERATIONS = [
    {"name": "Sprint 57", "start": "2026-08-31T00:00:00Z", "finish": "2026-09-04T00:00:00Z"},
    {"name": "Sprint 58", "start": "2026-09-07T00:00:00Z", "finish": "2026-09-11T00:00:00Z"},
    {"name": "Sprint 59", "start": "2026-09-14T00:00:00Z", "finish": "2026-09-18T00:00:00Z"},
]


def test_current_sprint_name_picks_the_iteration_whose_range_contains_today():
    assert current_sprint_name(ITERATIONS, _ts(2026, 9, 8)) == "Sprint 58"


def test_current_sprint_name_is_null_in_the_gap_between_two_sprints():
    assert current_sprint_name(ITERATIONS, _ts(2026, 9, 5)) is None


def test_current_sprint_name_is_null_when_the_iteration_has_no_dates():
    undated = [{"name": "Sprint 58", "start": None, "finish": None}]
    assert current_sprint_name(undated, _ts(2026, 9, 8)) is None


def test_current_sprint_name_is_null_on_an_empty_iteration_list():
    """`az` failed (see collect()'s degrade path below) — no iterations to check at all."""
    assert current_sprint_name([], _ts(2026, 9, 8)) is None


def test_resolve_default_sprint_matches_leaf_shape_a_ticket_sprint_already_carries():
    """`_shape_ado_ticket()` already cuts a ticket's sprint to the leaf name ("Sprint 58", not
    "AgentIQ\\Sprint 58") — the stamped default must compare equal to that, not to a path."""
    tickets = {"1": {"sprint": "Sprint 58"}}
    assert resolve_default_sprint(ITERATIONS, tickets, _ts(2026, 9, 8)) == "Sprint 58"


def test_resolve_default_sprint_falls_back_to_all_when_current_sprint_has_no_tickets():
    """An empty table under an auto-picked filter reads exactly like a broken board — falling
    back to null ("tất cả") is safer than a technically-correct empty view."""
    tickets = {"1": {"sprint": "Sprint 57"}}  # nothing in Sprint 58
    assert resolve_default_sprint(ITERATIONS, tickets, _ts(2026, 9, 8)) is None


def test_resolve_default_sprint_is_null_when_there_is_no_current_sprint():
    tickets = {"1": {"sprint": "Sprint 57"}}
    assert resolve_default_sprint(ITERATIONS, tickets, _ts(2026, 9, 5)) is None


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


def test_build_writes_stamps_default_sprint_from_iterations_and_tickets():
    writes = build_writes(
        agents=[],
        registry={},
        escalations=[],
        tickets=[{"id": "1", "title": "t", "state": "New", "sprint": "Sprint 58", "url": "u"}],
        pr_by_ticket={},
        now=_ts(2026, 9, 8),
        iterations=ITERATIONS,
    )

    assert writes[-1]["data"]["default_sprint"] == "Sprint 58"


def test_build_writes_default_sprint_is_null_without_an_iterations_reader():
    """Every call site written before this parameter existed must keep working unchanged."""
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1000.0)

    assert writes[-1]["data"]["default_sprint"] is None


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


def test_collect_uses_injected_readers_and_never_touches_the_network():
    """The readers are injected so the collection step is testable without `az`, `gh` or a live
    session — the same shape `test_dashboard.py` already uses for `ask`."""
    from board_state import collect

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "waiting"}],
        read_registry=lambda: {"t1": {"branch": "feature/x", "short_id": "ab12"}},
        read_escalations=lambda: [{"id": "e1", "kind": "credentials", "question": "?"}],
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "S1", "url": "u"}],
        read_prs=dict,
        now=lambda: 500.0,
    )

    sessions = [w for w in writes if w["collection"] == "sessions"]
    assert sessions[0]["data"]["state"] == "waiting"
    assert writes[-1]["data"]["written_at"] == 500.0


def test_collect_degrades_to_an_empty_source_when_one_reader_fails():
    """`az` not being authenticated must not blank the whole board — every other source is
    still worth publishing, and meta/status will show the ADO age going stale."""
    from board_state import collect

    def boom():
        raise OSError("az not logged in")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "idle"}],
        read_registry=dict,
        read_escalations=list,
        read_tickets=boom,
        read_prs=dict,
        now=lambda: 500.0,
    )

    assert [w for w in writes if w["collection"] == "sessions"]
    assert not [w for w in writes if w["collection"] == "tickets"]
    assert writes[-1]["data"]["last_ado_sweep"] is None


from board_state import collect

# --- Coverage added beyond the brief -----------------------------------------------------
#
# The brief's two tests above wire non-trivial data into all five readers but only ever assert
# two facts: the `state` field session_docs derives from `read_agents`, and `written_at` from
# `now`. registry/escalations/tickets/prs data sits in every fixture unchecked; only
# `read_tickets`'s failure path is exercised, never the other four readers'; and the plan's own
# most-important rule — an empty-but-successful ticket sweep must still stamp `last_ado_sweep` —
# is asserted by neither test (test 1's ticket list is non-empty, test 2's reader raises instead
# of returning empty). Closing those gaps below, one behaviour per test so a mutation names
# exactly which reader or which path broke.


def test_collect_stamps_last_ado_sweep_normally_when_the_sweep_finds_nothing():
    """The plan's central freshness rule: a ticket read that SUCCEEDS with an empty list is a
    real sweep that found nothing, not a failed one — `last_ado_sweep` must stamp the same as
    any other successful pass. `tickets or []` normalises the value for build_writes but must
    not also flip `ado_swept_at` to None; only `_safe` catching an exception may do that."""
    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
    )

    assert not [w for w in writes if w["collection"] == "tickets"]
    assert writes[-1]["data"]["last_ado_sweep"] == 500.0


def test_collect_carries_registry_fields_into_the_session_doc():
    """Proves `_safe(read_registry, {})`'s SUCCESS value — not just its `{}` fallback — reaches
    `session_docs`. The brief's own test 1 supplies this same registry fixture and never checks
    it; a `collect` that silently dropped the registry reader's return value would still pass
    every brief assertion because they only ever look at `state`."""
    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "running"}],
        read_registry=lambda: {"t1": {"branch": "feature/x", "short_id": "ab12"}},
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
    )

    sessions = [w for w in writes if w["collection"] == "sessions"]
    assert sessions[0]["data"]["branch"] == "feature/x"
    assert sessions[0]["data"]["short_id"] == "ab12"


def test_collect_writes_escalations_from_the_injected_reader():
    """No brief test ever inspects the escalations collection — a `collect` that always passed
    `escalations=[]` to `build_writes`, ignoring `read_escalations` entirely, would still pass
    both brief tests."""
    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=lambda: [{"id": "e1", "kind": "credentials", "question": "?"}],
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
    )

    escalations = [w for w in writes if w["collection"] == "escalations"]
    assert escalations[0]["doc_id"] == "e1"
    assert escalations[0]["data"]["severity"] == "P0"


def test_collect_writes_tickets_and_attaches_pr_data_from_the_injected_readers():
    """Neither brief test inspects the tickets collection (test 1's ticket list is non-empty but
    unchecked; test 2's is empty because the reader raises). Also proves `_safe(read_prs, {})`'s
    success value reaches `ticket_docs` — the brief's own `read_prs=lambda: {}` fixture is too
    trivial to tell a dropped argument from a working one."""
    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=lambda: [{"id": "8311", "title": "t", "state": "Active", "sprint": "S", "url": "u"}],
        read_prs=lambda: {"8311": {"number": 42, "state": "OPEN", "url": "https://example/42"}},
        now=lambda: 500.0,
    )

    tickets = [w for w in writes if w["collection"] == "tickets"]
    assert tickets[0]["doc_id"] == "8311"
    assert tickets[0]["data"]["pr"] == {"number": 42, "state": "OPEN", "url": "https://example/42"}


def test_collect_degrades_agents_to_empty_without_blanking_other_sources():
    """The brief only ever makes `read_tickets` fail. A broken `claude agents --json` call must
    not blank escalations/tickets too — and raising RuntimeError (not one of the subprocess-
    shaped exceptions dashboard.py narrowly catches) proves `_safe` catches broadly rather than
    only OSError/SubprocessError/JSONDecodeError."""

    def boom():
        raise RuntimeError("claude cli not found")

    writes = collect(
        read_agents=boom,
        read_registry=dict,
        read_escalations=lambda: [{"id": "e1", "kind": "credentials", "question": "?"}],
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "S", "url": "u"}],
        read_prs=dict,
        now=lambda: 500.0,
    )

    assert not [w for w in writes if w["collection"] == "sessions"]
    assert [w for w in writes if w["collection"] == "escalations"]
    assert [w for w in writes if w["collection"] == "tickets"]


def test_collect_degrades_registry_to_empty_without_blanking_sessions():
    """Agent-first, not registry-first — the same rule `session_docs` itself enforces: a broken
    registry read must not blank sessions, only strip the enrichment it would have added."""

    def boom():
        raise KeyError("registry file missing a key")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "running"}],
        read_registry=boom,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
    )

    sessions = [w for w in writes if w["collection"] == "sessions"]
    assert sessions[0]["data"]["state"] == "running"
    assert sessions[0]["data"]["branch"] is None


def test_collect_degrades_escalations_to_empty_without_blanking_other_sources():
    def boom():
        raise ValueError("queue file corrupt")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "running"}],
        read_registry=dict,
        read_escalations=boom,
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "S", "url": "u"}],
        read_prs=dict,
        now=lambda: 500.0,
    )

    assert not [w for w in writes if w["collection"] == "escalations"]
    assert [w for w in writes if w["collection"] == "sessions"]
    assert [w for w in writes if w["collection"] == "tickets"]


def test_collect_degrades_prs_to_empty_without_blanking_tickets():
    """A broken PR lookup must not blank the tickets collection — it must only leave `pr` null
    on every ticket, same as when no PR references it at all."""

    def boom():
        raise TypeError("gh output not JSON")

    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "S", "url": "u"}],
        read_prs=boom,
        now=lambda: 500.0,
    )

    tickets = [w for w in writes if w["collection"] == "tickets"]
    assert tickets[0]["data"]["pr"] is None


def test_collect_calls_now_exactly_once_so_every_stamp_in_one_call_agrees():
    """`stamp = now()` is captured once and reused for `written_at`, `last_session_scan` AND
    `last_ado_sweep`. A `now` called more than once would be invisible to every other test here,
    since all of them pass a constant lambda that returns the same value regardless of call
    count."""
    calls = []

    def counting_now():
        calls.append(len(calls))
        return 100.0 + len(calls)

    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=counting_now,
    )

    assert len(calls) == 1
    meta = writes[-1]["data"]
    assert meta["written_at"] == meta["last_session_scan"] == meta["last_ado_sweep"]


def test_collect_lets_a_broken_clock_propagate_instead_of_writing_a_bogus_timestamp():
    """Unlike the five source readers, `now` is not wrapped in `_safe` — every document's
    freshness depends on it, so a broken clock must fail loudly rather than silently write a
    fabricated timestamp the board would present as real."""

    def boom():
        raise RuntimeError("clock broken")

    threw = False
    try:
        collect(
            read_agents=list,
            read_registry=dict,
            read_escalations=list,
            read_tickets=list,
            read_prs=dict,
            now=boom,
        )
    except RuntimeError:
        threw = True
    assert threw


from board_state import _registry_path, _safe


def test_safe_prints_which_reader_failed_and_why_to_stderr(capsys):
    """This exact broad catch once swallowed a FileNotFoundError in the registry join for a full
    day — a broken reader and a working one looked identical from the outside, because nothing
    said which one gave up. `_safe` must name the reader and the exception on stderr every time
    it falls back."""

    def boom():
        raise FileNotFoundError("no such file: .parallel-registry.json")

    result = _safe(boom, {"fallback": True}, "registry")

    assert result == {"fallback": True}
    err = capsys.readouterr().err
    assert "registry" in err
    assert "no such file" in err


def test_safe_prints_nothing_when_the_reader_succeeds(capsys):
    result = _safe(lambda: 42, None, "agents")

    assert result == 42
    assert capsys.readouterr().err == ""


def test_registry_path_joins_the_repo_root_with_the_known_registry_location():
    """The registry join went silently dead for a full day because it resolved against the
    wrong repo root, not because this join itself was wrong — but pin the join's own shape too,
    now that it is a named, reusable seam instead of an inline string buried in a closure."""
    assert _registry_path("/repo") == "/repo/.claude/worktrees/.parallel-registry.json"


def test_collect_wires_the_manager_reader_into_meta_status():
    """`collect()` used to have no `manager` parameter at all, so `build_writes` was always
    called without one and meta/status.manager_session_id stayed null forever — even while a
    real manager session was running. Prove the injected reader's value reaches the meta doc."""
    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
        read_manager=lambda: {"session_id": "m1", "started_at": 100.0},
    )

    meta = writes[-1]["data"]
    assert meta["manager_session_id"] == "m1"
    assert meta["manager_started_at"] == 100.0


def test_collect_defaults_manager_fields_to_null_when_no_reader_is_given():
    """Every `collect()` call in this file predating this parameter omits `read_manager` — the
    default must keep producing the same null fields those tests were already written against."""
    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
    )

    meta = writes[-1]["data"]
    assert meta["manager_session_id"] is None
    assert meta["manager_started_at"] is None


def test_collect_degrades_manager_to_empty_without_blanking_other_sources():
    """A broken manager-session read (no session has ever run yet, or the state file is
    corrupt) must not blank the sessions collection — only leave the manager chip null, same as
    when no manager reader is given at all."""

    def boom():
        raise OSError("manager-session.json missing")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "running"}],
        read_registry=dict,
        read_escalations=list,
        read_tickets=list,
        read_prs=dict,
        now=lambda: 500.0,
        read_manager=boom,
    )

    assert [w for w in writes if w["collection"] == "sessions"]
    assert writes[-1]["data"]["manager_session_id"] is None


def _board_html_script():
    """The board's freshness/alarm logic lives only in board.html's inline <script> — there is
    no Python model of it to import, so these tests read the script text directly, the same way
    test_dashboard.py checks dashboard.html."""
    import pathlib

    html = (pathlib.Path(__file__).parent / "board.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "board.html has no <script> block — did the file move?"
    return "\n".join(blocks)


def test_ado_cadence_matches_the_real_15_minute_cron():
    """The cron that fills this board's db runs every 15 min (see board_state.py's sweep loop).
    board.html previously hard-coded 30 min here, so every staleness check below was computed
    from a baseline twice as long as reality."""
    script = _board_html_script()
    m = re.search(r"const ADO_CADENCE_S\s*=\s*([0-9]+)\s*\*\s*([0-9]+)", script)
    assert m, "ADO_CADENCE_S declaration not found"
    assert int(m.group(1)) * int(m.group(2)) == 15 * 60


def test_ado_stale_threshold_is_30_minutes_not_90():
    """A dead cron must be flagged after 30 min, not the old 90-minute (30min cadence x3)
    threshold this page shipped with — 90 minutes of silently-stale data on a board people
    trust at a glance is the actual bug being fixed here."""
    script = _board_html_script()
    cadence_m = re.search(r"const ADO_CADENCE_S\s*=\s*([0-9]+)\s*\*\s*([0-9]+)", script)
    multiplier_m = re.search(r"const ADO_STALE_MULTIPLIER\s*=\s*([0-9]+)", script)
    assert cadence_m and multiplier_m, "ADO cadence/multiplier constants not found"
    cadence_s = int(cadence_m.group(1)) * int(cadence_m.group(2))
    threshold_s = cadence_s * int(multiplier_m.group(1))
    assert threshold_s == 30 * 60


def test_stale_banner_markup_starts_hidden():
    """The banner must exist in the initial markup and be hidden until JS decides ADO data is
    stale — otherwise it either never appears (missing element) or flashes on every load
    (missing `hidden`)."""
    import pathlib

    html = (pathlib.Path(__file__).parent / "board.html").read_text(encoding="utf-8")
    assert re.search(r'id="stale-banner"[^>]*\bhidden\b', html), (
        "#stale-banner must be present and hidden by default in the markup"
    )


def test_stale_banner_uses_the_bad_tone_not_the_warn_tone():
    """The banner has to read as an alarm, not the same soft warning tone the small freshness
    chip already uses — otherwise it's just a second, bigger version of the thing people were
    already ignoring."""
    import pathlib

    html = (pathlib.Path(__file__).parent / "board.html").read_text(encoding="utf-8")
    css_m = re.search(r"\.stale-banner\s*\{([^}]*)\}", html)
    assert css_m, ".stale-banner CSS rule not found"
    css = css_m.group(1)
    assert "--bad-ink" in css or "--bad-soft" in css
    assert "--warn-ink" not in css and "--warn-soft" not in css


def test_render_stale_banner_hides_when_ado_is_fresh():
    """The banner is a persistent DOM node re-rendered on the existing 30s tick, not a one-shot
    alert — it must actively hide itself once data is fresh again, not just skip showing."""
    script = _board_html_script()
    assert "renderStaleBanner(adoInfo)" in script
    assert "banner.hidden = true" in script
    assert "banner.hidden = false" in script


def test_freshness_info_treats_a_never_swept_source_as_stale():
    """last_ado_sweep can be null (the sweep has literally never run once). That must still
    read as stale and drive the banner — a null timestamp is the clearest possible sign the
    cron never started, not a reason to stay quiet."""
    script = _board_html_script()
    m = re.search(
        r'if \(ts == null\) return \{ text: "[^"]*", stale: (true|false), never: (true|false) \};',
        script,
    )
    assert m, "freshnessInfo's null-timestamp branch not found"
    assert m.group(1) == "true", "a never-swept source must be reported as stale"
    assert m.group(2) == "true"


# ---------------------------------------------------------------------------
# "Làm mới" button — a manual trigger of the SAME redraw path the 30s tick uses.
#
# The page is a read-only artifact in a sandbox: it cannot reach the filesystem, cannot run `az`,
# and cannot call back to the machine running the pump timer. So this button re-reads the artifact
# db and nothing else. Every test below exists to keep that honest — a button that looked like it
# fetched fresh ADO numbers would be worse than no button at all.
# ---------------------------------------------------------------------------


def _board_html():
    import pathlib

    return (pathlib.Path(__file__).parent / "board.html").read_text(encoding="utf-8")


def test_board_markup_has_a_refresh_container():
    """The strip is a persistent element the render path fills, the same way #summary and
    #freshness are — not a node built once at boot that a later render would orphan."""
    assert re.search(r'id="refresh"', _board_html()), "#refresh container not found in the markup"


def test_refresh_button_is_labelled_in_vietnamese():
    script = _board_html_script()
    assert "Làm mới" in script, "the refresh button must be labelled 'Làm mới'"


def test_refresh_button_click_runs_the_refresh_path():
    """The button must be wired to refresh(), not to a private copy of the read logic."""
    script = _board_html_script()
    assert re.search(r'onclick:\s*refresh\b', script), "the button's onclick must call refresh()"


def test_refresh_redraws_through_the_existing_render_function():
    """`render()` is the one redraw path — escalations, sessions, tickets, assignments, summary
    and freshness all come from it. A refresh that repainted only some sections would leave the
    board half-updated with no sign of it."""
    script = _board_html_script()
    body = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "refresh() not found"
    assert "render()" in body.group(1), "refresh() must redraw through render()"


def test_refresh_re_reads_every_source_the_live_listeners_read():
    """One table of sources drives both the boot listeners and the button, so a collection can
    never be wired into one path and forgotten in the other — which is exactly how a 'refresh'
    silently stops refreshing one section."""
    script = _board_html_script()
    assert re.search(r"const SOURCES\s*=", script), "SOURCES table not found"
    table = re.search(r"const SOURCES\s*=\s*\[(.*?)\n\];", script, re.S)
    assert table, "SOURCES table body not found"
    for key in ("sessions", "escalations", "tickets", "meta", "assignments"):
        assert f'"{key}"' in table.group(1), f"{key} missing from the SOURCES table"
    assert ".get()" in script, "refresh() must actually re-read the db with get()"


def test_refresh_shows_the_real_age_of_the_data_beside_the_button():
    """The whole point of the label: the button re-reads what the pump timer already wrote, so
    the data keeps whatever age it had. Showing a wall-clock stamp AND a relative age means a
    click that changes nothing visibly leaves the age visibly unchanged too."""
    script = _board_html_script()
    assert "dữ liệu tính đến" in script, "the data-age label text not found"
    assert "written_at" in script, "the age label must come from meta.written_at"
    assert "toLocaleTimeString" in script, "the label must include a wall-clock time"


def test_refresh_label_reuses_the_existing_freshness_helpers():
    """freshnessInfo()/ageText() already phrase ages for this page. A second implementation would
    drift from the chips right beside it."""
    script = _board_html_script()
    body = re.search(r"function renderRefresh\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "renderRefresh() not found"
    assert "freshnessInfo(" in body.group(1) or "ageText(" in body.group(1)


def test_refresh_button_shows_that_it_is_reading():
    """A click with no visible response reads as a dead button and invites a second click."""
    script = _board_html_script()
    assert "Đang tải lại…" in script, "no busy label on the refresh button"
    assert re.search(r"refresh\.busy\s*=\s*true", script), "busy flag never set"
    assert re.search(r"refresh\.busy\s*=\s*false", script), "busy flag never cleared"


def test_refresh_says_so_when_the_read_fails():
    """A failed re-read must not leave the previous numbers sitting there looking freshly
    confirmed — that is the one way this button could actively mislead."""
    script = _board_html_script()
    body = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "refresh() not found"
    assert "catch" in body.group(1), "refresh() must handle a failed read"
    assert re.search(r"refresh\.error\s*=", body.group(1)), "refresh() must record the error"
    assert "Không đọc lại được dữ liệu" in script, "no Vietnamese error text for a failed refresh"


def test_refresh_clears_a_dead_sources_error_flag_on_success():
    """A snapshot listener that errors is terminal — it never fires again. The manual re-read is
    the only way back, so it has to clear view.errors or the board keeps showing 'nguồn dữ liệu bị
    lỗi' forever after a re-read that actually worked."""
    script = _board_html_script()
    body = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "refresh() not found"
    assert re.search(r"view\.errors\[[^\]]+\]\s*=\s*false", body.group(1)), (
        "a successful re-read must clear the per-source error flag"
    )


def test_refresh_button_never_claims_to_fetch_fresh_ado_data():
    """The sandbox cannot reach ADO. The page has to say what the button really does, in the UI
    and not only in a tooltip a reader may never hover."""
    script = _board_html_script()
    assert "không gọi" in script and "ADO" in script, (
        "the page must state that the button does not call out to ADO"
    )


def test_board_adds_no_second_timer():
    """The page already redraws on one 30s setInterval. The button is a manual trigger of that
    same path — a second timer would double the redraw rate and race the first."""
    script = _board_html_script()
    assert len(re.findall(r"setInterval\s*\(", script)) == 1, "board.html must have exactly one setInterval"


def test_board_html_uses_no_dom_apis_the_artifact_sandbox_forbids():
    """Every one of these fails silently in the artifact sandbox rather than throwing, so a
    single slip would blank a section with no error anywhere. The page builds DOM through h()."""
    html = _board_html()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in html, f"board.html must not use {banned}"


def test_board_html_loads_no_external_script_and_only_the_allowed_font_stylesheet():
    """The artifact CSP blocks external scripts outright. Stylesheets are blocked too, with one
    exception it explicitly allows and this page already depends on: fonts.googleapis.com. So the
    check is 'no external script at all, and no stylesheet from anywhere else' — banning the font
    link outright would break a working page for a rule the sandbox does not actually have."""
    html = _board_html()
    assert not re.search(r"<script[^>]+\bsrc\s*=", html, re.I), "board.html must not load an external script"
    for href in re.findall(r'<link[^>]+rel=["\']?stylesheet["\']?[^>]*>', html, re.I):
        url = re.search(r'href="([^"]*)"', href)
        assert url and url.group(1).startswith("https://fonts.googleapis.com/"), (
            f"only fonts.googleapis.com stylesheets are allowed, got: {href}"
        )


# ---------------------------------------------------------------------------
# Assignments — the manager's own ledger, which has never reached the board at all.
# ---------------------------------------------------------------------------


def _assignment(**over):
    rec = {
        "id": "a1",
        "ts": 100.0,
        "title": "Dựng lại pipeline",
        "priority": "P0",
        "deadline": "2030-01-01",
        "ado_refs": ["4321"],
        "status": "in_progress",
        "plan": [],
        "note": "ghi chú",
    }
    rec.update(over)
    return rec


def test_assignment_docs_key_on_id_and_carry_the_ledger_fields():
    from board_state import assignment_docs

    docs = assignment_docs([_assignment()], now=200.0)
    assert set(docs) == {"a1"}
    doc = docs["a1"]
    assert doc["title"] == "Dựng lại pipeline"
    assert doc["priority"] == "P0"
    assert doc["status"] == "in_progress"
    assert doc["deadline"] == "2030-01-01"
    assert doc["ado_refs"] == ["4321"]
    assert doc["note"] == "ghi chú"
    assert doc["ts"] == 100.0


def test_assignment_docs_skip_a_record_with_no_id():
    """Same rule as every other doc builder here: a document id cannot be empty."""
    from board_state import assignment_docs

    assert assignment_docs([_assignment(id=None)], now=200.0) == {}


def test_assignment_docs_let_the_latest_record_for_an_id_win():
    """The ledger is append-only: the same id appears once per update, oldest first. The board
    must show the newest, not the first one it happened to read."""
    from board_state import assignment_docs

    docs = assignment_docs(
        [_assignment(status="assigned", note="cũ"), _assignment(status="done", note="mới")],
        now=200.0,
    )
    assert docs["a1"]["status"] == "done"
    assert docs["a1"]["note"] == "mới"


def test_assignment_docs_keep_a_done_assignment_instead_of_dropping_it():
    """Mirroring only open assignments is how the escalation mirror froze records open forever
    (see main()'s current_state comment). A finished assignment must still be published, marked
    done — the board shows what happened, not only what is outstanding."""
    from board_state import assignment_docs

    docs = assignment_docs([_assignment(status="done"), _assignment(id="a2", status="cancelled")], now=200.0)
    assert set(docs) == {"a1", "a2"}
    assert docs["a1"]["status"] == "done"
    assert docs["a2"]["status"] == "cancelled"


def test_assignment_docs_compute_progress_from_the_plan_steps():
    from board_state import assignment_docs

    plan = [
        {"step": "một", "state": "done"},
        {"step": "hai", "state": "done"},
        {"step": "ba", "state": "todo"},
        {"step": "bốn", "state": "doing"},
    ]
    assert assignment_docs([_assignment(plan=plan)], now=200.0)["a1"]["progress"] == 0.5


def test_assignment_docs_ignore_a_progress_field_someone_stored_in_the_ledger():
    """Derived data is computed here, never read back from the record. A stale `progress` written
    into the ledger by hand must lose to the plan steps, which are the truth."""
    from board_state import assignment_docs

    rec = _assignment(plan=[{"step": "một", "state": "done"}], progress=0.0, at_risk=False)
    doc = assignment_docs([rec], now=200.0)["a1"]
    assert doc["progress"] == 1.0


def test_assignment_docs_report_no_progress_rather_than_zero_when_there_is_no_plan():
    """None, not 0.0 — an empty bar reads as 'nothing done yet', which is a different (and
    wrong) claim from 'nobody has broken this down yet'."""
    from board_state import assignment_docs

    assert assignment_docs([_assignment(plan=[])], now=200.0)["a1"]["progress"] is None


def test_assignment_docs_compute_at_risk_from_a_passed_step_eta():
    """at_risk is not a ledger field. An unfinished step whose ETA has gone by puts the whole
    assignment at risk even when its own deadline is still far off."""
    from board_state import assignment_docs

    now = 1893456000.0  # 2030-01-01
    rec = _assignment(deadline="2035-01-01", plan=[{"step": "một", "state": "todo", "eta": "2020-01-01"}])
    assert assignment_docs([rec], now=now)["a1"]["at_risk"] is True


def test_assignment_docs_ignore_an_at_risk_field_stored_in_the_ledger():
    from board_state import assignment_docs

    now = 1893456000.0
    rec = _assignment(deadline="2020-01-01", at_risk=False)
    assert assignment_docs([rec], now=now)["a1"]["at_risk"] is True


def test_assignment_docs_never_flag_a_finished_assignment_as_at_risk():
    """A deadline that passed after the work was already done is history, not an alarm."""
    from board_state import assignment_docs

    now = 1893456000.0
    for status in ("done", "cancelled"):
        rec = _assignment(status=status, deadline="2020-01-01")
        assert assignment_docs([rec], now=now)["a1"]["at_risk"] is False


def test_assignment_docs_flag_an_unplanned_assignment_as_stalled():
    """Open, never broken into steps, and sitting that way for over an hour: nobody has started.
    A distinct signal from at_risk, which needs a date to be late against."""
    from board_state import assignment_docs

    doc = assignment_docs([_assignment(ts=0.0, plan=[])], now=100000.0)["a1"]
    assert doc["stalled"] is True


def test_assignment_docs_tolerate_a_plan_that_is_not_a_list():
    """`plan` is model-authored against a prose schema — drift is normal, not an error."""
    from board_state import assignment_docs

    for junk in ("chưa có", {"step": "một"}, None, 7):
        doc = assignment_docs([_assignment(plan=junk)], now=200.0)["a1"]
        assert doc["plan"] == []
        assert doc["progress"] is None


def test_assignment_docs_drop_a_plan_step_that_is_not_a_dict():
    from board_state import assignment_docs

    doc = assignment_docs([_assignment(plan=["một", {"step": "hai", "state": "done"}, None])], now=200.0)["a1"]
    assert [s["step"] for s in doc["plan"]] == ["hai"]
    assert doc["progress"] == 1.0


def test_assignment_docs_mark_an_unrecognised_step_state_as_unknown_never_todo():
    """Same rule as normalize_state(): an unrecognised value must look unrecognised. Folding a
    step the manager wrote as `in_progress` into `todo` would understate real progress, and
    folding it into `done` would overstate it."""
    from board_state import assignment_docs

    doc = assignment_docs([_assignment(plan=[{"step": "một", "state": "in_progress"}])], now=200.0)["a1"]
    assert doc["plan"][0]["state"] == "unknown"


def test_assignment_docs_carry_the_step_fields_the_page_renders():
    from board_state import assignment_docs

    step = {"step": "dựng schema", "owner": "worker-a", "depends_on": ["một"], "eta": "2030-02-02", "state": "doing"}
    got = assignment_docs([_assignment(plan=[step])], now=200.0)["a1"]["plan"][0]
    assert got["step"] == "dựng schema"
    assert got["owner"] == "worker-a"
    assert got["depends_on"] == ["một"]
    assert got["eta"] == "2030-02-02"
    assert got["state"] == "doing"


def test_assignment_docs_coerce_ado_refs_to_a_list():
    from board_state import assignment_docs

    assert assignment_docs([_assignment(ado_refs=None)], now=200.0)["a1"]["ado_refs"] == []


def test_assignment_docs_default_an_unrecognised_priority_to_p1():
    """Same reasoning as escalation_severity(): an unknown value stays a human's call, and must
    never be quietly promoted to P0."""
    from board_state import assignment_docs

    assert assignment_docs([_assignment(priority="urgent!")], now=200.0)["a1"]["priority"] == "P1"


def test_build_writes_emits_the_assignments_collection():
    from board_state import build_writes

    writes = build_writes(
        agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={},
        now=200.0, assignments=[_assignment()],
    )
    rows = _writes_for(writes, "assignments")
    assert len(rows) == 1
    assert rows[0]["op"] == "set"
    assert rows[0]["doc_id"] == "a1"
    assert rows[0]["data"]["title"] == "Dựng lại pipeline"


def test_build_writes_still_puts_meta_status_last_with_assignments_present():
    """meta/status asserts the rows before it are current — a new collection must not slip in
    after it."""
    from board_state import build_writes

    writes = build_writes(
        agents=[], registry={}, escalations=[], tickets=[], pr_by_ticket={},
        now=200.0, assignments=[_assignment()],
    )
    assert writes[-1]["collection"] == "meta"


def test_collect_emits_assignment_documents_from_the_injected_reader():
    from board_state import collect

    writes = collect(
        read_agents=list, read_registry=dict, read_escalations=list, read_tickets=list,
        read_prs=dict, now=lambda: 200.0,
        read_assignments=lambda: [_assignment()],
    )
    rows = _writes_for(writes, "assignments")
    assert [r["doc_id"] for r in rows] == ["a1"]


def test_collect_folds_an_append_only_ledger_to_the_latest_record_per_id(tmp_path):
    """End to end over a real ledger file, the way main() reads it: two records for one id, and
    the board must publish one document carrying the newer one."""
    from assignments import append
    from board_state import collect
    from escalations import current_state

    path = str(tmp_path / "assignments.jsonl")
    append(_assignment(status="assigned", note="cũ"), path=path)
    append(_assignment(status="done", note="mới"), path=path)

    writes = collect(
        read_agents=list, read_registry=dict, read_escalations=list, read_tickets=list,
        read_prs=dict, now=lambda: 200.0,
        read_assignments=lambda: current_state(path),
    )
    rows = _writes_for(writes, "assignments")
    assert len(rows) == 1
    assert rows[0]["data"]["status"] == "done"
    assert rows[0]["data"]["note"] == "mới"


def test_collect_degrades_assignments_to_empty_without_blanking_other_sources():
    from board_state import collect

    def boom():
        raise RuntimeError("ledger unreadable")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "state": "working"}],
        read_registry=dict, read_escalations=list, read_tickets=list, read_prs=dict,
        now=lambda: 200.0, read_assignments=boom,
    )
    assert not _writes_for(writes, "assignments")
    assert _writes_for(writes, "sessions")
    assert writes[-1]["collection"] == "meta"


def test_collect_needs_no_assignments_reader_from_an_existing_caller():
    """Every call site written before this parameter existed must keep working unchanged."""
    from board_state import collect

    writes = collect(
        read_agents=list, read_registry=dict, read_escalations=list, read_tickets=list,
        read_prs=dict, now=lambda: 200.0,
    )
    assert not _writes_for(writes, "assignments")
    assert writes[-1]["collection"] == "meta"


def test_collect_wires_the_iterations_reader_into_default_sprint():
    from board_state import collect

    writes = collect(
        read_agents=list,
        read_registry=dict,
        read_escalations=list,
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "Sprint 58", "url": "u"}],
        read_prs=dict,
        now=lambda: _ts(2026, 9, 8),
        read_iterations=lambda: ITERATIONS,
    )
    assert writes[-1]["data"]["default_sprint"] == "Sprint 58"


def test_collect_degrades_iterations_to_empty_without_blanking_other_sources():
    """`az` failing on the iteration lookup (not logged in, network down) must not blank the
    board — same contract as every other reader in collect()."""
    from board_state import collect

    def boom():
        raise OSError("az not logged in")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "state": "idle"}],
        read_registry=dict, read_escalations=list, read_tickets=list, read_prs=dict,
        now=lambda: 500.0, read_iterations=boom,
    )
    assert _writes_for(writes, "sessions")
    assert writes[-1]["data"]["default_sprint"] is None
    assert writes[-1]["collection"] == "meta"


def test_collect_needs_no_iterations_reader_from_an_existing_caller():
    """Every call site written before this parameter existed must keep working unchanged."""
    from board_state import collect

    writes = collect(
        read_agents=list, read_registry=dict, read_escalations=list, read_tickets=list,
        read_prs=dict, now=lambda: 200.0,
    )
    assert writes[-1]["data"]["default_sprint"] is None


def test_main_wires_an_iterations_reader_for_the_default_sprint():
    import inspect

    import board_state

    src = inspect.getsource(board_state.main)
    assert "read_iterations" in src, "main() never wires an iterations reader"
    assert "get_ado_iterations" in src


def test_main_actually_wires_the_real_pr_reader_into_the_collect_call(monkeypatch, tmp_path, capsys):
    """A source-string check (`"read_prs" in inspect.getsource(main)`) would stay green even if
    the `collect(...)` call inside main() went back to `read_prs=dict` — the `def read_prs():`
    closure would still be defined and still mention every name a string check could look for, it
    would just sit there unused. The only thing that actually proves the wiring is running
    main() and checking a ticket doc really has PR data.

    Every OTHER subprocess-backed reader is faked here too, deliberately — this must not become
    a test that shells out to a real `az`/`claude` and depends on this machine being logged in.
    `dashboard.get_github_prs` is the one seam left real end to end: main() must reach it, not a
    bypass of it.
    """
    import json

    import board_state
    import dashboard
    import manager_daemon
    import manager_session

    monkeypatch.setattr(manager_daemon, "list_agents", lambda: [])
    monkeypatch.setattr(
        dashboard, "get_ado_backlog", lambda: [{"id": "42", "title": "t", "state": "New", "sprint": "S", "url": "u"}]
    )
    monkeypatch.setattr(dashboard, "get_ado_iterations", lambda: [])
    monkeypatch.setattr(dashboard, "_ado_identities", lambda: [])
    monkeypatch.setattr(manager_session, "_read_state", lambda: {})
    monkeypatch.setattr(manager_session, "resolve_repo_root", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(board_state, "QUEUE_PATH", str(tmp_path / "no-escalations.jsonl"))
    monkeypatch.setattr(board_state, "LEDGER_PATH", str(tmp_path / "no-assignments.jsonl"))
    monkeypatch.setattr(
        dashboard,
        "get_github_prs",
        lambda repo_root: [{"number": 1, "title": "fix: x (AB#42)", "url": "pu", "state": "OPEN", "isDraft": False}],
    )

    board_state.main()

    writes = json.loads(capsys.readouterr().out)
    tickets = [w for w in writes if w["collection"] == "tickets"]
    assert tickets[0]["data"]["pr"] == {"number": 1, "state": "OPEN", "url": "pu"}


def test_main_reads_the_whole_assignment_ledger_not_only_the_open_ones():
    """open_assignments() would drop every finished assignment off the board the moment it was
    closed. read_all() hands over every record of every id — done included, and every earlier
    revision of each, which is the only place the start of the work is written down.
    current_state() would keep all the ids too but fold away those earlier revisions, leaving
    only a `ts` that says when the manager last touched the assignment."""
    import inspect

    import board_state

    src = inspect.getsource(board_state.main)
    assert "read_assignments" in src, "main() never wires an assignments reader"
    assert "LEDGER_PATH" in src
    # Comments stripped: main() is expected to *explain* why open_assignments() is the wrong
    # reader, so only an actual call to it counts as the mistake.
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "open_assignments(" not in code
    assert "read_all(LEDGER_PATH)" in code


# ---------------------------------------------------------------------------
# Assignments on the page.
# ---------------------------------------------------------------------------


def test_board_renders_an_assignments_section_in_the_main_render_path():
    script = _board_html_script()
    assert re.search(r"function renderAssignments\(\)", script), "renderAssignments() not found"
    call = re.search(r"board\.replaceChildren\((.*?)\);", script, re.S)
    assert call, "render()'s replaceChildren call not found"
    assert "renderAssignments()" in call.group(1), "assignments section missing from render()"


def test_board_subscribes_to_the_assignments_collection():
    script = _board_html_script()
    assert '"assignments"' in script or "'assignments'" in script
    assert "view.assignments" in script, "assignments never reach the view state"


def test_board_labels_every_assignment_status_in_vietnamese():
    """assignments.py's STATUSES is the closed vocabulary; a status with no label would render
    as a bare English slug on a Vietnamese board."""
    from assignments import STATUSES

    script = _board_html_script()
    block = re.search(r"const ASSIGNMENT_STATUS_LABEL\s*=\s*\{(.*?)\}", script, re.S)
    assert block, "ASSIGNMENT_STATUS_LABEL not found"
    for status in STATUSES:
        assert status in block.group(1), f"no Vietnamese label for status {status}"
    assert re.search(r"[àáảãạăâằắẳẵặầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
                     block.group(1), re.I), "status labels must be Vietnamese with diacritics"


def test_board_labels_every_plan_step_state_in_vietnamese_including_unknown():
    script = _board_html_script()
    block = re.search(r"const STEP_STATE_LABEL\s*=\s*\{(.*?)\}", script, re.S)
    assert block, "STEP_STATE_LABEL not found"
    for state in ("todo", "doing", "done", "unknown"):
        assert state in block.group(1), f"no label for step state {state}"


def test_board_shows_plan_steps_and_how_many_are_done():
    script = _board_html_script()
    body = re.search(r"function assignmentCard\((.*?)\n\}", script, re.S)
    assert body, "assignmentCard() not found"
    card = body.group(1)
    assert ".plan" in card, "the card never reads the plan steps"
    assert "doneSteps" in card, "the card never says how much of the plan is done"
    assert "at_risk" in card, "the card never surfaces at_risk"


def test_board_handles_a_missing_or_broken_assignments_source_like_every_other_section():
    """Loading, empty and errored must look like three different things here too — an errored
    ledger rendering as 'no assignments' is the exact failure the other sections guard against."""
    script = _board_html_script()
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", script, re.S)
    assert body, "renderAssignments() not found"
    assert "view.loaded.assignments" in body.group(1)
    assert "view.errors.assignments" in body.group(1)


def test_refresh_strip_never_hands_a_null_child_to_replace_children():
    """h() drops a null child; replaceChildren() renders it as the literal text "null".

    renderRefresh() appends the error chip conditionally, so the not-errored case passes null
    straight into replaceChildren — which put the word "null" on the topbar of a perfectly
    healthy board. Caught by rendering the page, not by reading it, so this pins the fix.
    """
    script = _board_html_script()
    body = re.search(r"function renderRefresh\(\)\s*\{(.*?)\n\}", script, re.S)
    assert body, "renderRefresh() not found"
    if ": null" in body.group(1):
        assert "filter(Boolean)" in body.group(1), (
            "a conditional child must be filtered out before replaceChildren, not passed as null"
        )


# ---------------------------------------------------------------------------
# Elapsed time, token spend and ETA — what a condensed assignment card has to say.
# ---------------------------------------------------------------------------


def _usage(inp=0, out=0, creation=0, cache_read=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": cache_read,
    }


def test_assignment_docs_measure_elapsed_from_the_oldest_record_for_that_id():
    """The ledger is append-only and current_state() keeps only the newest record per id, whose
    `ts` is when it was last UPDATED. Taking that as the start reports a fresh assignment for one
    that has been running all day."""
    from board_state import assignment_docs

    ledger = [
        _assignment(ts=100.0, status="assigned"),
        _assignment(ts=500.0, status="in_progress"),
    ]

    doc = assignment_docs(ledger, now=900.0)["a1"]

    assert doc["elapsed_seconds"] == 800.0, "elapsed must run from the oldest record, not the newest"


def test_assignment_docs_stop_the_clock_once_the_assignment_is_closed():
    """A finished assignment took as long as it took. Letting `now` keep running would grow the
    number forever and make last week's closed work look like the longest job on the board."""
    from board_state import assignment_docs

    for status in ("done", "cancelled"):
        ledger = [_assignment(ts=100.0, status="in_progress"), _assignment(ts=500.0, status=status)]
        doc = assignment_docs(ledger, now=999_999.0)[ "a1"]
        assert doc["elapsed_seconds"] == 400.0, f"the clock must stop at the last record for {status}"


def test_assignment_docs_report_no_elapsed_when_the_ledger_has_no_usable_timestamp():
    from board_state import assignment_docs

    doc = assignment_docs([_assignment(ts=None)], now=900.0)["a1"]

    assert doc["elapsed_seconds"] is None


def test_assignment_docs_report_unknown_token_spend_rather_than_zero():
    """0 means "measured, and it was nothing". An unreadable transcript means "not measured".
    Rendering the second as the first is the whole reason this field is nullable."""
    from board_state import assignment_docs

    rec = _assignment(plan=[{"step": "x", "owner": "worker-a", "state": "doing"}])

    doc = assignment_docs([rec], now=900.0, registry={}, usage_by_session={})["a1"]

    assert doc["tokens"] is None, "an owner with no registry entry must read as unmeasured, not zero"


def test_assignment_docs_keep_cache_reads_out_of_the_headline_token_number():
    """Measured on this machine: one session logged 2.2M output against 954M cache-read. Summing
    all four fields into one "tokens used" number reports ~50x the real spend."""
    from board_state import assignment_docs

    rec = _assignment(plan=[{"step": "x", "owner": "worker-a", "state": "doing"}])
    registry = {"worker-a": {"session_id": "sid-a"}}
    usage = {"sid-a": _usage(inp=10, out=20, creation=30, cache_read=9_000_000)}

    doc = assignment_docs([rec], now=900.0, registry=registry, usage_by_session=usage)["a1"]

    assert doc["tokens"]["spend"] == 60, "the headline number is input + output + cache_creation"
    assert doc["tokens"]["cache_read"] == 9_000_000, "cache reads are reported, but on their own"


def test_assignment_docs_sum_tokens_across_every_owner_session_once():
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "owner": "worker-a", "state": "done"},
        {"step": "b", "owner": "worker-b", "state": "doing"},
        {"step": "c", "owner": "worker-a", "state": "todo"},
    ])
    registry = {"worker-a": {"session_id": "sid-a"}, "worker-b": {"session_id": "sid-b"}}
    usage = {"sid-a": _usage(out=100), "sid-b": _usage(out=5)}

    doc = assignment_docs([rec], now=900.0, registry=registry, usage_by_session=usage)["a1"]

    assert doc["tokens"]["spend"] == 105, "a session owning two steps must not be counted twice"
    assert doc["tokens"]["partial"] is False


def test_assignment_docs_ignore_an_owner_that_is_not_a_worktree_task():
    """`owner` is free text the manager writes: "self (EM)", "CTO", "chưa giao" are people and
    placeholders, not sessions. They have no transcript and must not drag the total to unknown."""
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "owner": "self (EM)", "state": "done"},
        {"step": "b", "owner": "worker-a", "state": "doing"},
    ])
    registry = {"worker-a": {"session_id": "sid-a"}}

    doc = assignment_docs([rec], now=900.0, registry=registry,
                          usage_by_session={"sid-a": _usage(out=7)})["a1"]

    assert doc["tokens"]["spend"] == 7
    assert doc["tokens"]["partial"] is False, "a human owner is not a transcript that failed to read"


def test_assignment_docs_flag_a_total_that_is_missing_a_session_transcript():
    """One worktree deleted, its transcript gone: the remaining sum is real but too low. Marked
    partial so the page can say "at least this much" instead of presenting it as the full cost."""
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "owner": "worker-a", "state": "done"},
        {"step": "b", "owner": "worker-gone", "state": "doing"},
    ])
    registry = {"worker-a": {"session_id": "sid-a"}, "worker-gone": {"session_id": "sid-gone"}}

    doc = assignment_docs([rec], now=900.0, registry=registry,
                          usage_by_session={"sid-a": _usage(out=7), "sid-gone": None})["a1"]

    assert doc["tokens"]["spend"] == 7
    assert doc["tokens"]["partial"] is True


def test_sum_usage_reads_usage_at_either_nesting_level():
    """Transcript lines carry usage under `message.usage` (assistant turns) and, on some lines,
    at the top level. Reading only one shape silently halves the count."""
    from board_state import sum_usage

    totals = sum_usage([
        {"message": {"usage": _usage(out=10, cache_read=1)}},
        {"usage": _usage(out=5, cache_read=2)},
        {"message": "not a dict"},
        {"usage": {"output_tokens": "nonsense"}},
        "not a record",
    ])

    assert totals["output_tokens"] == 15
    assert totals["cache_read_input_tokens"] == 3


def test_read_session_usage_returns_none_when_the_transcript_is_missing(tmp_path):
    """None, not an empty total: "no file" and "a file that logged nothing" are different facts
    and only one of them may render as a number."""
    from board_state import read_session_usage

    assert read_session_usage("no-such-session", str(tmp_path)) is None


def test_read_session_usage_finds_the_transcript_under_any_project_directory(tmp_path):
    """A worktree session's transcript lives under a project directory named after the WORKTREE
    path, not the repo root, so the lookup is by session id across all of them."""
    import json as _json

    from board_state import read_session_usage

    proj = tmp_path / "-home-someone-repo--claude-worktrees-worker-a"
    proj.mkdir()
    (proj / "sid-a.jsonl").write_text(
        _json.dumps({"message": {"usage": _usage(inp=1, out=2, creation=3, cache_read=4)}}) + "\n"
        + "{ truncated line\n",
        encoding="utf-8",
    )

    totals = read_session_usage("sid-a", str(tmp_path))

    assert totals["output_tokens"] == 2
    assert totals["cache_read_input_tokens"] == 4


def test_assignment_docs_surface_the_eta_of_the_step_being_worked():
    """`eta` is free text a person wrote ("2-3 giờ", "chờ CTO"). It is shown verbatim, never
    parsed into a date the writer never committed to."""
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "owner": "worker-a", "state": "done", "eta": "xong rồi"},
        {"step": "b", "owner": "worker-a", "state": "doing", "eta": "2-3 giờ"},
        {"step": "c", "owner": "worker-a", "state": "todo", "eta": "sau khi worker xong"},
    ])

    doc = assignment_docs([rec], now=900.0, registry={"worker-a": {"session_id": "s"}})["a1"]

    assert doc["eta"]["text"] == "2-3 giờ"


def test_assignment_docs_fall_back_to_the_first_unfinished_step_when_nothing_is_being_worked():
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "state": "done", "eta": "xong rồi"},
        {"step": "b", "state": "todo", "eta": "chưa đặt"},
    ])

    doc = assignment_docs([rec], now=900.0)["a1"]

    assert doc["eta"]["text"] == "chưa đặt"


def test_assignment_docs_say_a_step_is_waiting_on_a_person():
    """An owner with no worktree entry is a human. No machine is burning time on it, so no
    completion time can be projected — the honest answer is that it is waiting on someone."""
    from board_state import assignment_docs

    rec = _assignment(plan=[{"step": "duyệt", "owner": "CTO", "state": "doing", "eta": "chờ CTO"}])

    doc = assignment_docs([rec], now=900.0, registry={"worker-a": {"session_id": "s"}})["a1"]

    assert doc["eta"]["waiting_human"] is True
    assert doc["eta"]["owner"] == "CTO"
    assert doc["eta"]["projected_seconds"] is None, "a human queue has no measured velocity"


def test_assignment_docs_project_the_remainder_from_measured_velocity():
    """Two of four steps done in 800s is 400s a step, so two steps left is ~800s more. Derived,
    never a promise — the page has to label it as an estimate."""
    from board_state import assignment_docs

    plan = [
        {"step": "a", "owner": "worker-a", "state": "done"},
        {"step": "b", "owner": "worker-a", "state": "done"},
        {"step": "c", "owner": "worker-a", "state": "doing", "eta": "2 giờ"},
        {"step": "d", "owner": "worker-a", "state": "todo"},
    ]
    ledger = [_assignment(ts=100.0, plan=plan), _assignment(ts=500.0, plan=plan)]
    registry = {"worker-a": {"session_id": "s"}}

    doc = assignment_docs(ledger, now=900.0, registry=registry)["a1"]

    assert doc["eta"]["projected_seconds"] == 800.0


def test_assignment_docs_project_nothing_before_the_first_step_finishes():
    """Zero steps done is zero measurements. Dividing by it would either crash or invent a rate."""
    from board_state import assignment_docs

    rec = _assignment(plan=[{"step": "a", "owner": "worker-a", "state": "doing"}])

    doc = assignment_docs([rec], now=900.0, registry={"worker-a": {"session_id": "s"}})["a1"]

    assert doc["eta"]["projected_seconds"] is None


def test_assignment_docs_report_no_eta_for_a_closed_assignment():
    from board_state import assignment_docs

    rec = _assignment(status="done", plan=[{"step": "a", "state": "done", "eta": "2 giờ"}])

    assert assignment_docs([rec], now=900.0)["a1"]["eta"] is None


def test_build_writes_passes_the_registry_and_usage_through_to_the_assignments():
    """The join from a plan step's `owner` to a session transcript runs through the registry, so
    the write set has to carry it — an assignment doc built without it can only ever say "chưa rõ"."""
    from board_state import build_writes

    rec = _assignment(plan=[{"step": "a", "owner": "worker-a", "state": "doing"}])
    writes = build_writes(
        agents=[], registry={"worker-a": {"session_id": "sid-a"}}, escalations=[], tickets=[],
        pr_by_ticket={}, now=900.0, assignments=[rec],
        usage_by_session={"sid-a": _usage(out=42)},
    )

    rows = _writes_for(writes, "assignments")
    assert rows[0]["data"]["tokens"]["spend"] == 42


def test_collect_reads_usage_for_the_sessions_the_registry_names():
    from board_state import collect

    seen = {}

    def read_usage(registry):
        seen.update(registry)
        return {"sid-a": _usage(out=42)}

    writes = collect(
        read_agents=list, read_registry=lambda: {"worker-a": {"session_id": "sid-a"}},
        read_escalations=list, read_tickets=list, read_prs=dict, now=lambda: 900.0,
        read_assignments=lambda: [_assignment(plan=[{"step": "a", "owner": "worker-a", "state": "doing"}])],
        read_usage=read_usage,
    )

    assert "worker-a" in seen, "the usage reader must be given the registry to join through"
    assert _writes_for(writes, "assignments")[0]["data"]["tokens"]["spend"] == 42


def test_collect_degrades_usage_to_unknown_without_blanking_the_assignments():
    from board_state import collect

    def boom(registry):
        raise RuntimeError("projects dir unreadable")

    writes = collect(
        read_agents=list, read_registry=lambda: {"worker-a": {"session_id": "sid-a"}},
        read_escalations=list, read_tickets=list, read_prs=dict, now=lambda: 900.0,
        read_assignments=lambda: [_assignment(plan=[{"step": "a", "owner": "worker-a", "state": "doing"}])],
        read_usage=boom,
    )

    rows = _writes_for(writes, "assignments")
    assert rows and rows[0]["data"]["tokens"] is None


# ---------------------------------------------------------------------------
# The condensed assignment card.
# ---------------------------------------------------------------------------


def _assignment_card_source():
    body = re.search(r"function assignmentCard\((.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "assignmentCard() not found"
    return body.group(1)


def _card_part(name):
    """The `summary` / `detail` array literal inside assignmentCard, so a test can tell what the
    collapsed line shows from what only opening the card shows."""
    card = _assignment_card_source()
    block = re.search(r"const " + name + r"\s*=\s*\[(.*?)\n  \];", card, re.S)
    assert block, f"assignmentCard() has no `{name}` list — the summary/detail split is what makes the card condensed"
    return block.group(1)


def test_board_collapses_an_assignment_card_through_the_existing_details_mechanism():
    """One collapse mechanism on the page, not two: collapsedGroup() already wraps
    <details>/<summary>, and a second hand-rolled toggle would style and behave differently."""
    script = _board_html_script()
    assert "collapsedGroup(" in _assignment_card_source(), "the card must collapse through collapsedGroup()"
    assert len(re.findall(r'h\("details"', script)) == 1, "only collapsedGroup() may build a <details>"


def test_board_summary_line_carries_steps_elapsed_tokens_and_eta():
    summary = _card_part("summary")
    assert "steps.length" in summary, "no step count on the collapsed line"
    assert "elapsed_seconds" in summary, "no elapsed time on the collapsed line"
    assert "token" in summary, "no token spend on the collapsed line"
    assert "eta" in summary, "no ETA on the collapsed line"


def test_board_keeps_the_long_note_and_the_step_list_behind_the_toggle():
    """The note is a paragraph of prose and the plan is every step — together they are what made
    one card fill the screen. They belong to the opened card, not the summary line."""
    summary, detail = _card_part("summary"), _card_part("detail")
    assert "a.note" not in summary, "the note must not be on the collapsed summary line"
    assert "a.note" in detail, "the note must still be readable once the card is open"
    assert "steps" in detail, "the step list belongs to the opened card"
    assert "steps.map" not in summary, "the collapsed line must not render every step"


def test_board_formats_token_counts_for_a_reader_not_as_raw_digits():
    """A 14-digit number is unreadable at a glance and is exactly what made the raw sum look
    plausible. Counts are shown as "1,2 tr" / "340 k"."""
    script = _board_html_script()
    fn = re.search(r"function tokenText\((.*?)\n\}", script, re.S)
    assert fn, "tokenText() not found"
    assert "tr" in fn.group(1) and "k" in fn.group(1), "no millions/thousands suffix"
    assert "1e6" in fn.group(1) or "1000000" in fn.group(1), "millions are never abbreviated"


def test_board_says_unknown_rather_than_zero_when_token_spend_was_not_measured():
    card = _assignment_card_source()
    assert "chưa rõ" in card, "an unmeasured token total must read 'chưa rõ', never 0"


def test_board_marks_a_projected_eta_as_an_estimate_and_never_as_a_commitment():
    """The derived number comes from steps-done-so-far, which is a crude rate. Presented bare it
    reads as a delivery time nobody promised."""
    card = _assignment_card_source()
    assert "projected_seconds" in card, "the card never shows the derived projection"
    assert "ước lượng" in card or "suy ra" in card, "a projection must be labelled as an estimate"


def test_board_says_an_assignment_is_waiting_on_a_person_instead_of_guessing_a_time():
    card = _assignment_card_source()
    assert "waiting_human" in card
    assert "chờ người" in card, "a human-owned step must say so in Vietnamese"


def test_board_labels_a_partial_token_total_as_a_floor():
    card = _assignment_card_source()
    assert "partial" in card, "the card never distinguishes a complete total from a partial one"


def test_assignment_docs_stop_the_clock_at_the_write_time_not_the_creation_time():
    """The real ledger, read on this machine: every revision of an id carries the IDENTICAL `ts`,
    because an update copies the creation record and edits fields. Subtracting the closing
    record's `ts` from the opening one's is always exactly zero, which reported every finished
    assignment as having taken no time. `updated_at` is when the write happened."""
    from board_state import assignment_docs

    ledger = [
        _assignment(ts=100.0, updated_at=100.0, status="in_progress"),
        _assignment(ts=100.0, updated_at=700.0, status="done"),
    ]

    assert assignment_docs(ledger, now=999_999.0)["a1"]["elapsed_seconds"] == 600.0


def test_assignment_docs_say_unknown_rather_than_zero_for_a_close_with_no_write_time():
    """Records written before append() stamped every write carry no close time at all. Zero is
    the number subtraction produces there, and it is a claim the ledger never made."""
    from board_state import assignment_docs

    ledger = [_assignment(ts=100.0, status="in_progress"), _assignment(ts=100.0, status="done")]

    assert assignment_docs(ledger, now=999_999.0)["a1"]["elapsed_seconds"] is None


def test_the_ledger_write_path_records_when_it_wrote(tmp_path):
    """End to end over a real file, the way main() reads it: two appends through the one mandated
    writer, and the board must be able to say how long the work took."""
    from assignments import append
    from board_state import assignment_docs
    from escalations import read_all

    path = str(tmp_path / "assignments.jsonl")
    append(_assignment(status="in_progress"), path=path)
    append(_assignment(status="done"), path=path)

    doc = assignment_docs(read_all(path), now=0.0)["a1"]

    assert doc["elapsed_seconds"] is not None, "append() must stamp when it wrote"
    assert doc["elapsed_seconds"] >= 0.0


def test_assignment_docs_never_call_a_retired_worker_a_person():
    """The registry only holds tasks whose worktree still exists. An owner missing from it may be
    a finished worker just as easily as a human, and "chờ người (board-systemd-timer)" is a
    confident wrong answer — the name says plainly that it is a task."""
    from board_state import assignment_docs

    rec = _assignment(plan=[
        {"step": "a", "owner": "board-systemd-timer", "state": "done"},
        {"step": "b", "owner": "board-systemd-timer", "state": "doing", "eta": "2-3 giờ"},
    ])

    eta = assignment_docs([rec], now=900.0, registry={})["a1"]["eta"]

    assert eta["waiting_human"] is False, "a kebab-case task name is never a person"
    assert eta["text"] == "2-3 giờ"
    assert eta["projected_seconds"] is None, "no running worker behind the step means no measured rate"


def test_assignment_docs_read_every_placeholder_owner_as_a_person():
    from board_state import assignment_docs

    for owner in ("CTO", "self (EM)", "chưa giao", "(CTO review)", "EM"):
        rec = _assignment(plan=[{"step": "x", "owner": owner, "state": "doing"}])
        eta = assignment_docs([rec], now=900.0)["a1"]["eta"]
        assert eta["waiting_human"] is True, f"{owner!r} is not a worktree task"


def test_board_never_prints_a_formatter_that_gave_up():
    """tokenText()/durationText() return null for anything they cannot format. Concatenated
    straight into a string that becomes the word "null" on the card — the same silent-garbage
    failure renderRefresh() was already bitten by."""
    card = _assignment_card_source()
    for expr in re.findall(r'"[^"]*"\s*\+\s*(tokenText|durationText)\(', card):
        raise AssertionError(f"{expr}() result concatenated without a null check")


# ---------------------------------------------------------------------------
# One work section: a running session lives inside the assignment it serves.
#
# "Việc đã giao" and "Phiên làm việc" named the same thing twice — every assignment already
# writes its worker into a plan step's `owner`, and that worker then appeared again in a section
# of its own. The section is gone and the session moved into the card it belongs to.
#
# The section it replaced carried the board's single most important alarm: a "đang chờ duyệt"
# worker looks exactly like a running one and is in fact stopped, waiting for a person. Its
# "cần bạn xử lý" group defaulted OPEN so that alarm could never sit below the fold. Folding
# sessions into cards that are collapsed by default is precisely how that alarm gets buried, so
# most of what follows exists to prove it did not.
# ---------------------------------------------------------------------------


def _js_function(name, src=None):
    """One top-level `function name(...) {...}` lifted out of board.html's <script>."""
    src = _board_html_script() if src is None else src
    body = re.search(r"^function " + name + r"\(.*?\n\}", src, re.S | re.M)
    assert body, f"{name}() not found in board.html"
    return body.group(0)


def _run_board_js(snippet):
    """Run board.html's real session/assignment join helpers under node.

    The join and the partition below are the two places a session can vanish — claimed by no
    card yet still counted by the topbar chip, which is an alarm the board reports and never
    shows. A regex over the source cannot see that; running the functions can, and they are
    pure (no DOM, no db), so node needs nothing stubbed.
    """
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:  # pragma: no cover - node is present in this repo's dev env
        pytest.skip("node is not installed")
    src = _board_html_script()
    prelude = "\n".join(
        _js_function(name, src) for name in ("ownersOf", "sessionsOf", "looseSessions", "needsAttention")
    )
    out = subprocess.run([node, "-e", prelude + "\n" + snippet], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_board_has_no_standalone_sessions_section():
    """Two sections describing the same work is the complaint. One goes."""
    script = _board_html_script()
    assert "function renderSessions" not in script, "renderSessions() must be gone, not just unused"
    call = re.search(r"board\.replaceChildren\((.*?)\);", script, re.S)
    assert call, "render()'s replaceChildren call not found"
    assert "renderSessions()" not in call.group(1), "the sessions section is still drawn on its own"
    assert 'sectionHead("Phiên làm việc"' not in script, "the standalone sessions section header survives"


def test_board_attaches_a_session_to_the_assignment_whose_step_owns_it():
    """The join is `plan[].owner` == the session's task name — the same one the token sum runs
    through the registry. A second, different mapping is how the two would drift."""
    out = _run_board_js(
        """
        const a = { plan: [{ step: "x", owner: "alpha" }, { step: "y", owner: "CTO" }] };
        const sessions = [
          { task: "alpha", managed: true, state: "running" },
          { task: "beta", managed: true, state: "running" },
        ];
        console.log(JSON.stringify(sessionsOf(a, sessions).map((s) => s.task)));
        """
    )
    assert out == '["alpha"]', f"session did not land on the assignment that owns it: {out}"


def test_board_partitions_every_session_between_the_cards_and_the_loose_group():
    """Exhaustive and non-overlapping. A session in neither half is an alarm the topbar counts
    and the board never renders — the exact drift the shared needsAttention() exists to stop."""
    out = _run_board_js(
        """
        const open = [
          { plan: [{ step: "x", owner: "alpha" }] },
          { plan: [{ step: "y", owner: "beta" }] },
          { plan: [] },
        ];
        const sessions = ["alpha", "beta", "gamma"].map((t) => ({ task: t, managed: true, state: "running" }));
        const claimed = open.flatMap((a) => sessionsOf(a, sessions)).map((s) => s.task);
        const loose = looseSessions(open, sessions).map((s) => s.task);
        console.log(JSON.stringify([claimed.sort(), loose.sort()]));
        """
    )
    assert out == '[["alpha","beta"],["gamma"]]', f"sessions were dropped or double-claimed: {out}"


def test_board_counts_the_same_stalled_sessions_the_topbar_chip_does():
    """renderSummary()'s chip counts every session needsAttention() matches. After the fold, the
    same sessions must still be reachable — some on cards, the rest in the loose group — or the
    chip announces a number the page cannot show."""
    out = _run_board_js(
        """
        const open = [{ plan: [{ step: "x", owner: "alpha" }] }];
        const sessions = [
          { task: "alpha", managed: true, state: "waiting" },
          { task: "beta", managed: true, state: "unknown" },
          { task: "gamma", managed: false, state: "waiting" },
          { task: "delta", managed: true, state: "running" },
        ];
        const chip = sessions.filter(needsAttention).length;
        const shown = open.flatMap((a) => sessionsOf(a, sessions)).concat(looseSessions(open, sessions));
        console.log(JSON.stringify([chip, shown.filter(needsAttention).length]));
        """
    )
    assert out == "[2,2]", f"the chip and what the board renders disagree: {out}"


def test_board_opens_an_assignment_card_that_holds_a_session_needing_attention():
    """The card is condensed by default — that is the whole point of it — EXCEPT when it holds a
    worker that has stopped and is waiting for a person. That one must be readable without any
    click at all."""
    card = _assignment_card_source()
    call = re.search(r"collapsedGroup\(summary, detail,\s*([^)]*)\)", card)
    assert call, "assignmentCard() must pass collapsedGroup() an open hint, not a bare (summary, detail)"
    assert "attention" in call.group(1), (
        "the open hint must come from the sessions needing attention, not from anything else"
    )


def test_board_still_leaves_an_ordinary_assignment_card_closed():
    """The open hint is conditional, never a constant — a card that always opens is the wall of
    text this card was condensed to escape."""
    card = _assignment_card_source()
    call = re.search(r"collapsedGroup\(summary, detail,\s*([^)]*)\)", card)
    assert call, "collapsedGroup() call not found in assignmentCard()"
    assert call.group(1).strip() not in ("true", "1"), "the card must not open unconditionally"


def test_board_marks_the_collapsed_line_when_the_card_holds_a_stalled_session():
    """Auto-opening is not enough on its own: the summary line is what a manager scans, so it has
    to say why this card is different."""
    summary = _card_part("summary")
    assert "attention" in summary, "the collapsed line carries no marker for a stopped worker"
    assert "cần bạn xử lý" in summary, "the marker must say, in Vietnamese, that a person is needed"
    assert "pill-bad" in summary, "a stopped worker is an alarm, not a neutral fact"


def test_board_sorts_an_assignment_holding_a_stalled_session_above_the_rest():
    """The group this replaced sat at the top of the page so the alarm was never below the fold.
    Visible-if-you-scroll is not what that group guaranteed."""
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "renderAssignments() not found"
    sorts = [line for line in body.group(1).splitlines() if ".sort(" in line]
    assert any("needsAttention" in line for line in sorts), (
        "cards holding a session that needs a human must sort to the top"
    )


def test_board_keeps_a_group_for_sessions_no_open_assignment_claims():
    """An ad-hoc terminal, or a worker whose assignment was closed under it, still belongs on the
    board — collapsed at the end, exactly like the unmanaged-sessions group it replaces."""
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "renderAssignments() not found"
    assert "looseSessions(" in body.group(1), "sessions belonging to no assignment are dropped entirely"
    assert "sessionGroup(" in body.group(1), "the leftover sessions get no group of their own"


def test_board_opens_the_leftover_group_when_it_holds_a_session_needing_attention():
    """Same rule as the cards, at the other end of the section: a stopped worker nobody assigned
    is still a stopped worker, and a collapsed group hides it just as well as a collapsed card."""
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "renderAssignments() not found"
    group = re.search(r"sessionGroup\((.*?)\);", body.group(1), re.S)
    assert group, "sessionGroup() call not found"
    open_hint = group.group(1).rsplit(",", 1)[-1].strip()
    assert "needsAttention" in open_hint, (
        "the leftover group's open hint must come from needsAttention, not be a constant"
    )


def test_board_never_hides_a_session_inside_the_collapsed_closed_assignments_group():
    """"Đã đóng" is collapsed by default. A card in there is two clicks deep, so it gets no
    session tiles at all — its sessions fall to the leftover group, which opens itself."""
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "renderAssignments() not found"
    closed = re.search(r"closed\.slice\(\)\.sort\(byUrgency\)\.map\((.*?)\)\)", body.group(1), re.S)
    assert closed, "the closed-assignments group's card mapping not found"
    assert "sessionsOf" not in closed.group(1), (
        "a closed card must not receive session tiles — it lives inside a collapsed group"
    )


def test_board_reports_a_broken_sessions_source_in_the_work_section():
    """The section now reads two sources. An errored one is unknown, never 'nothing is running' —
    the same rule every other section on this page already follows."""
    body = re.search(r"function renderAssignments\(\)\s*\{(.*?)\n\}\n", _board_html_script(), re.S)
    assert body, "renderAssignments() not found"
    assert "view.errors.sessions" in body.group(1), "a dead sessions listener reads as 'no sessions'"
    assert "view.loaded.sessions" in body.group(1), "loading and empty look the same"


def test_board_summary_line_counts_steps_and_never_a_percentage():
    """`0/7 bước` and `0%` say the same thing twice. The steps survive; the percentage does not."""
    summary = _card_part("summary")
    assert "bước" in summary, "the collapsed line no longer says how many steps are done"
    assert "%" not in summary, "the percentage is still on the collapsed line"


def test_board_no_longer_computes_a_percentage_anywhere():
    """board_state.py keeps publishing `progress` for the localhost dashboard, but a value this
    page stopped showing must not leave a dead computation behind."""
    card = _assignment_card_source()
    assert "a.progress" not in card, "the card still reads a field it no longer shows"
    assert "pct" not in card, "the percentage computation is dead code now"
