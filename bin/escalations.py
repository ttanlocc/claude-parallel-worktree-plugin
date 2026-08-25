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
