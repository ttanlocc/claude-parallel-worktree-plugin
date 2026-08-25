#!/usr/bin/env python3
"""Watch the escalation queue: let the manager settle what it can, hand the rest to the human.

Run: manager_daemon.py [queue-path]
"""

import sys
import time

from escalations import QUEUE_PATH, append, current_state, record_answer
from manager import decide, deliver_answer, run_manager


def process_open(path: str, ask_model, deliver) -> list[dict]:
    """One pass. Returns what this pass acted on, so a caller can log or test it."""
    acted = []
    for rec in current_state(path):
        status = rec.get("status")

        if status == "open":
            outcome = decide(rec, ask_model)
            if outcome["outcome"] == "answered":
                record_answer(path, rec["id"], outcome["answer"], "manager")
                deliver(rec["session_id"], outcome["answer"])
            else:
                pending = dict(rec)
                pending["status"] = "needs_human"
                pending["tier"] = "tier3"
                pending["reason"] = outcome["reason"]
                append(path, pending)
            acted.append(outcome)

        elif status == "answered" and not rec.get("delivered"):
            # A human answered through the dashboard; carry it back to the worker exactly once.
            if rec.get("decided_by") == "human":
                deliver(rec["session_id"], rec["answer"])
            done = dict(rec)
            done["delivered"] = True
            append(path, done)

    return acted


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else QUEUE_PATH
    print(f"manager daemon watching {path}")
    while True:
        try:
            for outcome in process_open(path, run_manager, deliver_answer):
                print(f"  {outcome['outcome']}: {outcome['reason']}")
        except Exception as e:  # a bad pass must not kill the daemon
            print(f"  pass failed: {e}", file=sys.stderr)
        time.sleep(5)


if __name__ == "__main__":
    main()
