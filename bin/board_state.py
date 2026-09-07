#!/usr/bin/env python3
"""Pure transforms from this plugin's own data sources into artifact-db documents.

No I/O, no subprocess, no session: every function here takes already-read data and returns
plain dicts. That is what makes the board's data model testable without publishing anything.
"""


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
