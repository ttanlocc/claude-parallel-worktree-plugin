#!/usr/bin/env python3
"""assert-based checks for dashboard.py's transcript rendering. Run: python3 bin/test_dashboard.py"""

import json
import os
import tempfile

from dashboard import render_task_log, render_transcript_line


def test_skips_non_conversation_lines():
    assert render_transcript_line({"type": "custom-title", "customTitle": "x"}) == []
    assert render_transcript_line({"type": "last-prompt", "lastPrompt": "x"}) == []
    assert render_transcript_line({"type": "attachment", "attachment": {}}) == []
    assert render_transcript_line({"type": "mode", "mode": "normal"}) == []


def test_renders_plain_string_user_content():
    obj = {"type": "user", "message": {"role": "user", "content": "hello"}}
    assert render_transcript_line(obj) == ["hello"]


def test_renders_assistant_text():
    obj = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "DONE"}]}}
    assert render_transcript_line(obj) == ["DONE"]


def test_renders_tool_use():
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}],
        },
    }
    lines = render_transcript_line(obj)
    assert len(lines) == 1
    assert lines[0].startswith("→ Bash(")


def test_renders_tool_result_string_content():
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "hi"}]},
    }
    assert render_transcript_line(obj) == ["← hi"]


def test_renders_tool_result_block_content():
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": [{"type": "text", "text": "block result"}]}],
        },
    }
    assert render_transcript_line(obj) == ["← block result"]


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
        assert result == ["line2", "line3", "line4"]
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
        assert render_task_log(path) == ["ok"]
    finally:
        os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
