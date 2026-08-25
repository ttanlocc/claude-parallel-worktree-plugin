#!/usr/bin/env python3
"""assert-based checks for the escalation queue and tier classifier. Run: python3 bin/test_escalations.py"""

import json
import os
import tempfile

from escalations import append, new_record, read_all


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return path


def test_new_record_has_required_fields():
    r = new_record("sess-1", "red_tests", "Tests fail, retry or reassign?")
    for key in (
        "id",
        "ts",
        "session_id",
        "kind",
        "question",
        "options",
        "evidence",
        "tier",
        "status",
        "decided_by",
        "answer",
        "answered_at",
    ):
        assert key in r, f"missing {key}"
    assert r["session_id"] == "sess-1"
    assert r["kind"] == "red_tests"
    assert r["status"] == "open"
    assert r["tier"] is None
    assert r["answer"] is None
    assert r["options"] == []
    assert r["evidence"] == {}


def test_new_record_ids_are_unique():
    a = new_record("s", "k", "q")
    b = new_record("s", "k", "q")
    assert a["id"] != b["id"]


def test_append_then_read_all_roundtrips():
    path = _tmp()
    try:
        a = new_record("s1", "k1", "q1")
        b = new_record("s2", "k2", "q2")
        append(path, a)
        append(path, b)
        got = read_all(path)
        assert [r["id"] for r in got] == [a["id"], b["id"]]
        assert got[0]["question"] == "q1"
    finally:
        os.unlink(path)


def test_read_all_missing_file_is_empty():
    assert read_all("/nonexistent/path/queue.jsonl") == []


def test_read_all_skips_malformed_lines():
    path = _tmp()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(new_record("s", "k", "good")) + "\n")
            f.write("not json at all\n")
            f.write("\n")
        got = read_all(path)
        assert len(got) == 1
        assert got[0]["question"] == "good"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
