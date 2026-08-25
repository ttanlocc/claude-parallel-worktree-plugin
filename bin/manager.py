#!/usr/bin/env python3
"""The manager tier: turn one escalation into a decision, and deliver it back to the worker.

Everything here except run_manager()/deliver_answer() is pure, so the judgement logic is testable
without spawning a model.
"""

import json
import subprocess

MANAGER_MODEL = "claude-fable-5"

_INSTRUCTIONS = """You are the manager for a team of autonomous coding sessions.

One worker cannot decide something alone and has escalated it to you. Decide it. You are the last
step before a human gets interrupted, so decide when the evidence supports a decision — but say so
honestly when it does not.

Reply with ONE JSON object and nothing else:
{"answer": "<what the worker should do, imperative and specific>",
 "reason": "<why, in one sentence, grounded in the evidence>",
 "confidence": "high" | "low"}

Use "low" when the evidence genuinely does not settle it — a human will then be asked instead.
Never invent evidence you were not given."""


def build_prompt(record: dict) -> str:
    """Assemble the manager's prompt: instructions, the question, its options, and the evidence."""
    parts = [_INSTRUCTIONS, "", f"Kind: {record.get('kind')}", f"Question: {record.get('question')}"]
    options = record.get("options") or []
    if options:
        parts.append("Options (your answer must be exactly one of these):")
        parts.extend(f"  - {o}" for o in options)
    evidence = record.get("evidence") or {}
    if evidence:
        parts.append("Evidence:")
        for key, value in evidence.items():
            parts.append(f"  {key}: {json.dumps(value) if not isinstance(value, str) else value}")
    return "\n".join(parts)


def parse_decision(text: str) -> dict | None:
    """Pull the decision object out of the model's reply — bare, fenced, or embedded in prose.

    Walks a real JSON decoder across every `{` rather than regex-matching braces: a regex cannot
    balance nesting, and a decision carrying any nested field would otherwise be dropped entirely.
    Among the objects found, prefers one shaped like a decision, so a model that echoes its input
    before answering does not get its echo mistaken for the answer.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    candidates = []
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    for obj in candidates:
        if all(k in obj for k in ("answer", "reason", "confidence")):
            return obj
    for obj in candidates:
        if "answer" in obj:
            return obj
    return None


def validate_decision(decision, record: dict) -> str | None:
    """Return an error string if this decision is not usable, else None."""
    if not isinstance(decision, dict):
        return "decision is not a JSON object"
    for field in ("answer", "reason", "confidence"):
        if not decision.get(field):
            return f"missing {field}"
    if decision["confidence"] not in ("high", "low"):
        return f"confidence must be high or low, got {decision['confidence']!r}"
    options = record.get("options") or []
    if options and decision["answer"] not in options:
        return f"answer {decision['answer']!r} is not one of the offered options"
    return None


def manager_argv(prompt: str) -> list[str]:
    """Argv for one short-lived manager call. Print mode: one question in, one answer out."""
    return ["claude", "--model", MANAGER_MODEL, "-p", prompt]


def resume_argv(session_id: str, message: str) -> list[str]:
    """Argv that delivers a message into an existing session (verified: works, exits 0)."""
    return ["claude", "--resume", session_id, "-p", message]


def run_manager(record: dict, timeout: int = 180) -> str:
    """Thin shell: spawn the manager and hand back its raw reply."""
    result = subprocess.run(
        manager_argv(build_prompt(record)),
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout


def deliver_answer(session_id: str, message: str, timeout: int = 180) -> str:
    """Thin shell: push an answer into a blocked worker session."""
    result = subprocess.run(
        resume_argv(session_id, message),
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout
