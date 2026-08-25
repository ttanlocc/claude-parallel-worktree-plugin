# parallel-worktree-run

Claude Code plugin: spin up N independent, fully running copies of a repo — each its own
git worktree, branch, dev stack, and ports — from a single Claude Code session, and dispatch
background sessions to implement tasks inside them.

Extracted from a working setup in a monorepo (aiquinta-platform); see
[Prerequisites](skills/parallel-worktree-run/SKILL.md#prerequisites--repo-layout-this-plugin-assumes)
in the skill for the repo layout `bin/dev-stack.sh` / `bin/dev-native.sh` expect
(`deploy/docker-compose.yml` with per-service `${..._HOST_PORT}` vars, pnpm + uv workspaces).
If your repo doesn't match, `bin/*.sh` is a reference implementation — adjust the `up`/`down`
command bodies; the worktree/slot/registry orchestration in `bin/parallel-task.sh` doesn't
need to change.

## Install

```
claude plugin install https://github.com/thanhtoan0499/claude-parallel-worktree-plugin
```

or add as a marketplace source and install by name — see Claude Code plugin docs.

## What's in here

- `skills/parallel-worktree-run/SKILL.md` — the skill Claude Code loads to drive the workflow.
- `bin/parallel-task.sh` — orchestrator: `start <task> <native|docker>`, `dispatch`, `list`, `stop`, `rm`.
  Wraps `git worktree add` + slot/port allocation + a JSON registry
  (`.claude/worktrees/.parallel-registry.json` in the target repo).
- `bin/dev-stack.sh` — docker-compose slot runner. Slot *N* offsets every host port by
  `N*100`: gateway `8081+N*100`, frontend `5173+N*100`, postgres `5432+N*100`,
  azurite `10000+N*100`, redis `6379+N*100`. Compose project name `<app>-s${N}`.
- `bin/dev-native.sh` — native (host-run, hot-reload) mode: task *N* → gateway `8500+N`,
  frontend `5500+N`, shared Postgres/Azurite across native tasks.
- `bin/dashboard.py` / `bin/dashboard.html` — local web dashboard (see Dashboard below):
  status + live log per running copy, reading `parallel-task.sh list --json` and each task's
  session transcript. Stdlib-only, no build step.
- `bin/escalations.py` / `bin/manager.py` / `bin/manager_daemon.py` — the manager tier: workers
  append what they cannot decide to a queue, a short-lived Fable manager settles the mechanical
  ones and answers the worker directly, and only irreversible or genuinely ambiguous calls reach
  you in the dashboard. Every automatic decision is logged where you can audit it.

All four are added to `PATH` while the plugin is active — the skill invokes them by bare
name (`parallel-task.sh`, `dev-stack.sh`, `dev-native.sh`, `dashboard.py`).

## Usage

From a Claude Code session at your repo root, just ask for two things in parallel — the
skill's trigger phrases include "song song" / "chạy song song" / "in parallel" / "multiple
live previews". Or drive it directly:

```bash
parallel-task.sh start my-task docker   # or: native
parallel-task.sh list
parallel-task.sh stop my-task
parallel-task.sh rm my-task             # --force to discard uncommitted changes
```

## Dashboard

See every running copy's live status and output in one page:

```bash
dashboard.py            # http://127.0.0.1:4400
dashboard.py 5000        # custom port
```

Lists each task's dev-stack status (`dev_status`, from the docker/native port checks) alongside
its dispatched session's live status (`agent_status`: `idle`/`busy`, from `claude agents --json`),
and streams that session's transcript as a readable log — click a task to follow it.

## Troubleshooting

See the [Troubleshooting section](skills/parallel-worktree-run/SKILL.md#troubleshooting) in
the skill — covers a `start` failing partway (auto-rolled-back since v1.0.0), port collisions
from an unoffset compose service, and how to verify a copy's isolation actually held (not
just that its frontend returns 200).
