#!/usr/bin/env bash
# assert-based checks for parallel-task.sh's pure argument parsing. Run: bash bin/test_parallel_task.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/parallel-task.sh"

fail=0
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS $desc"
  else
    echo "FAIL $desc: expected [$expected] got [$actual]"
    fail=1
  fi
}

out="$(parse_start_args my-task native)"
assert_eq "no --ticket" "$(printf 'my-task\tnative\torigin/main\t[]')" "$out"

out="$(parse_start_args --ticket 8172 my-task native)"
assert_eq "one --ticket" "$(printf 'my-task\tnative\torigin/main\t["8172"]')" "$out"

out="$(parse_start_args --ticket 8172 --ticket 8165 my-task native some-ref)"
assert_eq "two --ticket + explicit base_ref" \
  "$(printf 'my-task\tnative\tsome-ref\t["8172","8165"]')" "$out"

if parse_start_args my-task 2>/dev/null; then
  echo "FAIL missing mode should return non-zero"
  fail=1
else
  echo "PASS missing mode returns non-zero"
fi

[[ $fail -eq 0 ]] && echo "all passed" || { echo "FAILURES ABOVE"; exit 1; }
