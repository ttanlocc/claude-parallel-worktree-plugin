#!/usr/bin/env python3
"""Escalation queue: one append-only JSONL record per report a worker cannot decide alone."""

import fcntl
import json
import os
import time
import uuid

QUEUE_PATH = os.path.expanduser("~/.claude/hermes/escalations.jsonl")


def new_record(session_id: str, kind: str, question: str, options=None, evidence=None) -> dict:
    """A fresh, undecided queue record. `tier` is filled in later by classify()."""
    return {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "session_id": session_id,
        "kind": kind,
        "question": question,
        "options": list(options or []),
        "evidence": dict(evidence or {}),
        "tier": None,
        "status": "open",
        "decided_by": None,
        "answer": None,
        "answered_at": None,
    }


def append(path: str, record: dict) -> None:
    """Append one record. Append-only: history is never rewritten, only added to.

    Locked around the write: several processes (daemon, dashboard, worker sessions) append
    concurrently, and an unlocked interleaved write produces a line `read_all` can only skip
    as garbage — silently losing the escalation.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record) + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_all(path: str) -> list[dict]:
    """Every record, oldest first. A truncated or garbage line is skipped, never fatal."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# Path fragments that make a diff a human's call regardless of how clean it looks.
SENSITIVE_PATH_MARKERS = (
    "auth",
    "secret",
    ".env",
    "credential",
    "migration",
    "token",
    "password",
    "passwd",
    ".pem",
    "id_rsa",
    ".key",
    "keystore",
    ".netrc",
    ".npmrc",
    "schema.sql",
    "alembic/",
)

_TIER2_KINDS = {"red_tests", "looping", "pick_implementation", "scope_question"}
_TIER3_KINDS = {
    "irreversible",
    "push_or_pr",
    "credentials",
    "spec_ambiguity",
    "no_convergence",
    "cost_anomaly",
    "worktree_collision",
}


def _as_text(value) -> str:
    """Render a field for a reason string whether it arrived as a str or a list."""
    if isinstance(value, str):
        return value
    return ", ".join(str(v) for v in value)


def _sensitive(changed_files) -> str | None:
    # A bare string is one path, not a sequence of characters — iterating it per-character
    # would match no marker and silently pass every secret file as clean.
    if isinstance(changed_files, str):
        changed_files = [changed_files]
    if isinstance(changed_files, (list, tuple)):
        for path in changed_files:
            low = str(path).lower()
            for marker in SENSITIVE_PATH_MARKERS:
                if marker in low:
                    return str(path)
        return None
    # Any other shape (dict, nested container, model-authored oddity) is untrusted,
    # worker-authored evidence — scan its whole string form rather than assume a structure.
    if changed_files:
        low = str(changed_files).lower()
        for marker in SENSITIVE_PATH_MARKERS:
            if marker in low:
                return str(changed_files)
    return None


def classify(record: dict) -> tuple[str, str]:
    """Route one record: ("tier2", reason) the manager may decide, or ("tier3", reason) for a human.

    Tier 3 wins ties — these signals hold whatever the kind is, so a mechanical-looking record
    carrying an irreversible, dependency, migration, or secret-shaped change still goes to a human.
    """
    ev = record.get("evidence") or {}
    kind = record.get("kind")

    if ev.get("irreversible"):
        return "tier3", "evidence marks this irreversible"
    if ev.get("deps_added"):
        return "tier3", f"adds dependency: {_as_text(ev['deps_added'])}"
    if ev.get("migration"):
        return "tier3", "changes a migration or schema"
    hit = _sensitive(ev.get("changed_files"))
    if hit:
        return "tier3", f"touches a sensitive path: {hit}"
    branch = str(ev.get("branch") or "").strip().lower()
    if branch in ("main", "master"):
        return "tier3", f"target branch is {branch}"

    if kind in _TIER3_KINDS:
        return "tier3", f"{kind} always needs a human"

    if kind == "diff_review":
        if ev.get("tests") != "green":
            return "tier3", f"tests are {ev.get('tests') or 'unknown'}, not green"
        # Silence is not consent. The record's author benefits from approval, so an undisclosed
        # field is treated as undisclosed rather than as clean. `key in ev` is not enough:
        # an explicit null is a routine shape for model-authored JSON, and the party filling
        # in evidence is the party asking for approval — so an undisclosed value must read as
        # undisclosed, never as clean.
        for key in ("deps_added", "migration", "changed_files"):
            if ev.get(key) is None:
                return "tier3", f"evidence does not disclose {key}"
        return "tier2", "tests green, no new deps, no migration, no sensitive path"

    if kind in _TIER2_KINDS:
        return "tier2", f"{kind} is a mechanical call"

    return "tier3", f"unknown kind {kind!r} — defaulting to a human"


def current_state(path: str) -> list[dict]:
    """Fold the append-only log into the latest state of each record, in first-seen order."""
    latest: dict[str, dict] = {}
    order: list[str] = []
    for rec in read_all(path):
        rid = rec.get("id")
        if not rid:
            continue
        if rid not in latest:
            order.append(rid)
        latest[rid] = rec
    return [latest[rid] for rid in order]


def record_answer(path: str, record_id: str, answer: str, decided_by: str) -> dict | None:
    """Answer a record by appending its updated copy. Returns the update, or None if unknown id."""
    for rec in current_state(path):
        if rec.get("id") == record_id:
            updated = dict(rec)
            updated["answer"] = answer
            updated["decided_by"] = decided_by
            updated["status"] = "answered"
            updated["answered_at"] = time.time()
            append(path, updated)
            return updated
    return None
