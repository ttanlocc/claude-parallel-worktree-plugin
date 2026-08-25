#!/usr/bin/env python3
"""assert-based checks for the escalation queue and tier classifier. Run: python3 bin/test_escalations.py"""

import json
import os
import tempfile

from escalations import append, classify, new_record, read_all


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


def test_tier2_mechanical_kinds():
    for kind in ("red_tests", "looping", "pick_implementation", "scope_question"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier2", f"{kind} should be tier2, got {tier} ({reason})"
        assert reason


def test_tier3_always_human_kinds():
    for kind in ("irreversible", "push_or_pr", "credentials", "spec_ambiguity", "no_convergence", "worktree_collision"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier3", f"{kind} should be tier3, got {tier} ({reason})"
        assert reason


def test_clean_diff_is_tier2():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "changed_files": ["bin/dashboard.py"],
            "migration": False,
        },
    )
    tier, reason = classify(r)
    assert tier == "tier2", reason


def test_diff_with_red_tests_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "red",
            "deps_added": [],
            "changed_files": ["bin/dashboard.py"],
            "migration": False,
        },
    )
    assert classify(r)[0] == "tier3"


def test_diff_adding_dependency_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": ["requests"],
            "changed_files": ["bin/x.py"],
            "migration": False,
        },
    )
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "depend" in reason.lower()


def test_diff_with_migration_is_tier3():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "changed_files": ["bin/x.py"],
            "migration": True,
        },
    )
    assert classify(r)[0] == "tier3"


def test_diff_touching_sensitive_path_is_tier3():
    for path in ("services/gateway/auth/token.py", "config/.env", "app/secrets.yaml", "migrations/0042_add.py"):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={
                "tests": "green",
                "deps_added": [],
                "changed_files": ["bin/ok.py", path],
                "migration": False,
            },
        )
        tier, reason = classify(r)
        assert tier == "tier3", f"{path} should force tier3 ({reason})"


def test_tier3_wins_ties():
    # A mechanical kind that nonetheless carries an irreversible flag stays tier3.
    r = new_record("s", "red_tests", "q", evidence={"irreversible": True})
    assert classify(r)[0] == "tier3"


def test_unknown_kind_defaults_to_tier3():
    tier, reason = classify(new_record("s", "something_new", "q"))
    assert tier == "tier3"
    assert "unknown" in reason.lower()


def test_diff_must_disclose_every_gated_field():
    # Only tests disclosed — the other three are simply absent.
    r = new_record("s", "diff_review", "merge?", evidence={"tests": "green"})
    tier, reason = classify(r)
    assert tier == "tier3", "an undisclosed field must not read as compliance"
    assert "disclose" in reason

    for missing in ("deps_added", "migration", "changed_files"):
        ev = {"tests": "green", "deps_added": [], "migration": False, "changed_files": ["bin/x.py"]}
        del ev[missing]
        tier, reason = classify(new_record("s", "diff_review", "merge?", evidence=ev))
        assert tier == "tier3", f"missing {missing} must force tier3"
        assert missing in reason


def test_changed_files_as_string_still_scanned():
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "migration": False,
            "changed_files": "services/gateway/auth/token.py",
        },
    )
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "sensitive" in reason


def test_tier3_evidence_beats_any_tier2_kind():
    for ev, why in (
        ({"deps_added": ["requests"]}, "dependency"),
        ({"migration": True}, "migration"),
        ({"changed_files": ["services/auth/token.py"]}, "sensitive path"),
    ):
        for kind in ("red_tests", "looping", "pick_implementation", "scope_question"):
            tier, reason = classify(new_record("s", kind, "q", evidence=ev))
            assert tier == "tier3", f"{kind} + {why} must be tier3, got {reason}"


def test_secret_and_migration_shapes_are_sensitive():
    for path in (
        "deploy/private_key.pem",
        "infra/id_rsa",
        "etc/passwd",
        "app/.netrc",
        "app/.npmrc",
        "keystore/prod.jks",
        "db/schema.sql",
        "alembic/versions/0042_add.py",
    ):
        r = new_record(
            "s",
            "diff_review",
            "merge?",
            evidence={
                "tests": "green",
                "deps_added": [],
                "migration": False,
                "changed_files": ["bin/ok.py", path],
            },
        )
        tier, reason = classify(r)
        assert tier == "tier3", f"{path} should be sensitive ({reason})"


def test_cost_anomaly_names_itself():
    tier, reason = classify(new_record("s", "cost_anomaly", "spend spiked"))
    assert tier == "tier3"
    assert "cost_anomaly" in reason
    assert "unknown" not in reason.lower()


def test_deps_added_as_string_reads_cleanly():
    r = new_record("s", "diff_review", "merge?", evidence={"deps_added": "requests"})
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "requests" in reason
    assert "r, e, q" not in reason


def test_explicitly_null_evidence_is_not_disclosure():
    for null_key in ("deps_added", "migration", "changed_files"):
        ev = {"tests": "green", "deps_added": [], "migration": False, "changed_files": ["bin/x.py"]}
        ev[null_key] = None
        tier, reason = classify(new_record("s", "diff_review", "merge?", evidence=ev))
        assert tier == "tier3", f"{null_key}=None must not read as disclosed"
        assert null_key in reason


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
