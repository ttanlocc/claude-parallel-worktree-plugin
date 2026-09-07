#!/usr/bin/env python3
"""Pure transforms from this plugin's own data sources into artifact-db documents.

No I/O, no subprocess, no session: every function here takes already-read data and returns
plain dicts. That is what makes the board's data model testable without publishing anything.
"""

from escalations import classify, normalize_kind, normalize_options

# Kinds that mean production is already hurting. Scored off the CANONICAL name, never the raw
# one: `blocked_on_credentials` is a credentials outage and must not be scored as an unknown.
_URGENT_KINDS = frozenset({"credentials", "irreversible", "cost_anomaly", "push_or_pr"})


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
            # `claude agents --json` has used both spellings; read whichever is present.
            "state": agent.get("state") or agent.get("status"),
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
