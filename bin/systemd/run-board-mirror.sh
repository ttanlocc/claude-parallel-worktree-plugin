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
# needs Bash permission (see the permission-mode comment below).
#
# board_mirror_diff.py, also run right here rather than inside the session, cuts board_state.py's
# full write set down to only what changed since the last successful run (see its own docstring
# for why) — the session downstream still just replays whatever array it's handed, same as before.
#
# Failure must be loud: any of {missing env, board_state.py failing, a broken claude -p
# invocation, a reply with no REFRESH_OK line anywhere in it} exits non-zero, so a failed run
# shows up as `systemctl --user status` "failed" and in the journal — not as a quietly stale
# board.
set -euo pipefail

# README step 2 symlinks this script into ~/.config/board-mirror/ — dirname on BASH_SOURCE alone
# would resolve against the symlink's location, not the repo, and go looking for board-mirror.md
# next to ~/.config. readlink -f follows the symlink to the real file first.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PLUGIN_BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

: "${ARTIFACT_URL:?ARTIFACT_URL is not set — see bin/systemd/board-mirror.env.example}"
: "${PWT_REPO_ROOT:?PWT_REPO_ROOT is not set — unset, board_state.py silently nulls branch/worktree/short_id/ado_refs instead of failing (see bin/systemd/board-mirror.env.example)}"

# Outside the repo, next to the env file from README step 1 — not tracked, not shipped, survives
# a `git pull`. Holds every doc_id this job wrote last time it actually succeeded, so this run can
# tell "unchanged" from "changed" and "gone" apart. Missing (first install) or unreadable (never
# synced, or a prior snapshot write itself failed) both mean "no previous run" to
# board_mirror_diff.py, which sends everything then — same as today's behavior, just once.
SNAPSHOT_PATH="$HOME/.config/board-mirror/last-writes.json"

# stdout only: board_state.py's own diagnostics (e.g. a reader falling back) go to stderr and
# are left to flow straight into the journal, not merged in here — merging would splice that text
# into the middle of the JSON array WRITES needs to stay parseable.
if ! WRITES="$(python3 "$PLUGIN_BIN_DIR/board_state.py")"; then
  echo "run-board-mirror: board_state.py exited non-zero (see its stderr above)" >&2
  exit 1
fi

# Cuts $WRITES (every document, every run) down to only what changed since the snapshot below was
# last written, plus a `delete` for every doc_id the snapshot remembers that isn't in $WRITES at
# all anymore — see board_mirror_diff.py's docstring. This is the fix for the 216-document,
# every-5-minutes batch that used to take 223s end to end and never deleted anything (orphaned
# rows piled up until someone cleaned 50 of them out by hand).
if ! DIFF_WRITES="$(python3 "$SCRIPT_DIR/board_mirror_diff.py" diff "$SNAPSHOT_PATH" <<<"$WRITES")"; then
  echo "run-board-mirror: board_mirror_diff.py failed to compute the diff" >&2
  exit 1
fi

PROMPT="$(
  sed '/<!--/,/-->/d' "$PLUGIN_BIN_DIR/board-mirror.md" \
    | sed "s#<ARTIFACT_URL>#$ARTIFACT_URL#g"
)"
# Plain prefix/suffix splitting, not sed and not `${PROMPT//pat/$DIFF_WRITES}`: $DIFF_WRITES is
# arbitrary JSON and its escaped backslashes are exactly the kind of content both of those
# reinterpret — sed's replacement treats `&`/`\` specially, and bash's `${.../pat/string}`
# replacement does its own backslash escaping too (a literal `\"` in $DIFF_WRITES can lose a
# backslash). `${var%%pat*}` / `${var#*pat}` only ever match `pat` to find a split point; the
# pieces are then joined by plain `${var}` expansion, which never reinterprets its contents.
PROMPT="${PROMPT%%<WRITE_ENTRIES_JSON>*}${DIFF_WRITES}${PROMPT#*<WRITE_ENTRIES_JSON>}"

# A systemd --user unit's PATH is whatever the user manager started with, not this shell's — it
# usually does NOT include ~/.local/bin. Default to the absolute path rather than bare `claude`.
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

# `claude -p` is non-interactive: nothing can click "approve" a permission prompt, so without a
# grant covering every tool the prompt actually uses, the run dies with "requires permission
# approval that was not granted". The prompt above only ever asks Claude to write_db —
# board_state.py and board_mirror_diff.py both run in this shell, not inside the session.
#
# `--allowedTools Artifact` looks like it should be enough and ISN'T — verified live (2026-09-08)
# with a real `claude -p` run that actually tried to write_db one document and read back the
# result:
#   --allowedTools Artifact                                            -> PROBE_FAIL:permission_denied
#   --permission-mode bypassPermissions
#     --disallowedTools Bash Edit Write Agent Workflow Skill ToolSearch -> PROBE_OK
# Artifact IS present in a headless session's tool list, but write_db is still a separate
# approval that --allowedTools has no specifier for (unlike Bash's command patterns or WebFetch's
# domain matching, Artifact has no per-action allow syntax) — so `Artifact` alone still hits the
# interactive-approval wall with nothing able to click through it, and the run dies exactly like
# an ungranted tool would. `--permission-mode bypassPermissions` clears every such approval;
# `--disallowedTools` then narrows back down explicitly (this one-line "write_db a batch" prompt
# has no business touching Bash/Edit/Write/Agent/Workflow/Skill/ToolSearch), which is why this is
# bypass-then-restrict rather than the wide-open default bypassPermissions would otherwise be.
if ! RAW_OUTPUT="$("$CLAUDE_BIN" -p --permission-mode bypassPermissions \
  --disallowedTools Bash Edit Write Agent Workflow Skill ToolSearch \
  --output-format json -- "$PROMPT" 2>&1)"; then
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

# board-mirror.md now asks for a bare one-line reply, but the model isn't guaranteed to comply —
# a live run answered with a lead-in sentence before the REFRESH_OK line. Match REFRESH_OK: as a
# substring of any line, not just an exact-match whole string, so a stray prefix doesn't get read
# as failure (which, via the snapshot-only-on-success write below, would silently defeat the
# whole diff-instead-of-full-resend point of this script forever). REFRESH_FAILED or no
# REFRESH_OK anywhere still falls through to the failure branch below.
OK_LINE="$(grep -m1 'REFRESH_OK:' <<<"$RESULT" || true)"
if [[ -n "$OK_LINE" ]]; then
  echo "run-board-mirror: $OK_LINE"
  # Snapshot the FULL write set (not $DIFF_WRITES) only now, after the write actually landed —
  # this is $WRITES, board_state.py's complete output, so next run's diff has every current
  # doc_id to compare against, not just the ones this run happened to send. Updating this before
  # the write, or when the write failed, would make the next run believe it's already synced and
  # silently skip real changes forever — a stale board with no error anywhere, the exact kind of
  # quiet failure that cost a full day before (see board-mirror.md's Scheduling section). A
  # failure here does NOT fail the run: the refresh itself already succeeded, so this only means
  # next run resends everything instead of just the diff — safe, just not free.
  if ! python3 "$SCRIPT_DIR/board_mirror_diff.py" snapshot "$SNAPSHOT_PATH" <<<"$WRITES"; then
    echo "run-board-mirror: refresh succeeded but snapshot update failed — next run will resend everything" >&2
  fi
  exit 0
fi

echo "run-board-mirror: refresh did not report success: $RESULT" >&2
exit 1
