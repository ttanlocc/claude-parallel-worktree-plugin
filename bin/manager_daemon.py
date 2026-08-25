#!/usr/bin/env python3
"""Watch the escalation queue: let the manager settle what it can, hand the rest to the human.

Run: manager_daemon.py [queue-path]
"""

import sys
import time
import traceback

from escalations import QUEUE_PATH, append, classify, current_state, record_answer
from manager import decide, deliver_answer, run_manager

DELIVERY_ATTEMPTS = 3


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
            update["reason"] = f"could not deliver to {rec['session_id']} after {attempts} tries: {e}"
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
            result = _try_deliver(path, rec, rec["answer"], deliver)
            acted.append(
                {
                    "outcome": result,
                    "reason": f"{rec['session_id']} ← {rec['answer']}",
                    "answer": rec["answer"],
                    "decided_by": rec.get("decided_by"),
                }
            )

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
            traceback.print_exc()
        time.sleep(5)


if __name__ == "__main__":
    main()
