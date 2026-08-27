#!/usr/bin/env python3
"""assert-based checks for dashboard.py's transcript rendering. Run: python3 bin/test_dashboard.py"""

import json
import os
import tempfile

import dashboard
from dashboard import _find_ado_links, render_task_log, render_transcript_line


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
        "ado_refs": [{"id": "8165", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/8165"}],
    }
    try:
        result = dashboard._enrich_branch_and_links("some/path", "feature/x", known_ado_ids=["8172", "8165"])
    finally:
        dashboard._lookup_pr_and_ticket = original_lookup
        dashboard._git_branch = original_branch
    ids = sorted(r["id"] for r in result["ado_refs"])
    assert ids == ["8165", "8172"]
    assert result["pr_number"] == 42


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
