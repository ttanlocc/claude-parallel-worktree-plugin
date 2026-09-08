#!/usr/bin/env python3
"""assert-based checks for the board-mirror systemd timer. Run: python3 bin/test_board_timer.py

These read the shipped files as text, the same way board-mirror.md is fed to `claude -p` as text
by run-board-mirror.sh — a regression here is exactly the kind of doc/unit drift this timer
replaced the session-scoped cron to avoid.
"""

import os
import re
import stat
import subprocess
import tempfile

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEMD_DIR = os.path.join(BIN_DIR, "systemd")


def _read(*parts):
    with open(os.path.join(BIN_DIR, *parts), encoding="utf-8") as f:
        return f.read()


def test_timer_cadence_is_three_minutes_off_the_round_marks():
    timer = _read("systemd", "board-mirror.timer")
    m = re.search(r"^OnCalendar=(.+)$", timer, re.MULTILINE)
    assert m, "board-mirror.timer must set OnCalendar"
    spec = m.group(1).strip()
    # */1/3 minute step off an offset that is not 0 — :00/:03/:06/... is the round-mark grid this
    # must avoid, same reasoning as the old cron's 7/22/37/52.
    step_match = re.search(r"\*:(\d+)/3:", spec)
    assert step_match, f"expected a '*:<offset>/3:...' minute step in OnCalendar, got {spec!r}"
    offset = int(step_match.group(1))
    assert offset % 3 != 0, f"offset {offset} still lands on the round 3-minute marks"


def test_doc_and_unit_agree_on_the_cadence():
    doc = _read("board-mirror.md")
    timer = _read("systemd", "board-mirror.timer")
    assert "Every 3 minutes" in doc, "the doc must state the CURRENT cadence as 3 minutes"
    # "15 minutes" may still appear as history ("This was 15 minutes...") but must not be stated
    # as the live cadence.
    assert "Every 15 minutes" not in doc
    assert re.search(r"\*:\d+/3:", timer), "the unit file must actually run every 3 minutes"


def test_env_var_names_match_what_board_state_and_dashboard_actually_read():
    run_script = _read("systemd", "run-board-mirror.sh")
    doc = _read("board-mirror.md")
    example_env = _read("systemd", "board-mirror.env.example")
    dashboard = _read("dashboard.py")
    manager_session = _read("manager_session.py")

    # PWR_ADO_ASSIGNED_TO is read by dashboard.py; PWT_REPO_ROOT by manager_session.py's
    # resolve_repo_root(), which board_state.py calls. Both must match, letter for letter.
    assert 'os.environ.get("PWR_ADO_ASSIGNED_TO"' in dashboard
    assert 'env.get("PWT_REPO_ROOT")' in manager_session

    for name in ("ARTIFACT_URL", "PWT_REPO_ROOT"):
        assert name in run_script, f"{name} must be validated by run-board-mirror.sh"
        assert name in doc, f"{name} must be documented in board-mirror.md"
        assert name in example_env, f"{name} must appear in the shipped .env.example"

    assert "PWR_ADO_ASSIGNED_TO" in doc
    assert "PWR_ADO_ASSIGNED_TO" in example_env


def test_run_script_fails_loudly_on_missing_env():
    run_script = _read("systemd", "run-board-mirror.sh")
    assert "set -euo pipefail" in run_script
    # `: "${VAR:?...}"` is bash's fail-loud-with-a-message idiom for a required var.
    assert re.search(r':\s*"\$\{ARTIFACT_URL:\?', run_script)
    assert re.search(r':\s*"\$\{PWT_REPO_ROOT:\?', run_script)


def test_run_script_finds_board_mirror_md_when_invoked_through_a_symlink():
    # README step 2 installs this script as a symlink under ~/.config/board-mirror/, not a copy.
    # `dirname "${BASH_SOURCE[0]}"` alone resolves against the symlink's own directory rather than
    # the repo it points into, so a text-only check of the script's source can't catch this —
    # it has to actually run through a real symlink to reproduce the reported
    # "sed: can't read .../board-mirror.md: No such file or directory".
    real_script = os.path.join(SYSTEMD_DIR, "run-board-mirror.sh")
    with tempfile.TemporaryDirectory() as tmp:
        symlinked_script = os.path.join(tmp, "run-board-mirror.sh")
        os.symlink(real_script, symlinked_script)

        fake_claude = os.path.join(tmp, "claude")
        with open(fake_claude, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env bash\n"
                'echo \'{"result": "REFRESH_OK: wrote 0 documents, last_ado_sweep=none"}\'\n'
            )
        os.chmod(fake_claude, os.stat(fake_claude).st_mode | stat.S_IEXEC)

        env = dict(os.environ)
        env["ARTIFACT_URL"] = "https://example.invalid/artifact"
        env["PWT_REPO_ROOT"] = tmp
        env["CLAUDE_BIN"] = fake_claude

        proc = subprocess.run(
            [symlinked_script], env=env, capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, (
            f"run-board-mirror.sh failed when invoked through a symlink: {proc.stderr}"
        )
        assert "REFRESH_OK" in proc.stdout


def test_run_script_pre_grants_the_artifact_permission_for_headless_runs():
    # `claude -p` runs non-interactively under the timer — no one can approve the write_db
    # permission prompt board-mirror.md's step 2 triggers, so without a pre-grant every run dies
    # with "requires permission approval that was not granted" and no rows ever get written.
    # Artifact has no finer specifier (unlike Bash's command patterns), so `Artifact` is the
    # narrowest grant --allowedTools supports; this pins that flag so a future edit can't drop it.
    run_script = _read("systemd", "run-board-mirror.sh")
    m = re.search(r'"\$CLAUDE_BIN"\s+-p\s+(.*?)--output-format', run_script)
    assert m, "expected a `claude -p ... --output-format` invocation in run-board-mirror.sh"
    assert re.search(r"--allowedTools\s+Artifact\b", m.group(1)), (
        f"claude -p invocation is missing --allowedTools Artifact: {m.group(1)!r}"
    )


def test_run_script_defaults_claude_bin_to_an_absolute_path():
    run_script = _read("systemd", "run-board-mirror.sh")
    # A systemd --user unit's PATH usually excludes ~/.local/bin, so a bare `claude` default
    # would fail with "command not found" under the timer even though it works in any login
    # shell — this pins the default to an absolute path instead.
    m = re.search(r'CLAUDE_BIN="\$\{CLAUDE_BIN:-(.+)\}"', run_script)
    assert m, "CLAUDE_BIN must have a default"
    assert m.group(1).startswith("$HOME/") or m.group(1).startswith("/"), (
        f"CLAUDE_BIN default {m.group(1)!r} is not an absolute path"
    )


def test_run_script_treats_anything_but_refresh_ok_as_failure():
    run_script = _read("systemd", "run-board-mirror.sh")
    assert "REFRESH_OK" in run_script
    assert "exit 1" in run_script
    # A non-zero claude -p exit must not be swallowed.
    assert "exited non-zero" in run_script


def test_prompt_requires_the_refresh_ok_or_refresh_failed_sentinel():
    doc = _read("board-mirror.md")
    assert "REFRESH_OK:" in doc
    assert "REFRESH_FAILED:" in doc


def test_service_is_a_plain_oneshot_unit_so_systemd_serializes_runs():
    service = _read("systemd", "board-mirror.service")
    assert re.search(r"^Type=oneshot$", service, re.MULTILINE), (
        "a non-templated oneshot unit is what lets systemd coalesce an overlapping timer fire "
        "into a no-op instead of starting a second claude -p run"
    )
    # Not templated (board-mirror@.service) — a template allows multiple concurrent instances,
    # which would defeat the overlap guard above.
    assert not os.path.exists(os.path.join(SYSTEMD_DIR, "board-mirror@.service"))


def test_service_does_not_run_as_root():
    service = _read("systemd", "board-mirror.service")
    assert "User=root" not in service
    assert "User=" not in service  # a --user unit: no User= means "run as whoever owns the session"


def test_no_real_secrets_committed():
    # The literal values the task handed us — if either shows up in a tracked file, someone
    # pasted the real config into the repo instead of ~/.config/board-mirror/env.
    real_artifact_url = "https://claude.ai/code/artifact/bc6f123e-bd44-4840-87e3-d78ffc95529e"
    real_repo_root = "/home/azureuser/aiq/aiquinta-platform"

    for fname in os.listdir(SYSTEMD_DIR):
        path = os.path.join(SYSTEMD_DIR, fname)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert real_artifact_url not in content, f"{fname} committed a real artifact URL"
        assert real_repo_root not in content, f"{fname} committed a real repo root path"

    doc = _read("board-mirror.md")
    assert real_artifact_url not in doc
    assert real_repo_root not in doc


def test_example_env_is_a_template_not_a_real_config():
    example_env = _read("systemd", "board-mirror.env.example")
    assert "REPLACE-WITH-YOUR-ARTIFACT-ID" in example_env
    assert "/absolute/path/to/aiquinta-platform" in example_env


def test_readme_config_step_covers_the_multi_identity_env_var():
    readme = _read("systemd", "README.md")
    # The config step (step 1) only walks through ARTIFACT_URL and PWT_REPO_ROOT by name;
    # PWR_ADO_ASSIGNED_TO must be called out there too, or installers who don't hold a second
    # ADO identity themselves will skip it and ship a silently half-empty board for whoever does.
    assert "PWR_ADO_ASSIGNED_TO" in readme


def test_readme_makes_linger_a_mandatory_step_with_the_privilege_it_needs():
    readme = _read("systemd", "README.md")
    assert "enable-linger" in readme
    assert "MANDATORY" in readme or "mandatory" in readme
    # Must say who/what is needed to run it, not just the bare command.
    assert "root" in readme.lower() or "sudo" in readme.lower()
    assert "Linger=yes" in readme


if __name__ == "__main__":
    failures = 0
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
