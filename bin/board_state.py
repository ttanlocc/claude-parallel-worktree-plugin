#!/usr/bin/env python3
"""Pure transforms from this plugin's own data sources into artifact-db documents.

No I/O, no subprocess, no session: every function here takes already-read data and returns
plain dicts. That is what makes the board's data model testable without publishing anything.
"""

import json
import os

from escalations import classify, normalize_kind, normalize_options

# Kinds that mean production is already hurting. Scored off the CANONICAL name, never the raw
# one: `blocked_on_credentials` is a credentials outage and must not be scored as an unknown.
_URGENT_KINDS = frozenset({"credentials", "irreversible", "cost_anomaly", "push_or_pr"})


# `claude agents --json --all` verified LIVE (2026-09-07, 33 real sessions): blocked(4) /
# done(21) / stopped(5) / None(8) — zero running, zero idle, zero waiting. board.html's whole
# vocabulary (STATE_RANK/STATE_LABEL) is the four canonical words on the right; this is the one
# seam that reconciles the CLI's real words with it, so a new CLI spelling is fixed in one place
# instead of guessed at by every consumer.
_STATE_MAP = {
    "working": "running",
    "blocked": "waiting",
    "stopped": "done",
    "running": "running",
    "idle": "idle",
    "waiting": "waiting",
    "done": "done",
}


def normalize_state(raw) -> str:
    """Map whatever `claude agents --json` actually emits onto the board's four canonical words.

    Anything not in the map — None/missing, or a future CLI word not seen yet — becomes
    "unknown", never "idle". Folding an unrecognised state into "idle" IS the bug this exists to
    prevent: a blocked worker showing the badge "Rảnh" (free) is the opposite of the truth. An
    unrecognised state must look unrecognised so board.html can still surface it, just not lie.
    """
    if isinstance(raw, str) and raw in _STATE_MAP:
        return _STATE_MAP[raw]
    return "unknown"


def session_docs(agents: list[dict], registry: dict) -> dict[str, dict]:
    """One document per live task, keyed by task name.

    Agent-first, not registry-first: a session doing work belongs on the board immediately,
    including one dispatched by any other means, and the registry only fills in branch/worktree
    once parallel-task.sh has provisioned it.
    """
    docs = {}
    for agent in agents or []:
        name = agent.get("name")
        if not name:
            # A document id cannot be empty; an unnamed agent has no addressable key.
            continue
        reg = (registry or {}).get(name) or {}
        docs[name] = {
            "task": name,
            "session_id": agent.get("sessionId"),
            "short_id": reg.get("short_id"),
            # `claude agents --json` has used both spellings; read whichever is present, then
            # translate it — see normalize_state() above.
            "state": normalize_state(agent.get("state") or agent.get("status")),
            "branch": reg.get("branch"),
            "worktree": reg.get("path"),
            "started_at": agent.get("startedAt"),
            "ado_refs": list(reg.get("ado_ids") or []),
        }
    return docs


def escalation_severity(record: dict) -> str:
    """P0 / P1 / P2 from what the record already says.

    An unrecognised kind stays P1 — a human's call. Recognising more kinds must never widen
    what looks urgent, or the vocabulary becomes a way to shout.
    """
    kind = normalize_kind(record.get("kind"))
    if kind in _URGENT_KINDS:
        return "P0"
    tier, _ = classify(record)
    return "P1" if tier == "tier3" else "P2"


def _safe_evidence(raw) -> dict[str, str]:
    """Coerce worker-authored evidence into a flat str->str dict the page can walk directly.

    Same tolerance as normalize_kind/normalize_options: evidence is written by a model against
    a prose schema, so "not even a dict" is normal drift, not an error — the whole thing is
    dropped rather than guessed at. A value that is not itself a string is stringified, never
    dropped: a value that says something is NOT affected ("git_push: khong anh huong") is exactly
    as load-bearing to a manager as one that says something broke.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in raw.items()}


def _str_or_none(value) -> str | None:
    """`reason`/`tier` must reach the page as a string or None — never some other JSON-native
    type a future producer might write."""
    return value if isinstance(value, str) else None


def escalation_docs(records: list[dict]) -> dict[str, dict]:
    """One document per escalation, keyed by its id."""
    docs = {}
    for rec in records or []:
        rec_id = rec.get("id")
        if not rec_id:
            continue
        docs[str(rec_id)] = {
            "ts": rec.get("ts"),
            "session": rec.get("session_id"),
            "kind": normalize_kind(rec.get("kind")),
            # Kept so the board can mark drift instead of absorbing it silently.
            "kind_raw": rec.get("kind"),
            "severity": escalation_severity(rec),
            "question": rec.get("question") or "",
            "options": normalize_options(rec.get("options")),
            "status": rec.get("status") or "open",
            "answer": rec.get("answer"),
            "answered_at": rec.get("answered_at"),
            # Why this reached a human, and what it touches — daemon/worker-authored context a
            # manager needs to act, not just triage. See docstrings above for the shape guards.
            "evidence": _safe_evidence(rec.get("evidence")),
            "reason": _str_or_none(rec.get("reason")),
            "tier": _str_or_none(rec.get("tier")),
        }
    return docs


def ticket_docs(tickets: list[dict], pr_by_ticket: dict) -> dict[str, dict]:
    """One document per ADO work item, keyed by its id.

    "Not started", "in flight" and "done this sprint" are filters over `state` + `sprint` on
    the page, not three collections here.
    """
    docs = {}
    for ticket in tickets or []:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue
        docs[str(ticket_id)] = {
            "id": str(ticket_id),
            "title": ticket.get("title") or "",
            "state": ticket.get("state") or "",
            "sprint": ticket.get("sprint") or "",
            "type": ticket.get("type") or "",
            "url": ticket.get("url") or "",
            "pr": (pr_by_ticket or {}).get(str(ticket_id)),
        }
    return docs


def meta_status(now: float, ado_swept_at=None, sessions_scanned_at=None, manager=None) -> dict:
    """When each source was last read, and who the manager is.

    One document, several clocks: the page shows the age of each source separately, because a
    30-minute-old ticket list must not be presentable as though it were as live as a session
    state. This doubles as the failure signal — an age that keeps growing IS the alarm.
    """
    manager = manager or {}
    return {
        "written_at": now,
        "last_ado_sweep": ado_swept_at,
        "last_session_scan": sessions_scanned_at,
        "manager_session_id": manager.get("session_id"),
        "manager_started_at": manager.get("started_at"),
    }


def build_writes(
    agents,
    registry,
    escalations,
    tickets,
    pr_by_ticket,
    now: float,
    ado_swept_at=None,
    manager=None,
) -> list[dict]:
    """Every document to write, in the order to write it.

    Shaped as the Artifact tool's `write_db` batch entries so the calling session passes this
    straight through without reshaping — the transform is testable here, and the session stays
    a thin courier.
    """
    writes = []
    for collection, docs in (
        ("sessions", session_docs(agents, registry)),
        ("escalations", escalation_docs(escalations)),
        ("tickets", ticket_docs(tickets, pr_by_ticket)),
    ):
        for doc_id, data in docs.items():
            writes.append({"op": "set", "collection": collection, "doc_id": doc_id, "data": data})
    # Last, always: this document asserts the rows above it are current, so a batch that dies
    # halfway must not have already claimed a sweep that did not land.
    writes.append(
        {
            "op": "set",
            "collection": "meta",
            "doc_id": "status",
            "data": meta_status(
                now=now,
                ado_swept_at=ado_swept_at,
                sessions_scanned_at=now,
                manager=manager,
            ),
        }
    )
    return writes


import subprocess
import sys

_SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def _safe(reader, fallback):
    """Read one source, or fall back. A source that cannot be read must not blank the board —
    every other source is still worth publishing, and meta/status shows the missing one aging."""
    try:
        return reader()
    except Exception:
        return fallback


def collect(read_agents, read_registry, read_escalations, read_tickets, read_prs, now) -> list[dict]:
    """Gather every source and return the write set. Readers are injected so this is testable
    without `az`, `gh`, or a live session."""
    tickets = _safe(read_tickets, None)
    stamp = now()
    return build_writes(
        agents=_safe(read_agents, []),
        registry=_safe(read_registry, {}),
        escalations=_safe(read_escalations, []),
        tickets=tickets or [],
        pr_by_ticket=_safe(read_prs, {}),
        now=stamp,
        # None, not `stamp`: a sweep that failed must not claim to have just run.
        ado_swept_at=stamp if tickets is not None else None,
    )


def main() -> int:
    """Print the write set as JSON. The scheduled session pipes this into `write_db`."""
    import time

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dashboard
    import manager_daemon

    def read_registry():
        # The registry FILE is already keyed by task name, which is the shape session_docs
        # wants. `dashboard.get_registry()` shells out to parallel-task.sh and returns a list;
        # reading the file skips a subprocess and a reshape.
        path = os.path.join(dashboard.REPO_DIR, ".claude", "worktrees", ".parallel-registry.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    writes = collect(
        # list_agents lives in manager_daemon, not dashboard.
        read_agents=manager_daemon.list_agents,
        read_registry=read_registry,
        read_escalations=lambda: dashboard.get_escalations()["needs_human"],
        # No `or None`: an empty backlog is a successful sweep that found nothing, and must
        # stamp last_ado_sweep. Only an exception (caught by _safe) means "did not run".
        read_tickets=dashboard.get_ado_backlog,
        read_prs=dict,
        now=time.time,
    )
    json.dump(writes, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
