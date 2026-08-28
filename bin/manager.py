#!/usr/bin/env python3
"""The manager tier: turn one escalation into a decision, and deliver it back to the worker.

Everything here except deliver_answer() is pure, so the judgement logic is testable without
spawning a model.
"""

import json
import subprocess

from escalations import classify

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
        value = decision.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"missing {field}"
    if decision["confidence"] not in ("high", "low"):
        return f"confidence must be high or low, got {decision['confidence']!r}"
    options = record.get("options") or []
    if options and decision["answer"] not in options:
        return f"answer {decision['answer']!r} is not one of the offered options"
    return None


def resume_argv(session_id: str, message: str) -> list[str]:
    """Argv that delivers a message into an existing session (verified: works, exits 0)."""
    return ["claude", "--resume", session_id, "-p", message]


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


def _route(record: dict, ask_model) -> dict:
    """Route one record. `ask_model(record) -> str` is injected so this stays testable.

    Anything the manager cannot settle cleanly degrades to needs_human — the failure mode of this
    function is "ask the person", never "guess".
    """
    try:
        tier, reason = classify(record)
    except Exception as e:  # a malformed record must not abort the queue pass
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": f"could not classify this escalation: {e}",
            "decided_by": None,
        }
    if tier == "tier3":
        return {"outcome": "needs_human", "answer": None, "reason": reason, "decided_by": None}

    try:
        raw = ask_model(record)
    except Exception as e:  # a dead model must not wedge the queue
        return {"outcome": "needs_human", "answer": None, "reason": f"manager call failed: {e}", "decided_by": None}

    try:
        decision = parse_decision(raw)
    except Exception as e:  # a pathological reply must not abort the queue pass
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": f"could not read the manager's reply: {e}",
            "decided_by": None,
        }
    if decision is None:
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": "could not parse a decision from the manager's reply",
            "decided_by": None,
        }

    invalid = validate_decision(decision, record)
    if invalid:
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": f"manager returned an unusable decision: {invalid}",
            "decided_by": None,
        }

    if decision["confidence"] != "high":
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": f"manager declined with low confidence: {decision['reason']}",
            "decided_by": None,
        }

    return {"outcome": "answered", "answer": decision["answer"], "reason": decision["reason"], "decided_by": "manager"}


def decide(record: dict, ask_model) -> dict:
    """Route one record, degrading to needs_human on anything at all.

    The specific guards inside _route give a human a useful reason. This outer catch exists for
    what nobody anticipated: records are authored by other models, and an unguarded raise here
    would abort the caller's whole queue pass, silently stranding every record behind it.
    """
    try:
        return _route(record, ask_model)
    except Exception as e:
        return {
            "outcome": "needs_human",
            "answer": None,
            "reason": f"could not route this escalation: {e}",
            "decided_by": None,
        }
