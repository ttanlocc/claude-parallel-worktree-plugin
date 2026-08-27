#!/usr/bin/env python3
"""assert-based checks for dashboard.py's transcript rendering. Run: python3 bin/test_dashboard.py"""

import json
import os
import subprocess
import tempfile

import dashboard
from dashboard import (
    PARALLEL_TASK_SH,
    _build_dispatch_argv,
    _build_dispatch_prompt,
    _fetch_ado_description,
    _find_ado_links,
    _parse_ticket_ids,
    _run_dispatch,
    _shape_ado_ticket,
    _strip_html,
    _ticket_task_slug,
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


def test_strip_html_removes_tags_and_unescapes_entities():
    raw = "<h2><b>Title</b></h2><div>Body&nbsp;text with <i>emphasis</i>.</div>"
    assert _strip_html(raw) == "Title Body text with emphasis ."


def test_strip_html_collapses_whitespace():
    assert _strip_html("<p>a</p>\n\n<p>   b   </p>") == "a b"


def test_strip_html_handles_empty_string():
    assert _strip_html("") == ""


def test_fetch_ado_description_degrades_on_timeout():
    original_run = subprocess.run

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("az", 15)

    subprocess.run = mock_run
    try:
        result = _fetch_ado_description("8148")
        assert result == ""
    finally:
        subprocess.run = original_run


def test_ticket_task_slug_kebab_cases_and_truncates():
    assert _ticket_task_slug("Fix the Setpoint Guard!") == "fix-the-setpoint-guard"
    long_title = "A very long ticket title that goes on and on past forty characters easily"
    slug = _ticket_task_slug(long_title)
    assert len(slug) <= 40
    assert slug == slug.lower()
    assert " " not in slug


def test_ticket_task_slug_handles_non_ascii():
    assert _ticket_task_slug("Cùng 1 input, câu trả lời không đổi") != ""


def test_build_dispatch_prompt_includes_all_ticket_content_and_instructions():
    tickets = [
        {"id": "8172", "title": "Fix setpoint guard", "description": "Root cause is X."},
        {"id": "8165", "title": "Related follow-up", "description": "Second half of the fix."},
    ]
    prompt = _build_dispatch_prompt(tickets, "Focus on the backend only, skip the UI part.")
    assert "AB#8172" in prompt
    assert "Fix setpoint guard" in prompt
    assert "Root cause is X." in prompt
    assert "AB#8165" in prompt
    assert "Second half of the fix." in prompt
    assert "Focus on the backend only, skip the UI part." in prompt
    assert "verify" in prompt.lower()  # the Hermes verify-before-done reminder is present


def test_build_dispatch_prompt_omits_instructions_section_when_blank():
    tickets = [{"id": "1", "title": "T", "description": "D"}]
    prompt = _build_dispatch_prompt(tickets, "")
    assert "Extra instructions" not in prompt


def test_build_dispatch_argv_includes_one_ticket_flag_per_id():
    argv = _build_dispatch_argv("fix-setpoint-guard", "native", ["8172", "8165"])
    assert argv == [
        PARALLEL_TASK_SH,
        "start",
        "fix-setpoint-guard",
        "native",
        "--ticket",
        "8172",
        "--ticket",
        "8165",
    ]


def test_build_dispatch_argv_with_no_tickets():
    argv = _build_dispatch_argv("some-task", "native", [])
    assert argv == [PARALLEL_TASK_SH, "start", "some-task", "native"]


def test_parse_ticket_ids_rejects_non_list_string():
    # A JSON string body like {"ticket_ids": "8172"} must not silently iterate into ['8','1','7','2'].
    try:
        _parse_ticket_ids("8172")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_parse_ticket_ids_rejects_non_list_number():
    # A JSON number body like {"ticket_ids": 8172} must not raise an uncaught TypeError when iterated.
    try:
        _parse_ticket_ids(8172)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_parse_ticket_ids_accepts_list_and_treats_absent_as_empty():
    assert _parse_ticket_ids(["8172", 8165]) == ["8172", "8165"]
    assert _parse_ticket_ids(None) == []
    assert _parse_ticket_ids([]) == []


def test_run_dispatch_returns_500_on_start_stage_subprocess_error_instead_of_raising():
    original_run = subprocess.run

    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired("parallel-task.sh", 180)

    subprocess.run = mock_run
    try:
        status, resp = dashboard._run_dispatch([PARALLEL_TASK_SH, "start", "x", "native"], "x", "prompt")
        assert status == 500
        assert "start failed" in resp["error"]
    finally:
        subprocess.run = original_run


def test_run_dispatch_returns_500_on_dispatch_stage_subprocess_error_instead_of_raising():
    original_run = subprocess.run
    calls = []

    def mock_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:  # start succeeds
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")
        raise OSError("parallel-task.sh not found")  # dispatch fails

    subprocess.run = mock_run
    try:
        status, resp = _run_dispatch([PARALLEL_TASK_SH, "start", "x", "native"], "x", "prompt")
        assert status == 500
        assert "dispatch failed" in resp["error"]
    finally:
        subprocess.run = original_run


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
