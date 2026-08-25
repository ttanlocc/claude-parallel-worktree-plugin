#!/usr/bin/env python3
"""assert-based checks for manager prompt building, decision parsing, and argv. Run: python3 bin/test_manager.py"""

from escalations import new_record
from manager import (
    MANAGER_MODEL,
    build_prompt,
    manager_argv,
    parse_decision,
    resume_argv,
    validate_decision,
)


def test_prompt_contains_question_and_options():
    r = new_record("s1", "red_tests", "Retry or reassign?", options=["retry", "reassign"])
    p = build_prompt(r)
    assert "Retry or reassign?" in p
    assert "retry" in p and "reassign" in p
    assert "JSON" in p


def test_prompt_includes_evidence():
    r = new_record("s1", "diff_review", "merge?", evidence={"tests": "green", "branch": "feature/x"})
    p = build_prompt(r)
    assert "green" in p
    assert "feature/x" in p


def test_parse_decision_plain_json():
    got = parse_decision('{"answer": "retry", "reason": "flaky", "confidence": "high"}')
    assert got == {"answer": "retry", "reason": "flaky", "confidence": "high"}


def test_parse_decision_json_in_fenced_block():
    text = 'Here is my call:\n```json\n{"answer": "retry", "reason": "r", "confidence": "high"}\n```\nthanks'
    got = parse_decision(text)
    assert got["answer"] == "retry"


def test_parse_decision_json_embedded_in_prose():
    text = 'I think {"answer": "reassign", "reason": "stuck", "confidence": "low"} is right.'
    got = parse_decision(text)
    assert got["answer"] == "reassign"
    assert got["confidence"] == "low"


def test_parse_decision_garbage_returns_none():
    assert parse_decision("I cannot help with that.") is None
    assert parse_decision("") is None
    assert parse_decision("{not json}") is None


def test_validate_decision_accepts_well_formed():
    r = new_record("s", "red_tests", "q")
    assert validate_decision({"answer": "go", "reason": "why", "confidence": "high"}, r) is None


def test_validate_decision_rejects_missing_fields():
    r = new_record("s", "red_tests", "q")
    assert validate_decision({"answer": "go"}, r) is not None
    assert validate_decision({"reason": "x", "confidence": "high"}, r) is not None


def test_validate_decision_rejects_bad_confidence():
    r = new_record("s", "red_tests", "q")
    err = validate_decision({"answer": "a", "reason": "b", "confidence": "maybe"}, r)
    assert err is not None
    assert "confidence" in err


def test_validate_decision_rejects_answer_outside_options():
    r = new_record("s", "pick_implementation", "which?", options=["A", "B"])
    err = validate_decision({"answer": "C", "reason": "r", "confidence": "high"}, r)
    assert err is not None
    assert "option" in err.lower()
    assert validate_decision({"answer": "A", "reason": "r", "confidence": "high"}, r) is None


def test_validate_decision_rejects_non_dict():
    r = new_record("s", "red_tests", "q")
    assert validate_decision(None, r) is not None
    assert validate_decision(["a"], r) is not None


def test_manager_argv_uses_fable_and_print_mode():
    argv = manager_argv("hello")
    assert argv[0] == "claude"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == MANAGER_MODEL
    assert "-p" in argv
    assert argv[-1] == "hello"


def test_resume_argv_targets_the_session():
    argv = resume_argv("sess-abc", "the answer")
    assert argv[0] == "claude"
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-abc"
    assert "-p" in argv
    assert argv[-1] == "the answer"


def test_parse_decision_handles_nested_objects():
    text = '{"answer": "retry", "reason": "flaky", "confidence": "high", "meta": {"attempt": 2}}'
    got = parse_decision(text)
    assert got is not None, "a nested field must not destroy the whole decision"
    assert got["answer"] == "retry"
    assert got["meta"] == {"attempt": 2}


def test_parse_decision_skips_a_decoy_object():
    text = (
        'Echoing the record: {"kind": "red_tests", "question": "retry?", "answer": null}\n'
        'My decision: {"answer": "retry once", "reason": "transient", "confidence": "high"}'
    )
    got = parse_decision(text)
    assert got["answer"] == "retry once", "should prefer the object that looks like a decision"
    assert got["confidence"] == "high"


def test_parse_decision_handles_nested_inside_prose_and_fences():
    text = (
        'Result:\n```json\n{"answer": "A", "reason": "r", "confidence": "high", "evidence": {"tests": "green"}}\n```\n'
    )
    got = parse_decision(text)
    assert got is not None
    assert got["evidence"]["tests"] == "green"


from manager import decide


def _ok(payload):
    return lambda record: payload


def test_tier3_record_never_calls_the_model():
    called = []

    def spy(record):
        called.append(record)
        return '{"answer": "x", "reason": "y", "confidence": "high"}'

    r = new_record("s", "push_or_pr", "push to main?")
    out = decide(r, spy)
    assert out["outcome"] == "needs_human"
    assert called == [], "tier3 must not spend a model call"
    assert "human" in out["reason"].lower() or "push_or_pr" in out["reason"]


def test_tier2_high_confidence_is_answered():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok('{"answer": "retry once", "reason": "looks flaky", "confidence": "high"}'))
    assert out["outcome"] == "answered"
    assert out["answer"] == "retry once"
    assert out["decided_by"] == "manager"


def test_tier2_low_confidence_falls_back_to_human():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok('{"answer": "maybe", "reason": "unclear", "confidence": "low"}'))
    assert out["outcome"] == "needs_human"
    assert "confidence" in out["reason"].lower()


def test_tier2_unparseable_reply_falls_back_to_human():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok("I'm not sure what you mean."))
    assert out["outcome"] == "needs_human"
    assert "pars" in out["reason"].lower()


def test_tier2_invalid_decision_falls_back_to_human():
    r = new_record("s", "pick_implementation", "which?", options=["A", "B"])
    out = decide(r, _ok('{"answer": "C", "reason": "r", "confidence": "high"}'))
    assert out["outcome"] == "needs_human"
    assert "option" in out["reason"].lower()


def test_model_failure_falls_back_to_human():
    def boom(record):
        raise RuntimeError("model unavailable")

    r = new_record("s", "red_tests", "retry?")
    out = decide(r, boom)
    assert out["outcome"] == "needs_human"
    assert "model unavailable" in out["reason"]


def test_clean_diff_gets_approved_by_manager():
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
    out = decide(r, _ok('{"answer": "approve", "reason": "clean", "confidence": "high"}'))
    assert out["outcome"] == "answered"
    assert out["answer"] == "approve"


def test_diff_touching_auth_reaches_human_without_model_call():
    called = []
    r = new_record(
        "s",
        "diff_review",
        "merge?",
        evidence={
            "tests": "green",
            "deps_added": [],
            "migration": False,
            "changed_files": ["services/gateway/auth/token.py"],
        },
    )
    out = decide(r, lambda rec: called.append(rec) or '{"answer":"a","reason":"b","confidence":"high"}')
    assert out["outcome"] == "needs_human"
    assert called == []


def test_parser_crash_falls_back_to_human():
    def pathological(record):
        return '{"a":' * 20000 + "1" + "}" * 20000

    r = new_record("s", "red_tests", "retry?")
    out = decide(r, pathological)
    assert out["outcome"] == "needs_human"
    assert out["decided_by"] is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
