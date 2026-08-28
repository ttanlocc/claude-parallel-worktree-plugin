#!/usr/bin/env python3
"""assert-based checks for the persistent manager session. Run: python3 bin/test_manager_session.py"""

import json
import os as _os
import subprocess
import tempfile
import threading

import manager_session as ms

_CHARTER_BACKUP = ms.CHARTER_PATH


class _Recorder:
    """Stand-in for subprocess.run that records argv and replays a scripted reply."""

    def __init__(self, session_id="sess-1", result="ok", raises=None):
        self.session_id = session_id
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        payload_dict = {"result": self.result}
        if self.session_id is not None:
            payload_dict["session_id"] = self.session_id
        payload = json.dumps(payload_dict)
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")


def _isolate():
    """Point every state path at a fresh temp dir and return it."""
    d = tempfile.mkdtemp()
    ms.STATE_PATH = _os.path.join(d, "state.json")
    ms.CHAT_PATH = _os.path.join(d, "chat.jsonl")
    ms.LOCK_PATH = _os.path.join(d, "lock")
    return d


def test_argv_bootstraps_without_resume():
    argv = ms.ask_argv(None, "hello")
    assert "--resume" not in argv
    assert argv[-2:] == ["-p", "hello"]
    assert "--model" in argv and "--effort" in argv


def test_argv_resumes_with_a_session_id():
    argv = ms.ask_argv("sess-9", "hello")
    assert argv[argv.index("--resume") + 1] == "sess-9"


def test_effort_and_model_are_sent_on_every_call():
    """--effort applies per invocation, so a resume that omits it silently downgrades."""
    for sid in (None, "sess-9"):
        argv = ms.ask_argv(sid, "x")
        assert argv[argv.index("--model") + 1] == ms.MANAGER_MODEL
        assert argv[argv.index("--effort") + 1] == ms.MANAGER_EFFORT


def test_bootstrap_saves_the_session_id_and_prepends_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    reply = ms.ask("first message", "cto", run=run)
    assert reply == "ok"
    assert json.load(open(ms.STATE_PATH))["session_id"] == "fresh-1"
    sent = run.calls[0][-1]
    assert "first message" in sent
    assert "Engineering Manager" in sent, "charter must be prepended to the first message"


def test_second_call_resumes_and_does_not_resend_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    ms.ask("first", "cto", run=run)
    ms.ask("second", "cto", run=run)
    second = run.calls[1]
    assert "--resume" in second
    assert second[-1] == "second", "the charter must not be resent on a resume"


def test_both_turns_reach_the_chat_log_with_their_source():
    _isolate()
    ms.ask("wake up", "daemon:tick", run=_Recorder(result="on it"))
    entries = ms.history()
    assert [e["role"] for e in entries] == ["system", "manager"]
    assert all(e["source"] == "daemon:tick" for e in entries)
    assert entries[1]["text"] == "on it"


def test_a_cto_turn_is_logged_as_cto():
    _isolate()
    ms.ask("status?", "cto", run=_Recorder())
    assert ms.history()[0]["role"] == "cto"


def test_a_failing_call_is_recorded_not_swallowed():
    _isolate()
    reply = ms.ask("x", "cto", run=_Recorder(raises=subprocess.TimeoutExpired("claude", 600)))
    assert "failed" in reply.lower()
    entries = ms.history()
    assert len(entries) == 2
    assert entries[0]["text"] == "x"
    assert entries[-1]["role"] == "manager"
    assert "failed" in entries[-1]["text"].lower()


def test_a_failing_call_releases_the_lock():
    _isolate()
    ms.ask("x", "cto", run=_Recorder(raises=OSError("boom")))
    assert ms.busy() is False


def test_busy_is_false_when_idle():
    _isolate()
    assert ms.busy() is False


def test_reset_drops_the_session_so_the_next_call_bootstraps():
    _isolate()
    run = _Recorder(session_id="a")
    ms.ask("one", "cto", run=run)
    ms.reset()
    assert not _os.path.exists(ms.STATE_PATH)
    run2 = _Recorder(session_id="b")
    ms.ask("two", "cto", run=run2)
    assert "--resume" not in run2.calls[0]
    assert json.load(open(ms.STATE_PATH))["session_id"] == "b"


def test_history_respects_its_limit_and_returns_the_newest():
    _isolate()
    run = _Recorder()
    for i in range(5):
        ms.ask(f"m{i}", "cto", run=run)
    tail = ms.history(limit=3)
    assert len(tail) == 3
    assert tail[-1]["role"] == "manager"


def test_charter_has_its_load_bearing_sections():
    text = ms.load_charter()
    for heading in ("## The eight actions", "## Routing work", "## Hard boundaries"):
        assert heading in text, heading


def test_a_broken_chat_log_does_not_cost_the_caller_its_reply():
    d = _isolate()
    blocker = _os.path.join(d, "blocker")
    open(blocker, "w").close()
    ms.CHAT_PATH = _os.path.join(blocker, "chat.jsonl")
    reply = ms.ask("x", "cto", run=_Recorder())
    assert reply == "ok", "a broken chat log must not cost the caller its reply"


def test_a_failed_state_write_does_not_discard_the_reply():
    d = _isolate()
    blocker = _os.path.join(d, "sblock")
    open(blocker, "w").close()
    ms.STATE_PATH = _os.path.join(blocker, "state.json")
    reply = ms.ask("x", "cto", run=_Recorder())
    assert reply == "ok", "a bookkeeping failure must not discard a reply already received"
    assert any("could not save the session id" in e["text"] for e in ms.history())


def test_a_second_caller_gets_manager_busy_while_the_first_holds_the_lock():
    _isolate()
    ms.LOCK_TIMEOUT = 1
    started, release = threading.Event(), threading.Event()

    def slow_run(argv, **kwargs):
        started.set()
        release.wait(5)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"session_id": "s", "result": "ok"}), stderr="")

    first = threading.Thread(target=ms.ask, args=("first", "cto"), kwargs={"run": slow_run})
    first.start()
    try:
        assert started.wait(5), "the first call never reached the subprocess"
        assert ms.busy() is True
        try:
            ms.ask("second", "cto", run=_Recorder())
        except ms.ManagerBusy:
            pass
        else:
            raise AssertionError("a second caller should have hit ManagerBusy")
        release.set()
        first.join(5)
        assert ms.busy() is False
    finally:
        ms.LOCK_TIMEOUT = 300


def test_a_busy_timeout_is_recorded_before_it_raises():
    _isolate()
    ms.LOCK_TIMEOUT = 1
    started, release = threading.Event(), threading.Event()

    def slow_run(argv, **kwargs):
        started.set()
        release.wait(5)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"session_id": "s", "result": "ok"}), stderr="")

    first = threading.Thread(target=ms.ask, args=("first", "cto"), kwargs={"run": slow_run})
    first.start()
    try:
        assert started.wait(5)
        try:
            ms.ask("lost one", "cto", run=_Recorder())
        except ms.ManagerBusy:
            pass
        release.set()
        first.join(5)
        assert any("busy" in e["text"] for e in ms.history()), "a dropped turn must be visible in the log"
    finally:
        ms.LOCK_TIMEOUT = 300


def test_a_reply_without_a_session_id_warns_instead_of_silently_rebootstrapping():
    _isolate()
    run = _Recorder()
    run.session_id = None
    reply = ms.ask("first", "cto", run=run)
    assert reply == "ok"
    assert any("without a session id" in e["text"] for e in ms.history())


def test_a_non_dict_payload_is_reported_not_raised():
    _isolate()

    def weird(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[1, 2, 3]", stderr="")

    reply = ms.ask("x", "cto", run=weird)
    assert "failed" in reply.lower()
    assert ms.history()[-1]["role"] == "manager"


def test_a_missing_charter_is_reported_not_raised():
    d = _isolate()
    ms.CHARTER_PATH = _os.path.join(d, "nope.md")
    try:
        reply = ms.ask("x", "cto", run=_Recorder())
        assert "failed" in reply.lower()
        assert ms.history()[0]["text"] == "x", "the incoming turn must survive the failure"
    finally:
        ms.CHARTER_PATH = _CHARTER_BACKUP


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
