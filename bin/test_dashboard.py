#!/usr/bin/env python3
"""assert-based checks for dashboard.py's transcript rendering. Run: python3 bin/test_dashboard.py"""

import json
import os
import subprocess
import tempfile

import dashboard
from dashboard import (
    _find_ado_links,
    _shape_ado_ticket,
    get_ado_backlog,
    render_task_log,
    render_transcript_line,
)


def test_skips_non_conversation_lines():
    assert render_transcript_line({"type": "custom-title", "customTitle": "x"}) == []
    assert render_transcript_line({"type": "last-prompt", "lastPrompt": "x"}) == []
    assert render_transcript_line({"type": "attachment", "attachment": {}}) == []
    assert render_transcript_line({"type": "mode", "mode": "normal"}) == []


def test_renders_plain_string_user_content():
    obj = {"type": "user", "message": {"role": "user", "content": "hello"}}
    assert render_transcript_line(obj) == [{"kind": "text", "role": "user", "tool": None, "text": "hello"}]


def test_renders_assistant_text():
    obj = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "DONE"}]}}
    assert render_transcript_line(obj) == [{"kind": "text", "role": "assistant", "tool": None, "text": "DONE"}]


def test_renders_tool_use_prefers_command_arg():
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}],
        },
    }
    assert render_transcript_line(obj) == [
        {"kind": "call", "role": "assistant", "tool": "Bash", "text": "echo hi", "call_id": "t1"}
    ]


def test_renders_tool_use_falls_back_to_json_input():
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "x"}}],
        },
    }
    lines = render_transcript_line(obj)
    assert lines[0]["tool"] == "Grep"
    assert lines[0]["text"] == '{"pattern": "x"}'


def test_renders_tool_result_string_content():
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]},
    }
    assert render_transcript_line(obj) == [
        {"kind": "result", "role": "user", "tool": None, "text": "hi", "result_for": "t1"}
    ]


def test_renders_tool_result_block_content():
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": [{"type": "text", "text": "block result"}]}],
        },
    }
    assert render_transcript_line(obj) == [
        {"kind": "result", "role": "user", "tool": None, "text": "block result", "result_for": None}
    ]


def test_render_task_log_reads_file_and_applies_limit():
    records = [{"type": "custom-title"}] + [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"line{i}"}]}}
        for i in range(5)
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        path = f.name
    try:
        result = render_task_log(path, limit=3)
        assert [r["text"] for r in result] == ["line2", "line3", "line4"]
    finally:
        os.unlink(path)


def test_render_task_log_skips_malformed_lines():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(
            '{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}\n'
        )
        f.write("not json at all\n")
        f.write("\n")
        path = f.name
    try:
        assert [r["text"] for r in render_task_log(path)] == ["ok"]
    finally:
        os.unlink(path)


def test_redacts_credential_looking_call_and_its_result():
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t9",
                        "name": "Bash",
                        "input": {"command": "grep -oP '(?<=personal access token = ).*' creds.txt"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "ghp_supersecrettoken"}],
            },
        },
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        path = f.name
    try:
        result = render_task_log(path)
        assert result[0]["text"] == "[redacted — this call touches a credential]"
        assert result[1]["text"] == "[redacted — this call touches a credential]"
        assert "call_id" not in result[0] and "result_for" not in result[1]
    finally:
        os.unlink(path)


def test_truncates_long_lines():
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]}}
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        path = f.name
    try:
        result = render_task_log(path)
        assert len(result[0]["text"]) == 401  # 400 chars + ellipsis
        assert result[0]["text"].endswith("…")
    finally:
        os.unlink(path)


def test_find_ado_links_returns_all_matches_deduplicated():
    text = (
        "See (AB#1) and again AB#1, but really [AB#7160](https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160)"
    )
    refs = _find_ado_links(text)
    assert refs == [
        {"id": "1", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/1"},
        {"id": "7160", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160"},
    ]


def test_find_ado_links_full_url_wins_over_bare_ref_for_same_id():
    text = "(AB#7160) ... [AB#7160](https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160)"
    refs = _find_ado_links(text)
    assert refs == [{"id": "7160", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160"}]


def test_find_ado_links_empty_when_absent():
    assert _find_ado_links("just a normal PR title") == []


def test_enrich_merges_registry_ado_ids_with_pr_scraped_refs():
    original_lookup = dashboard._lookup_pr_and_ticket
    original_branch = dashboard._git_branch
    dashboard._git_branch = lambda cwd: "irrelevant"
    dashboard._lookup_pr_and_ticket = lambda branch: {
        "pr_number": 42,
        "pr_url": "https://github.com/x/y/pull/42",
        "pr_state": "OPEN",
        "ado_refs": [{"id": "8165", "url": "https://different-org.dev.azure.com/other/project/_workitems/edit/8165"}],
    }
    try:
        result = dashboard._enrich_branch_and_links("some/path", "feature/x", known_ado_ids=["8172", "8165"])
    finally:
        dashboard._lookup_pr_and_ticket = original_lookup
        dashboard._git_branch = original_branch
    ids = sorted(r["id"] for r in result["ado_refs"])
    assert ids == ["8165", "8172"]
    assert result["pr_number"] == 42
    # Verify registry-first precedence: 8165 should use registry-constructed URL, not PR-scraped one
    assert (
        next(r for r in result["ado_refs"] if r["id"] == "8165")["url"]
        == "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/8165"
    )


def test_git_branch_degrades_on_timeout():
    original_run = subprocess.run

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    subprocess.run = mock_run
    try:
        result = dashboard._git_branch("/some/path")
        assert result is None
    finally:
        subprocess.run = original_run


def test_lookup_pr_and_ticket_degrades_on_timeout():
    original_run = subprocess.run

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("gh", 10)

    subprocess.run = mock_run
    try:
        result = dashboard._lookup_pr_and_ticket("feature/x")
        assert result == {"pr_number": None, "pr_url": None, "pr_state": None, "ado_refs": []}
    finally:
        subprocess.run = original_run


def test_shape_ado_ticket_extracts_known_fields():
    raw = {
        "id": 8148,
        "fields": {
            "System.Id": 8148,
            "System.State": "New",
            "System.Title": "Confirm agent run/trace tracked fields",
        },
    }
    assert _shape_ado_ticket(raw) == {
        "id": "8148",
        "title": "Confirm agent run/trace tracked fields",
        "state": "New",
        "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/8148",
    }


def test_shape_ado_ticket_handles_missing_fields():
    assert _shape_ado_ticket({"id": 1, "fields": {}}) == {
        "id": "1",
        "title": "",
        "state": "",
        "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/1",
    }


def test_get_ado_backlog_degrades_on_timeout():
    original_run = subprocess.run
    dashboard._CACHE.pop("ado_backlog", None)  # Clear cache so test runs fresh

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("az", 20)

    subprocess.run = mock_run
    try:
        result = get_ado_backlog()
        assert result == []
    finally:
        subprocess.run = original_run


def test_start_manager_turn_returns_before_the_model_does():
    """A model call must never be held inside an HTTP request."""
    import threading
    import time as _time

    import dashboard

    released = threading.Event()

    def slow_ask(text, source):
        _time.sleep(0.3)
        released.set()
        return "done"

    began = _time.monotonic()
    thread = dashboard.start_manager_turn("hello", "cto", ask=slow_ask)
    assert _time.monotonic() - began < 0.2, "start_manager_turn blocked on the model"
    thread.join(timeout=5)
    assert released.is_set()


def test_start_manager_turn_swallows_exceptions_from_ask():
    """ManagerBusy (raised when ask()'s lock-wait itself times out) or any other failure inside
    the background thread must not escape as an unhandled-thread traceback — manager_session.ask
    already logs the failure to the chat itself before re-raising."""
    import contextlib
    import io

    import dashboard

    def boom(text, source):
        raise RuntimeError("boom")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        thread = dashboard.start_manager_turn("hello", "cto", ask=boom)
        thread.join(timeout=5)
    assert "manager turn failed: boom" in stderr.getvalue()


def test_manager_chat_payload_shape():
    import dashboard

    payload = dashboard.manager_chat_payload(
        history=lambda limit=200: [{"ts": 1.0, "role": "cto", "source": "cto", "text": "hi"}],
        busy=lambda: True,
    )
    assert payload["busy"] is True
    assert payload["entries"][0]["role"] == "cto"


def test_manager_chat_payload_degrades_when_history_fails():
    import dashboard

    def boom(limit=200):
        raise OSError("no such file")

    payload = dashboard.manager_chat_payload(history=boom, busy=lambda: False)
    assert payload["entries"] == []
    assert payload["busy"] is False


def test_manager_chat_post_returns_503_and_spawns_nothing_when_busy():
    """The naive `except manager_session.ManagerBusy` around start_manager_turn is dead code: the
    background thread is already spawned and the call has already returned by the time ManagerBusy
    could fire inside it, so nothing on the request thread can ever catch it there. Checking
    busy() before spawning is what makes a 503 actually reachable."""
    import dashboard

    spawned = []
    status, body = dashboard._manager_chat_post(
        "hello", busy=lambda: True, start=lambda text, source: spawned.append((text, source))
    )
    assert status == 503
    assert body == {"error": "manager busy"}
    assert spawned == []


def test_manager_chat_post_spawns_when_not_busy():
    import dashboard

    spawned = []
    status, body = dashboard._manager_chat_post(
        "hello", busy=lambda: False, start=lambda text, source: spawned.append((text, source))
    )
    assert status == 202
    assert body == {"ok": True}
    assert spawned == [("hello", "cto")]


def test_concurrent_posts_spawn_only_one_manager_turn():
    """The busy-check and the spawn must be atomic, or a burst pays for N Opus calls."""
    import threading as _t
    import time as _time

    import dashboard

    spawned = []
    state = {"busy": False}
    gate = _t.Barrier(8)
    statuses = []

    def fake_busy():
        return state["busy"]

    def fake_start(text, source):
        _time.sleep(0.02)  # widen the check-then-act window so an unlocked impl reliably loses
        state["busy"] = True  # stands in for the real session lock being taken
        spawned.append(text)

    def one():
        gate.wait(5)
        statuses.append(dashboard._manager_chat_post("hi", busy=fake_busy, start=fake_start)[0])

    threads = [_t.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(spawned) == 1, spawned
    assert statuses.count(202) == 1, statuses
    assert statuses.count(503) == 7, statuses


def test_read_json_body_flags_oversized_body_as_413():
    """The one status code that must survive the body-reading extraction unchanged: a route-level
    `except ValueError: status=400` would silently flatten this to 400 for every POST route."""
    import io
    import types

    import dashboard

    fake = types.SimpleNamespace(headers={"Content-Length": str(dashboard.MAX_BODY_BYTES + 1)}, rfile=io.BytesIO(b""))
    try:
        dashboard.Handler._read_json_body(fake)
        raise AssertionError("expected _BodyTooLarge")
    except dashboard._BodyTooLarge as e:
        assert str(e) == "request body too large"


def test_read_json_body_rejects_non_dict_json():
    import io
    import types

    import dashboard

    payload = b"[1, 2, 3]"
    fake = types.SimpleNamespace(headers={"Content-Length": str(len(payload))}, rfile=io.BytesIO(payload))
    try:
        dashboard.Handler._read_json_body(fake)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert not isinstance(e, dashboard._BodyTooLarge)
        assert str(e) == "bad request body: body must be a JSON object"


def test_read_json_body_parses_valid_object():
    import io
    import types

    import dashboard

    payload = json.dumps({"text": "hi"}).encode()
    fake = types.SimpleNamespace(headers={"Content-Length": str(len(payload))}, rfile=io.BytesIO(payload))
    assert dashboard.Handler._read_json_body(fake) == {"text": "hi"}


def test_assignments_post_rejects_bare_string_ado_refs():
    """The most likely caller mistake — the retired route used `ticket_ids: list[str]` — must
    produce a 400, not a dropped connection after an unrecoverable ledger write."""
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "ado_refs": "8172"}, append_fn=appended.append, start=lambda *a: None
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_non_list_ado_refs():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "ado_refs": 8172}, append_fn=appended.append, start=lambda *a: None
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_ado_ref_missing_id():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "ado_refs": [{"url": "https://example.com"}]},
        append_fn=appended.append,
        start=lambda *a: None,
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_ado_ref_with_non_string_id():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "ado_refs": [{"id": 8172, "url": "https://example.com"}]},
        append_fn=appended.append,
        start=lambda *a: None,
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_normalises_ado_refs_and_defaults_missing_url():
    import dashboard

    appended = []
    started = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "ado_refs": [{"id": "8172", "url": "https://x/8172"}, {"id": "8165"}]},
        append_fn=appended.append,
        start=lambda text, source: started.append((text, source)),
    )
    assert status == 202
    assert resp["record"]["ado_refs"] == [
        {"id": "8172", "url": "https://x/8172"},
        {"id": "8165", "url": ""},
    ]
    assert appended == [resp["record"]]
    assert len(started) == 1


def test_assignments_post_rejects_blank_title():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post({"title": "   "}, append_fn=appended.append, start=lambda *a: None)
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_out_of_range_priority():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": "fix it", "priority": "P9"}, append_fn=appended.append, start=lambda *a: None
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_non_string_title_int():
    """The sibling bug to malformed ado_refs: {"title": 12345} is valid JSON and reaches
    `title.strip()` inside new_assignment unless caught first."""
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post({"title": 12345}, append_fn=appended.append, start=lambda *a: None)
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_rejects_non_string_title_list():
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(
        {"title": ["not", "a", "string"]}, append_fn=appended.append, start=lambda *a: None
    )
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_falsy_non_string_title_still_blank_not_type_error():
    """0 and False must still take the `or ""` fallback — the existing "title is required"
    message from new_assignment — not the new "title must be a string" branch."""
    import dashboard

    for falsy in (0, False):
        appended = []
        status, resp = dashboard._assignments_post({"title": falsy}, append_fn=appended.append, start=lambda *a: None)
        assert status == 400
        assert resp["error"] == "title is required"
        assert appended == []


def test_assignments_post_rejects_non_dict_body():
    """body.get(...) on a non-dict raises AttributeError before validation even starts — the
    broadened except must turn that into a 400 too, not an uncaught crash."""
    import dashboard

    appended = []
    status, resp = dashboard._assignments_post(["not", "a", "dict"], append_fn=appended.append, start=lambda *a: None)
    assert status == 400
    assert "error" in resp
    assert appended == []


def test_assignments_post_returns_500_and_skips_notify_when_ledger_write_fails():
    import dashboard

    started = []

    def boom(rec):
        raise OSError("disk full")

    status, resp = dashboard._assignments_post(
        {"title": "fix it"}, append_fn=boom, start=lambda text, source: started.append(text)
    )
    assert status == 500
    assert "error" in resp
    assert started == []


def test_assignments_post_returns_202_with_warning_when_notify_fails():
    """The ledger write already succeeded — losing the notification must not lose the record."""
    import dashboard

    appended = []

    def boom(text, source):
        raise RuntimeError("manager busy")

    status, resp = dashboard._assignments_post({"title": "fix it"}, append_fn=appended.append, start=boom)
    assert status == 202
    assert resp["record"]["title"] == "fix it"
    assert "warning" in resp
    assert appended == [resp["record"]]


def test_get_assignments_decorates_with_at_risk_and_progress():
    import os as _os
    import tempfile

    import dashboard
    from assignments import new_assignment
    from escalations import append

    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("late one", deadline="2020-01-01")
        a["plan"] = [{"state": "done"}, {"state": "todo"}]
        append(path, a)
        rows = dashboard.get_assignments(path=path)
        assert rows[0]["at_risk"] is True
        assert rows[0]["progress"] == 0.5
        assert rows[0]["stalled"] is False  # it already has a plan
    finally:
        _os.unlink(path)


def test_get_assignments_decorates_with_stalled():
    import os as _os
    import tempfile
    import time

    import dashboard
    from assignments import STALLED_AFTER_SECONDS, new_assignment
    from escalations import append

    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("dropped one")
        a["ts"] = time.time() - STALLED_AFTER_SECONDS - 1
        append(path, a)
        rows = dashboard.get_assignments(path=path)
        assert rows[0]["stalled"] is True
    finally:
        _os.unlink(path)


def test_get_escalations_normalizes_a_bare_string_options_field():
    """The live bug: one on-disk record has options="Approve" (a bare string, not a list) —
    get_escalations() must hand back a real list so no consumer's forEach/`for o in x` walks
    the string's individual characters or throws outright."""
    import os as _os
    import tempfile

    import dashboard
    from escalations import append, new_record

    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        r = new_record("s1", "diff_review", "Ship it?")
        r["options"] = "Approve"  # the malformed shape found live
        r["status"] = "needs_human"
        append(path, r)
        result = dashboard.get_escalations(path=path)
        assert result["needs_human"][0]["options"] == ["Approve"]
    finally:
        _os.unlink(path)


def test_argv_repo_dir_extracts_the_non_numeric_arg():
    import dashboard

    assert dashboard._argv_repo_dir(["4400", "/some/repo"]) == "/some/repo"


def test_argv_repo_dir_none_when_only_a_port_is_given():
    import dashboard

    assert dashboard._argv_repo_dir(["4400"]) is None


def test_argv_repo_dir_none_with_no_args():
    import dashboard

    assert dashboard._argv_repo_dir([]) is None


def test_parse_ado_refs_allowlists_http_and_https_only():
    """An untrusted ado_refs[].url reaches an <a href> in dashboard.html with no CSP — the manager
    also writes ledger records straight through assignments.append(rec) with no validation at
    all, so this allowlist (not target="_blank", which does not block a javascript: URL in every
    browser) is what stands between model-authored ado_refs and script execution on a page holding
    the operator's ledger, chat, and escalation queue."""
    import dashboard

    raw = [
        {"id": "1", "url": "javascript:alert(1)"},
        {"id": "2", "url": "data:text/html,<script>alert(1)</script>"},
        {"id": "3", "url": "//evil.example/redirect"},
        {"id": "4", "url": ""},
        {"id": "5", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/5"},
    ]
    by_id = {r["id"]: r["url"] for r in dashboard._parse_ado_refs(raw)}
    assert by_id["1"] == "", "javascript: must not reach the page"
    assert by_id["2"] == "", "data: must not reach the page"
    assert by_id["3"] == "", "a scheme-relative URL must not reach the page"
    assert by_id["4"] == ""
    assert by_id["5"] == "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/5"


def test_parse_ado_refs_allowlist_is_case_insensitive_on_the_scheme():
    import dashboard

    by_id = {r["id"]: r["url"] for r in dashboard._parse_ado_refs([{"id": "1", "url": "HTTPS://Example.com/x"}])}
    assert by_id["1"] == "HTTPS://Example.com/x"


def test_ticket_dispatch_helpers_are_gone():
    import dashboard

    for gone in (
        "_build_dispatch_argv",
        "_run_dispatch",
        "_build_dispatch_prompt",
        "_ticket_task_slug",
        "_parse_ticket_ids",
    ):
        assert not hasattr(dashboard, gone), f"{gone} should have been removed with the route"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
