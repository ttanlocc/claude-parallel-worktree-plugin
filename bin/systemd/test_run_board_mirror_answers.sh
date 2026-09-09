#!/usr/bin/env bash
# assert-based checks for how run-board-mirror.sh wires the return path in — the two properties
# that are structural rather than logical, so a unit test on board_mirror_answers.py cannot see
# them:
#   1. bringing answers down cannot fail the run (the script is `set -e`; an unguarded call would)
#   2. it happens BEFORE the REFRESH_OK branch that decides this run's exit code, so a mirror
#      that failed upward still lets a decided answer come down.
# Check 3 runs the script's own guard text, verbatim, against a deliberately failing step.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMP="$SCRIPT_DIR/run-board-mirror.sh"

fail() { echo "FAIL $1" >&2; exit 1; }

grep -q '^if ! .*board_mirror_answers\.py' "$PUMP" \
  || fail "board_mirror_answers.py is not called inside an 'if !' guard — under set -e it would abort the run"
echo "ok the return path runs inside a failure-tolerating guard"

answers_at="$(grep -n 'board_mirror_answers\.py' "$PUMP" | head -1 | cut -d: -f1)"
ok_at="$(grep -n '^OK_LINE=' "$PUMP" | head -1 | cut -d: -f1)"
[[ -n "$answers_at" && -n "$ok_at" ]] || fail "could not locate both the answers step and the OK_LINE branch in $PUMP"
(( answers_at < ok_at )) || fail "the answers step runs after the OK_LINE branch — a failed mirror would skip it"
echo "ok answers come down before the mirror's own success/failure branch decides the exit code"

STUB="$(mktemp -d)"
trap 'rm -rf "$STUB"' EXIT
printf '#!/usr/bin/env bash\necho "stub python3 says no" >&2\nexit 3\n' > "$STUB/python3"
chmod +x "$STUB/python3"

FRAGMENT="$(awk '/^if ! .*board_mirror_answers\.py/,/^fi$/' "$PUMP")"
[[ -n "$FRAGMENT" ]] || fail "could not extract the answers guard from $PUMP"

OUT="$(PATH="$STUB:$PATH" SCRIPT_DIR="$SCRIPT_DIR" RESULT="REFRESH_OK: wrote 4 documents" \
  bash -c "set -euo pipefail
$FRAGMENT
echo STILL_RUNNING" 2>&1)" || fail "the answers guard aborted the run when its step failed"
grep -q STILL_RUNNING <<<"$OUT" || fail "the run did not continue past a failing answers step: $OUT"
echo "ok a failing return path leaves the rest of the run alive"

echo "all checks passed"
