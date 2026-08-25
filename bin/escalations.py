#!/usr/bin/env python3
"""Escalation queue: one append-only JSONL record per report a worker cannot decide alone."""

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
    """Append one record. Append-only: history is never rewritten, only added to."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


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
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


# Path fragments that make a diff a human's call regardless of how clean it looks.
SENSITIVE_PATH_MARKERS = ("auth", "secret", ".env", "credential", "migration", "token", "password")

_TIER2_KINDS = {"red_tests", "looping", "pick_implementation", "scope_question"}
_TIER3_KINDS = {
    "irreversible",
    "push_or_pr",
    "credentials",
    "spec_ambiguity",
    "no_convergence",
    "worktree_collision",
}


def _sensitive(changed_files) -> str | None:
    for path in changed_files or []:
        low = path.lower()
        for marker in SENSITIVE_PATH_MARKERS:
            if marker in low:
                return path
    return None


def classify(record: dict) -> tuple[str, str]:
    """Route one record: ("tier2", reason) the manager may decide, or ("tier3", reason) for a human.

    Tier 3 wins ties — anything that smells irreversible or security-shaped goes to the human even
    when the rest of the record looks mechanical.
    """
    ev = record.get("evidence") or {}
    kind = record.get("kind")

    if ev.get("irreversible"):
        return "tier3", "evidence marks this irreversible"
    if kind in _TIER3_KINDS:
        return "tier3", f"{kind} always needs a human"

    if kind == "diff_review":
        if ev.get("tests") != "green":
            return "tier3", f"tests are {ev.get('tests') or 'unknown'}, not green"
        if ev.get("deps_added"):
            return "tier3", f"adds dependency: {', '.join(ev['deps_added'])}"
        if ev.get("migration"):
            return "tier3", "changes a migration or schema"
        hit = _sensitive(ev.get("changed_files"))
        if hit:
            return "tier3", f"touches a sensitive path: {hit}"
        return "tier2", "tests green, no new deps, no migration, no sensitive path"

    if kind in _TIER2_KINDS:
        return "tier2", f"{kind} is a mechanical call"

    return "tier3", f"unknown kind {kind!r} — defaulting to a human"
