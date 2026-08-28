#!/usr/bin/env python3
"""Watch the escalation queue: let the manager settle what it can, hand the rest to the human.

Run: manager_daemon.py [queue-path]
"""

import json
import os
import subprocess
import sys
import time
import traceback

import manager_session
from escalations import QUEUE_PATH, append, classify, current_state, record_answer
from manager import build_prompt, decide, deliver_answer

DELIVERY_ATTEMPTS = 3

SEEN_PATH = os.path.expanduser("~/.claude/hermes/manager-seen-sessions.json")
TICK_SECONDS = int(os.environ.get("PWT_MANAGER_TICK_SECONDS", "1800"))
DONE_STATUSES = ("idle", "done", "finished", "stopped")

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def list_agents(run=subprocess.run) -> list[dict]:
    """Every session Claude Code knows about, or an empty list if the call fails.

    Degrading to empty rather than raising keeps a transient CLI failure from aborting the
    escalation pass, which is the more important half of a daemon cycle.
    """
    try:
        proc = run(
            ["claude", "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        agents = json.loads(proc.stdout)
    except SUBPROC_ERRORS:
        return []
    return agents if isinstance(agents, list) else []


def finished_sessions(agents: list[dict], seen: dict) -> tuple[list[dict], dict]:
    """Sessions that just stopped working, and the status map to persist.

    A session absent from `seen` is recorded silently: on a daemon restart, work that finished days
    ago must not be re-announced.
    """
    fired, updated = [], dict(seen)
    for agent in agents:
        sid = agent.get("sessionId")
        if not sid:
            continue
        status = agent.get("status")
        previous = seen.get(sid)
        if previous is not None and previous != status and status in DONE_STATUSES:
            fired.append(agent)
        updated[sid] = status
    return fired, updated


def _read_seen() -> dict:
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_seen(seen: dict) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)


def should_tick(last_tick: float, now: float, open_count: int, running_count: int) -> bool:
    """Tick only when the interval has passed AND there is something to chase.

    An idle dashboard must not burn tokens on a manager with nothing to manage.
    """
    if now - last_tick < TICK_SECONDS:
        return False
    return open_count > 0 or running_count > 0


def ask_via_session(record: dict, ask=manager_session.ask_result) -> str:
    """Put one escalation to the persistent manager and hand back its raw reply.

    Raises on a failed call rather than returning the failure note: the note is ordinary text,
    and letting it reach parse_decision is how a timeout gets read as an approval.
    """
    ok, raw = ask(build_prompt(record), "daemon:escalation")
    if not ok:
        raise RuntimeError(raw)
    return raw


def _try_deliver(path: str, rec: dict, message: str, deliver) -> str:
    """Carry one answer back to its worker, marking it delivered only once it actually landed.

    Marking before delivering is how an answer gets silently lost: the queue reads as delivered
    while the worker is still blocked. After DELIVERY_ATTEMPTS failures the record goes back to a
    human — an undeliverable answer must surface, never vanish.
    """
    try:
        deliver(rec["session_id"], message)
    except Exception as e:
        attempts = int(rec.get("delivery_attempts") or 0) + 1
        update = {**rec, "delivery_attempts": attempts}
        if attempts >= DELIVERY_ATTEMPTS:
            update["status"] = "needs_human"
            update["reason"] = f"could not deliver to {rec.get('session_id', '?')} after {attempts} tries: {e}"
        append(path, update)
        return "delivery_failed"
    append(path, {**rec, "delivered": True})
    return "delivered"


def process_open(path: str, ask_model, deliver) -> list[dict]:
    """One pass. Returns what this pass acted on, so a caller can log or test it."""
    acted = []
    for rec in current_state(path):
        status = rec.get("status")

        if status == "open":
            outcome = decide(rec, ask_model)
            if outcome["outcome"] == "answered":
                updated = record_answer(path, rec["id"], outcome["answer"], "manager")
                _try_deliver(path, updated, outcome["answer"], deliver)
            else:
                pending = dict(rec)
                pending["status"] = "needs_human"
                try:
                    pending["tier"] = classify(rec)[0]
                except Exception:
                    pending["tier"] = "tier3"
                pending["reason"] = outcome["reason"]
                append(path, pending)
            acted.append(outcome)

        elif status == "answered" and not rec.get("delivered"):
            result = _try_deliver(path, rec, rec.get("answer"), deliver)
            acted.append(
                {
                    "outcome": result,
                    "reason": f"{rec.get('session_id', '?')} ← {rec.get('answer', '?')}",
                    "answer": rec.get("answer"),
                    "decided_by": rec.get("decided_by"),
                }
            )

    return acted


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else QUEUE_PATH
    print(f"manager daemon watching {path}")
    last_tick = time.time()
    while True:
        try:
            for outcome in process_open(path, ask_via_session, deliver_answer):
                print(f"  {outcome['outcome']}: {outcome['reason']}")
        except Exception as e:  # a bad pass must not kill the daemon
            print(f"  pass failed: {e}", file=sys.stderr)
            traceback.print_exc()

        try:
            agents = list_agents()
            fired, seen = finished_sessions(agents, _read_seen())
            _write_seen(seen)
            for agent in fired:
                name = agent.get("name") or agent.get("sessionId")
                manager_session.ask(
                    f"Worker '{name}' (session {agent.get('sessionId')}) finished. "
                    "Check its work, update the ledger, and dispatch what comes next.",
                    "daemon:worker-finished",
                )

            running = sum(1 for a in agents if a.get("status") == "busy")
            if should_tick(last_tick, time.time(), len(open_assignments()), running):
                last_tick = time.time()
                manager_session.ask(
                    "Tick. Walk the open assignments: chase anything past its ETA, update each "
                    "note, and write a report if you have not written one in 24 hours.",
                    "daemon:tick",
                )
        except Exception as e:  # a bad wake pass must not kill the daemon
            print(f"  wake pass failed: {e}", file=sys.stderr)

        time.sleep(5)


if __name__ == "__main__":
    main()
