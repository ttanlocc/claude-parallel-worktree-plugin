#!/usr/bin/env bash
# run-board-mirror.sh — the ExecStart target for board-mirror.service.
#
# board-mirror.md is the single source of truth for the prompt; this script strips its
# HTML-comment scaffolding, fills in <ARTIFACT_URL>/<WRITE_ENTRIES_JSON>, and feeds the result to
# a one-shot `claude -p` run. Keeping the prompt in one place means the doc and the timer can
# never silently drift apart the way board-mirror.md's cadence and the old cron once did.
#
# board_state.py runs right here in bash, not inside the `claude -p` session: this script already
# has a shell, and running it here means a failure shows up as this script's own exit code instead
# of the model having to run a Bash step and report back — which also means `claude -p` never
# needs Bash permission, only Artifact (see the --allowedTools comment below).
#
# Failure must be loud: any of {missing env, board_state.py failing, a broken claude -p
# invocation, a reply that isn't exactly REFRESH_OK} exits non-zero, so a failed run shows up as
# `systemctl --user status` "failed" and in the journal — not as a quietly stale board.
set -euo pipefail

# README step 2 symlinks this script into ~/.config/board-mirror/ — dirname on BASH_SOURCE alone
# would resolve against the symlink's location, not the repo, and go looking for board-mirror.md
# next to ~/.config. readlink -f follows the symlink to the real file first.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PLUGIN_BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

: "${ARTIFACT_URL:?ARTIFACT_URL is not set — see bin/systemd/board-mirror.env.example}"
: "${PWT_REPO_ROOT:?PWT_REPO_ROOT is not set — unset, board_state.py silently nulls branch/worktree/short_id/ado_refs instead of failing (see bin/systemd/board-mirror.env.example)}"

# stdout only: board_state.py's own diagnostics (e.g. a reader falling back) go to stderr and
# are left to flow straight into the journal, not merged in here — merging would splice that text
# into the middle of the JSON array WRITES needs to stay parseable.
if ! WRITES="$(python3 "$PLUGIN_BIN_DIR/board_state.py")"; then
  echo "run-board-mirror: board_state.py exited non-zero (see its stderr above)" >&2
  exit 1
fi

PROMPT="$(
  sed '/<!--/,/-->/d' "$PLUGIN_BIN_DIR/board-mirror.md" \
    | sed "s#<ARTIFACT_URL>#$ARTIFACT_URL#g"
)"
# Plain prefix/suffix splitting, not sed and not `${PROMPT//pat/$WRITES}`: $WRITES is arbitrary
# JSON and its escaped backslashes are exactly the kind of content both of those reinterpret —
# sed's replacement treats `&`/`\` specially, and bash's `${.../pat/string}` replacement does its
# own backslash escaping too (a literal `\"` in $WRITES can lose a backslash). `${var%%pat*}` /
# `${var#*pat}` only ever match `pat` to find a split point; the pieces are then joined by plain
# `${var}` expansion, which never reinterprets its contents.
PROMPT="${PROMPT%%<WRITE_ENTRIES_JSON>*}${WRITES}${PROMPT#*<WRITE_ENTRIES_JSON>}"

# A systemd --user unit's PATH is whatever the user manager started with, not this shell's — it
# usually does NOT include ~/.local/bin. Default to the absolute path rather than bare `claude`.
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

# `claude -p` is non-interactive: nothing can click "approve" a permission prompt, so without a
# pre-grant covering every tool the prompt actually uses, the run dies with "requires permission
# approval that was not granted". The prompt above only ever asks Claude to write_db — board_state.py
# runs in this shell, not inside the session — so Artifact is both necessary and sufficient here.
# --allowedTools has no finer specifier for it (unlike Bash's command patterns or WebFetch's
# domain matching), so `Artifact` — covering all of its actions, not just write_db — is the
# narrowest grant this flag supports.
if ! RAW_OUTPUT="$("$CLAUDE_BIN" -p --allowedTools Artifact --output-format json -- "$PROMPT" 2>&1)"; then
  echo "run-board-mirror: claude -p exited non-zero: $RAW_OUTPUT" >&2
  exit 1
fi

RESULT="$(python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except ValueError as e:
    print(f"run-board-mirror: claude -p did not return valid JSON: {e}", file=sys.stderr)
    sys.exit(1)
result = payload.get("result") if isinstance(payload, dict) else None
if not isinstance(result, str):
    print("run-board-mirror: claude -p JSON had no string result field", file=sys.stderr)
    sys.exit(1)
print(result)
' <<<"$RAW_OUTPUT")" || exit 1

if [[ "$RESULT" == REFRESH_OK:* ]]; then
  echo "run-board-mirror: $RESULT"
  exit 0
fi

echo "run-board-mirror: refresh did not report success: $RESULT" >&2
exit 1
