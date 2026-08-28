#!/usr/bin/env python3
"""The one serialized door to the persistent Engineering Manager session.

Everything reaching the manager — the CTO's chat, escalations, worker-completion and tick wakes —
goes through ask(). One session means one memory: the manager knows what it dispatched, what it
decided, and what the CTO told it. An flock serializes callers, because two processes resuming the
same session would race on its transcript.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import time

HERMES_DIR = os.path.expanduser("~/.claude/hermes")
STATE_PATH = os.path.join(HERMES_DIR, "manager-session.json")
CHAT_PATH = os.path.join(HERMES_DIR, "manager-chat.jsonl")
LOCK_PATH = os.path.join(HERMES_DIR, "manager.lock")
CHARTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "engineering-manager",
    "SKILL.md",
)

MANAGER_MODEL = os.environ.get("PWT_MANAGER_MODEL", "claude-opus-5")
MANAGER_EFFORT = os.environ.get("PWT_MANAGER_EFFORT", "max")

# ponytail: global lock, per-account locks if throughput matters. 300s = background threads never
# starve while an HTTP request waits; a short timeout drops daemon wakes with no UI retry surface.
LOCK_TIMEOUT = 300
CALL_TIMEOUT = 600

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


class ManagerBusy(RuntimeError):
    """Another caller held the lock past the timeout."""


def load_charter(path: str | None = None) -> str:
    """Read the charter. Resolved at call time so the module global stays the single source."""
    with open(path or CHARTER_PATH, encoding="utf-8") as fh:
        return fh.read()


def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(session_id: str) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"session_id": session_id, "started_at": time.time()}, fh)


def reset() -> None:
    """Forget the session so the next ask() bootstraps a fresh one."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(STATE_PATH)


def append_chat(role: str, source: str, text: str) -> None:
    """One line per turn. `role` says who spoke, `source` says what prompted the exchange."""
    os.makedirs(os.path.dirname(CHAT_PATH), exist_ok=True)
    with open(CHAT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "role": role, "source": source, "text": text}) + "\n")


def history(limit: int = 200) -> list[dict]:
    """The newest `limit` turns, oldest first. A corrupt line is skipped, never fatal."""
    try:
        with open(CHAT_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@contextlib.contextmanager
def _locked(timeout: int | None = None):
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                raise ManagerBusy(f"manager busy: lock held longer than {timeout}s")
            time.sleep(0.2)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def busy() -> bool:
    """True when a call is in flight. Drives the chat's thinking indicator."""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def ask_argv(session_id, text: str) -> list[str]:
    """Argv for one manager turn.

    Model and effort go on every call, resumes included: --effort applies to the invocation, not to
    the stored session, so omitting it on a resume silently downgrades the manager.
    """
    argv = ["claude", "--model", MANAGER_MODEL, "--effort", MANAGER_EFFORT, "--output-format", "json"]
    if session_id:
        argv += ["--resume", session_id]
    return argv + ["-p", text]


def _log_quietly(role: str, source: str, text: str) -> None:
    """Append a chat entry, never raising — a failure to log must not mask the failure it records."""
    try:
        append_chat(role, source, text)
    except OSError:
        pass


def _ask_locked(text: str, source: str, run, timeout: int) -> str:
    """One turn, with the lock already held. Returns a string on every path."""
    _log_quietly("cto" if source == "cto" else "system", source, text)
    try:
        session_id = _read_state().get("session_id")
        prompt = text if session_id else f"{load_charter()}\n\n---\n\n{text}"
    except OSError as e:
        reply = f"manager call failed before dispatch: {e}"
        _log_quietly("manager", source, reply)
        return reply

    try:
        proc = run(
            ask_argv(session_id, prompt),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    except SUBPROC_ERRORS + (ValueError,) as e:
        reply = f"manager call failed: {e}"
        _log_quietly("manager", source, reply)
        return reply

    reply = payload.get("result") or ""
    if not session_id:
        new_id = payload.get("session_id")
        if not new_id:
            _log_quietly(
                "manager",
                source,
                "WARNING: the manager replied without a session id — the next turn will start a "
                "new session and lose this context",
            )
        else:
            try:
                _write_state(new_id)
            except OSError as e:
                _log_quietly(
                    "manager",
                    source,
                    f"WARNING: replied but could not save the session id ({e}) — the next turn "
                    "will start a new session and lose this context",
                )
    _log_quietly("manager", source, reply)
    return reply


def ask(text: str, source: str, run=subprocess.run, timeout: int = CALL_TIMEOUT) -> str:
    """Send one turn to the manager and return its reply.

    The charter is prepended to the FIRST user message rather than passed as a flag, because
    --resume replays the transcript: a charter inside the transcript is re-read on every later
    call, while a flag passed once at bootstrap is not.

    Every failure except ManagerBusy becomes a logged reply string rather than an exception — a
    caller that loses its message cannot recover it, so the chat log is the record. ManagerBusy
    alone propagates, because the HTTP layer maps it to a 503.
    """
    try:
        with _locked():
            return _ask_locked(text, source, run, timeout)
    except ManagerBusy:
        _log_quietly("manager", source, "manager busy — this turn was not delivered")
        raise
