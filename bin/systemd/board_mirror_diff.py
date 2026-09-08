#!/usr/bin/env python3
"""Cut board_state.py's full write set down to what actually changed since the last successful
run, and turn documents that disappeared into `delete` ops.

Pure transforms only, like board_state.py itself — no I/O in diff_writes()/snapshot_from_writes(),
so this is testable without a real snapshot file. main() below is the only I/O, called twice by
run-board-mirror.sh: once (mode "diff") before the write, once (mode "snapshot") only after the
write actually succeeds. Snapshotting before a write that then fails would make the next run
believe it's already synced and silently skip real changes forever — that ordering lives in
run-board-mirror.sh, not here, but it's the whole reason "diff" and "snapshot" are separate calls
instead of one that does both.
"""

import json
import os
import sys


def _key(entry: dict) -> str:
    return f"{entry['collection']}/{entry['doc_id']}"


def diff_writes(previous: dict, writes: list[dict]) -> list[dict]:
    """previous: {"collection/doc_id": data} from the last successful run's snapshot, or {} if
    there isn't one yet (first run, or a snapshot that failed to save last time).

    writes: this run's full "set" list from board_state.py, one entry per document that exists
    right now, meta/status last.

    Returns what to actually send: unchanged "set" entries dropped, a "delete" for every
    previously-known doc_id no longer in `writes` at all, and meta/status always kept and always
    last — it's the refresh timestamp, so "unchanged" never applies to it.
    """
    if not previous:
        return list(writes)

    meta = writes[-1]
    rest = writes[:-1]
    # Includes meta's own key, not just rest's — otherwise meta/status (always real, never
    # "gone") would look like a doc that disappeared and get turned into a spurious delete.
    current_keys = {_key(w) for w in writes}

    changed = [w for w in rest if previous.get(_key(w)) != w["data"]]
    deletes = [
        {"op": "delete", "collection": key.split("/", 1)[0], "doc_id": key.split("/", 1)[1]}
        for key in previous
        if key not in current_keys
    ]
    return changed + deletes + [meta]


def snapshot_from_writes(writes: list[dict]) -> dict:
    """The full current state, keyed for next run's diff — every doc that exists now, not just
    the ones this run happened to change."""
    return {_key(w): w["data"] for w in writes}


def main() -> int:
    """CLI, both modes read the full write-set JSON array from stdin:

    `board_mirror_diff.py diff <snapshot-path>` prints the entries to actually send (JSON array)
    to stdout. A missing snapshot file is treated as "no previous run" (previous={}), same as an
    empty one — both mean "send everything", which is also correct behavior on a first run.

    `board_mirror_diff.py snapshot <snapshot-path>` overwrites the snapshot with this run's full
    state. Written via a temp file + rename so a crash mid-write can't leave a half-written,
    unparseable snapshot for the next run to trip over.
    """
    if len(sys.argv) != 3 or sys.argv[1] not in ("diff", "snapshot"):
        print("usage: board_mirror_diff.py {diff|snapshot} <snapshot-path>", file=sys.stderr)
        return 1
    mode, path = sys.argv[1], sys.argv[2]
    writes = json.load(sys.stdin)

    if mode == "diff":
        try:
            with open(path, encoding="utf-8") as f:
                previous = json.load(f)
        except (FileNotFoundError, ValueError):
            previous = {}
        json.dump(diff_writes(previous, writes), sys.stdout, ensure_ascii=False)
        return 0

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot_from_writes(writes), f, ensure_ascii=False)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
