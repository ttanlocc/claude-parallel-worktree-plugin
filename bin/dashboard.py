#!/usr/bin/env python3
"""Live status/log dashboard for parallel-task.sh copies."""

import html
import json
import re


def _flatten_tool_result(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts)
    return ""


def render_transcript_line(obj: dict) -> list[dict]:
    """Return zero or more renderable records for one transcript JSON record.

    Each record is {"kind": "text"|"call"|"result", "role", "tool", "text"}. "call" records also
    carry "call_id" and "result" records carry "result_for" (both from the tool_use/tool_result
    id pairing) — render_task_log uses those to redact a credential-looking call's own result,
    then strips them before returning.
    """
    if obj.get("type") not in ("user", "assistant"):
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role") or obj.get("type")
    content = message.get("content")
    if isinstance(content, str):
        return [{"kind": "text", "role": role, "tool": None, "text": content}]
    if not isinstance(content, list):
        return []
    lines = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                lines.append({"kind": "text", "role": role, "tool": None, "text": text})
        elif item_type == "tool_use":
            name = item.get("name", "?")
            args = item.get("input") or {}
            preview = args.get("command") or args.get("file_path") or json.dumps(args)
            lines.append({"kind": "call", "role": role, "tool": name, "text": str(preview), "call_id": item.get("id")})
        elif item_type == "tool_result":
            lines.append(
                {
                    "kind": "result",
                    "role": role,
                    "tool": None,
                    "text": _flatten_tool_result(item.get("content")),
                    "result_for": item.get("tool_use_id"),
                }
            )
    return lines


# Heuristic, not a secret scanner: catches a command that NAMES what it's after (grep for
# "personal access token", reading a .pem, etc). It cannot catch a bare secret value with no
# label, so it works by redacting the credential-looking CALL and, via call_id/result_for,
# that same call's result — not by scanning result text for secret-shaped content.
# ponytail: keyword heuristic: misses secrets with no label in the command; upgrade to a real
# secret-scanner (e.g. detect-secrets) if this misses cases in practice.
_SENSITIVE_RE = re.compile(r"(?i)personal access token|api[_ -]?key|password|private key|-----BEGIN|secret")
_REDACTED = "[redacted — this call touches a credential]"
_MAX_LINE_CHARS = 400


def render_task_log(transcript_path: str, limit: int = 200) -> list[dict]:
    """Read a session transcript JSONL file and return the last `limit` rendered records,
    with credential-looking tool calls (and their matching results) redacted."""
    records: list[dict] = []
    sensitive_ids: set[str] = set()
    with open(transcript_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for rec in render_transcript_line(obj):
                call_id = rec.pop("call_id", None)
                result_for = rec.pop("result_for", None)
                if rec["kind"] == "call" and _SENSITIVE_RE.search(rec["text"]):
                    if call_id:
                        sensitive_ids.add(call_id)
                    rec["text"] = _REDACTED
                elif rec["kind"] == "result" and result_for in sensitive_ids:
                    rec["text"] = _REDACTED
                if len(rec["text"]) > _MAX_LINE_CHARS:
                    rec["text"] = rec["text"][:_MAX_LINE_CHARS] + "…"
                records.append(rec)
    return records[-limit:]


import http.server
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from escalations import QUEUE_PATH, classify, current_state, record_answer

PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))
PARALLEL_TASK_SH = os.path.join(PLUGIN_BIN, "parallel-task.sh")
MAX_BODY_BYTES = 64 * 1024

# Every subprocess-wrapping function below degrades silently on failure. TimeoutExpired
# subclasses SubprocessError, not OSError, so it must be listed via SubprocessError (its actual
# parent) rather than assumed to ride along with OSError.
_SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def get_escalations() -> dict:
    """What the human still has to answer, and what the manager already decided for them."""
    state = current_state(QUEUE_PATH)
    needs_human = [r for r in state if r.get("status") == "needs_human"]
    for r in state:
        if r.get("status") == "open":
            try:
                is_tier3 = classify(r)[0] == "tier3"
            except Exception:
                is_tier3 = True  # can't classify it -> fail closed, show it to a human
            if is_tier3:
                needs_human.append(r)
    decisions = [r for r in state if r.get("decided_by") == "manager"]
    decisions.sort(key=lambda r: r.get("answered_at") or 0, reverse=True)
    return {"needs_human": needs_human, "recent_decisions": decisions[:20]}


# The repo whose copies we report on. Set once in main(); `parallel-task.sh` finds
# its registry via `git rev-parse --show-toplevel`, so this must be inside the
# target repo — NOT the plugin's own directory, which is typically installed under
# ~/.claude/plugins/ and isn't a git repo at all.
REPO_DIR = os.getcwd()


# Both sources shell out to CLIs costing ~0.5s each. The page polls two endpoints every
# 2s and both call get_tasks(), so uncached the calls overlap, pile up, and the server
# stops answering (measured: 15s per request). One snapshot per TTL is plenty — nothing
# here changes faster than the poll interval.
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 1.5
_CACHE_LOCK = threading.Lock()


def _cached(key: str, fn, ttl: float = _CACHE_TTL):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _CACHE_LOCK:  # one refresh at a time; latecomers take the fresh value
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = fn()
        _CACHE[key] = (time.monotonic(), value)
        return value


# PR/ticket lookups hit the network (gh) or a subprocess (git) — much pricier than the
# 1.5s-TTL local calls above, and branch/PR association barely changes turn to turn.
_ENRICH_TTL = 30.0

_ADO_URL_RE = re.compile(r"https?://dev\.azure\.com/\S+/_workitems/edit/(\d+)")
_ADO_REF_RE = re.compile(r"\bAB[#-](\d+)\b", re.IGNORECASE)
_ADO_DEFAULT_BASE = "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/"

_EMPTY_PR_TICKET = {"pr_number": None, "pr_url": None, "pr_state": None, "ado_refs": []}


def _find_ado_links(text: str) -> list[dict]:
    """Every ADO reference in the text, deduplicated by id, first-seen order. A full
    dev.azure.com URL wins over a bare AB#NNNN ref for the same id (keeps whatever org/project
    form the author actually used instead of assuming this repo's default one)."""
    by_id: dict[str, str] = {}
    order: list[str] = []
    # Collect all matches with their positions
    matches = []
    for m in _ADO_URL_RE.finditer(text):
        matches.append((m.start(), "url", m.group(1), m.group(0)))
    for m in _ADO_REF_RE.finditer(text):
        matches.append((m.start(), "ref", m.group(1), _ADO_DEFAULT_BASE + m.group(1)))
    # Sort by position in text, process in order
    matches.sort(key=lambda x: x[0])
    for pos, match_type, ticket_id, url in matches:
        if ticket_id not in by_id:
            order.append(ticket_id)
            by_id[ticket_id] = url
        elif match_type == "url":
            # URL wins over bare ref for the same ID
            by_id[ticket_id] = url
    return [{"id": i, "url": by_id[i]} for i in order]


def _git_branch(path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or None
    except _SUBPROC_ERRORS:
        return None


def _lookup_pr_and_ticket(branch: str) -> dict:
    """This repo's convention (seen in real commit/PR history): PR titles/bodies carry an
    AB#NNNN or full ADO link — e.g. 'fix: ... (AB#7160)' or a `[AB#6541](https://dev.azure...)`
    markdown link in the body. One `gh` call gets both; no separate ADO API access needed."""
    if not branch:
        return dict(_EMPTY_PR_TICKET)
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,title,body,state",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=REPO_DIR,
        )
        prs = json.loads(result.stdout) if result.returncode == 0 else []
    except _SUBPROC_ERRORS:
        prs = []
    if not prs:
        return dict(_EMPTY_PR_TICKET)
    pr = prs[0]
    ado_refs = _find_ado_links(f"{pr.get('title', '')}\n{pr.get('body') or ''}")
    return {
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "pr_state": pr.get("state"),
        "ado_refs": ado_refs,
    }


def _enrich_branch_and_links(cwd: str, known_branch: str | None, known_ado_ids: list[str] | None = None) -> dict:
    """cwd is the empty string for a shared checkout (the repo root, not a dedicated worktree):
    its "current branch" belongs to whichever session last ran `git checkout` there, not to any
    one session, so deriving one would misattribute a PR/ticket to every session sharing it."""
    branch = known_branch
    if not branch and cwd:
        branch = _cached(f"branch:{cwd}", lambda: _git_branch(cwd), ttl=_ENRICH_TTL)
    registry_refs = [{"id": i, "url": _ADO_DEFAULT_BASE + i} for i in (known_ado_ids or [])]
    if not branch:
        merged = list(registry_refs)
        return {"branch": branch, **_EMPTY_PR_TICKET, "ado_refs": merged}
    pr_ticket = _cached(f"prticket:{branch}", lambda: _lookup_pr_and_ticket(branch), ttl=_ENRICH_TTL)
    seen = {r["id"] for r in registry_refs}
    merged = list(registry_refs) + [r for r in pr_ticket["ado_refs"] if r["id"] not in seen]
    return {"branch": branch, **pr_ticket, "ado_refs": merged}


def get_registry() -> list[dict]:
    """Copies provisioned by `parallel-task.sh start` — branch, ports, dev-stack state."""

    def run():
        result = subprocess.run(
            [PARALLEL_TASK_SH, "list", "--json"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_DIR,
            timeout=20,
        )
        return json.loads(result.stdout)

    return _cached("registry", run)


def get_sessions() -> list[dict]:
    """Every live Claude session, whether or not parallel-task.sh created it."""

    def run():
        result = subprocess.run(
            ["claude", "agents", "--json", "--all"], capture_output=True, text=True, check=True, timeout=20
        )
        return json.loads(result.stdout)

    return _cached("sessions", run)


_ADO_ORG = "https://dev.azure.com/agentiqai"
_ADO_PROJECT = "AgentIQ"
_ADO_BACKLOG_WIQL = (
    "SELECT [System.Id], [System.Title], [System.State] FROM WorkItems "
    f"WHERE [System.TeamProject] = '{_ADO_PROJECT}' AND [System.AssignedTo] = @Me "
    "AND [System.State] NOT IN ('Closed', 'Removed')"
)


def _shape_ado_ticket(raw: dict) -> dict:
    fields = raw.get("fields") or {}
    ticket_id = str(raw.get("id") or fields.get("System.Id") or "")
    return {
        "id": ticket_id,
        "title": fields.get("System.Title") or "",
        "state": fields.get("System.State") or "",
        "url": _ADO_DEFAULT_BASE + ticket_id,
    }


_TAG_RE = re.compile(r"<[^>]+>")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _ticket_task_slug(title: str) -> str:
    ascii_title = title.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_title.lower()).strip("-")
    return slug[:40].strip("-") or "ticket"


def _build_dispatch_prompt(tickets: list[dict], instructions: str) -> str:
    sections = []
    for t in tickets:
        sections.append(f"AB#{t['id']}: {t['title']}\n{t['description']}")
    body = "\n\n".join(sections)
    parts = [
        "You've been assigned the following ticket(s):",
        body,
    ]
    if instructions.strip():
        parts.append(f"Extra instructions from the manager:\n{instructions.strip()}")
    parts.append(
        "Follow this repo's CLAUDE.md conventions. Before reporting done: run the relevant "
        "tests and confirm they're green, and verify the actual behavior — don't mark this "
        "complete on a self-report alone."
    )
    return "\n\n".join(parts)


def _build_dispatch_argv(slug: str, mode: str, ticket_ids: list[str]) -> list[str]:
    argv = [PARALLEL_TASK_SH, "start", slug, mode]
    for tid in ticket_ids:
        argv += ["--ticket", tid]
    return argv


def _parse_ticket_ids(raw) -> list[str]:
    """raw is body.get('ticket_ids'): absent/empty (None, [], "", 0, False) means no tickets
    requested, same as before. Anything else must be a list — a bare string would otherwise pass
    the caller's `if not ticket_ids` check by iterating into its own characters, and a bare number
    would raise an uncaught TypeError when iterated. Raises ValueError, caught by do_POST's
    existing body-parsing except block."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("ticket_ids must be a list")
    return [str(i) for i in raw]


def _run_dispatch(start_argv: list[str], slug: str, prompt: str) -> tuple[int, dict]:
    """Runs `parallel-task.sh start` then `dispatch` for an already-validated request. Returns
    (http_status, body) instead of letting a subprocess failure (timeout, or the script missing/
    non-executable) cross into do_POST uncaught and drop the connection with no response."""
    try:
        start_result = subprocess.run(start_argv, capture_output=True, text=True, cwd=REPO_DIR, timeout=180)
    except (subprocess.SubprocessError, OSError) as e:
        return 500, {"error": f"start failed: {e}"}
    if start_result.returncode != 0:
        return 500, {"error": f"start failed: {start_result.stderr[-2000:]}"}

    try:
        dispatch_result = subprocess.run(
            [PARALLEL_TASK_SH, "dispatch", slug, prompt], capture_output=True, text=True, cwd=REPO_DIR, timeout=60
        )
    except (subprocess.SubprocessError, OSError) as e:
        return 500, {"error": f"dispatch failed: {e}"}
    if dispatch_result.returncode != 0:
        return 500, {"error": f"dispatch failed: {dispatch_result.stderr[-2000:]}"}

    return 200, {"ok": True, "task": slug}


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_ado_description(ticket_id: str) -> str:
    try:
        result = subprocess.run(
            ["az", "boards", "work-item", "show", "--id", ticket_id, "--org", _ADO_ORG, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        fields = json.loads(result.stdout).get("fields") or {}
        return _strip_html(fields.get("System.Description") or "")
    except _SUBPROC_ERRORS:
        return ""


def get_ado_backlog() -> list[dict]:
    """Tickets assigned to you, not closed — the manager's read-only view into ADO. Any
    failure (az not authenticated, network down) degrades to an empty backlog, same as every
    other subprocess-backed source in this file — a dashboard that can't reach ADO still shows
    live sessions."""

    def run():
        result = subprocess.run(
            ["az", "boards", "query", "--org", _ADO_ORG, "--wiql", _ADO_BACKLOG_WIQL, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return []
        return [_shape_ado_ticket(r) for r in json.loads(result.stdout)]

    try:
        return _cached("ado_backlog", run, ttl=60.0)
    except _SUBPROC_ERRORS:
        return []


def get_tasks() -> list[dict]:
    """Sessions running in this repo, enriched with registry data where it exists.

    Session-first, not registry-first: a session doing work is visible immediately —
    during provisioning, and for worktrees created by any other means. The registry
    only adds branch/ports/dev-stack once a copy has been fully provisioned.
    """
    try:
        registry = get_registry()
    except Exception:
        registry = []  # a broken/absent registry must not hide live sessions
    by_session = {r["session_id"]: r for r in registry if r.get("session_id")}

    repo = os.path.realpath(REPO_DIR)
    rows, seen = [], set()
    for s in get_sessions():
        cwd = s.get("cwd") or ""
        if not os.path.realpath(cwd).startswith(repo):
            continue  # other repos aren't this dashboard's business
        sid = s.get("sessionId")
        reg = by_session.get(sid, {})
        seen.add(sid)
        rows.append(
            {
                "task": reg.get("task") or s.get("name") or (cwd.rsplit("/", 1)[-1] or "-"),
                "path": reg.get("path") or cwd,
                "session_id": sid,
                "short_id": reg.get("short_id") or s.get("id"),
                "agent_status": s.get("status"),
                "agent_state": s.get("state"),
                "kind": s.get("kind"),
                "started_at": s.get("startedAt"),
                "mode": reg.get("mode"),
                "ports": reg.get("ports"),
                "dev_status": reg.get("dev_status"),
                "managed": bool(reg),
                **_enrich_branch_and_links(
                    cwd if os.path.realpath(cwd) != repo else "", reg.get("branch"), reg.get("ado_ids")
                ),
            }
        )

    # Registered copies whose session already exited still matter (stack may be up).
    for r in registry:
        if r.get("session_id") not in seen:
            rows.append(
                {
                    **r,
                    "kind": None,
                    "started_at": None,
                    "managed": True,
                    **_enrich_branch_and_links(r.get("path", ""), r.get("branch"), r.get("ado_ids")),
                }
            )

    rows.sort(key=lambda r: (not r["managed"], r["task"] or ""))
    return rows


def _slugify_cwd(path: str) -> str:
    return path.replace("/", "-").replace(".", "-")


def transcript_path_for(task: dict) -> str | None:
    session_id = task.get("session_id")
    wt_path = task.get("path")
    if not session_id or not wt_path:
        return None
    home = os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects", _slugify_cwd(wt_path), f"{session_id}.jsonl")


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tasks":
            try:
                self._json(get_tasks())
            except (*_SUBPROC_ERRORS, subprocess.CalledProcessError) as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/log"):
            name = parsed.path[len("/api/tasks/") : -len("/log")]
            try:
                tasks = {t["task"]: t for t in get_tasks()}
            except (*_SUBPROC_ERRORS, subprocess.CalledProcessError) as e:
                self._json({"error": str(e)}, status=500)
                return
            task = tasks.get(name)
            path = transcript_path_for(task) if task else None
            if not path or not os.path.exists(path):
                self._json({"status": "not-available"})
                return
            try:
                self._json({"lines": render_task_log(path)})
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/escalations":
            try:
                self._json(get_escalations())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/ado-tickets":
            try:
                self._json(get_ado_backlog())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/":
            html_path = os.path.join(PLUGIN_BIN, "dashboard.html")
            if not os.path.exists(html_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # Browsers omit Origin entirely for a same-origin request (which is how the dashboard's
        # own fetch("/api/...") calls look) but always send it on a cross-origin one — so this
        # blocks another open tab from firing a drive-by POST (worktree provisioning, a real
        # dispatched session) at this server without needing CORS preflight to save it.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.server.server_address[1]}":
            self._json({"error": "cross-origin POST refused"}, status=403)
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/escalations/") and parsed.path.endswith("/answer"):
            rid = parsed.path[len("/api/escalations/") : -len("/answer")]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._json({"error": "request body too large"}, status=413)
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                answer = (body.get("answer") or "").strip()
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": f"bad request body: {e}"}, status=400)
                return
            if not answer:
                self._json({"error": "answer is required"}, status=400)
                return
            try:
                state = {r["id"]: r for r in current_state(QUEUE_PATH) if r.get("id")}
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            existing = state.get(rid)
            if existing is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            # Decline rather than append a conflicting state: someone may already have acted
            # on the existing answer, and this endpoint cannot undo that.
            if existing.get("status") != "needs_human":
                self._json(
                    {"error": f"escalation {rid} is already {existing.get('status')}", "record": existing},
                    status=409,
                )
                return
            try:
                updated = record_answer(QUEUE_PATH, rid, answer, "human")
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            if updated is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            self._json({"ok": True, "record": updated})
            return
        if parsed.path == "/api/tickets/dispatch":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._json({"error": "request body too large"}, status=413)
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                ticket_ids = _parse_ticket_ids(body.get("ticket_ids"))
                instructions = str(body.get("instructions") or "")
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": f"bad request body: {e}"}, status=400)
                return
            if not ticket_ids:
                self._json({"error": "ticket_ids is required and must be non-empty"}, status=400)
                return

            backlog_by_id = {t["id"]: t for t in get_ado_backlog()}
            tickets = []
            for tid in ticket_ids:
                title = backlog_by_id.get(tid, {}).get("title", f"ticket {tid}")
                tickets.append({"id": tid, "title": title, "description": _fetch_ado_description(tid)})

            slug = _ticket_task_slug(tickets[0]["title"])
            prompt = _build_dispatch_prompt(tickets, instructions)
            start_argv = _build_dispatch_argv(slug, "native", ticket_ids)

            status, resp = _run_dispatch(start_argv, slug, prompt)
            self._json(resp, status=status)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # ponytail: quiet by default; add real logging if this needs debugging later


def main():
    global REPO_DIR
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    port = int(args[0]) if args and args[0].isdigit() else 4400
    repo_args = [a for a in args if not a.isdigit()]
    REPO_DIR = os.path.abspath(repo_args[0]) if repo_args else os.getcwd()

    # Fail loudly at startup rather than serving 500s: a wrong repo dir is the
    # difference between "no copies running" and "you're looking at the wrong repo".
    try:
        tasks = get_tasks()
    except Exception as e:
        print(f"error: cannot read parallel-task registry in {REPO_DIR}\n  {e}", file=sys.stderr)
        print("hint: run from your repo root, or pass it: dashboard.py [port] <repo-dir>", file=sys.stderr)
        raise SystemExit(1)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}  (repo: {REPO_DIR}, {len(tasks)} copies)")
    server.serve_forever()


if __name__ == "__main__":
    main()
