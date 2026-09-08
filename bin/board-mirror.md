<!--
  The prompt a scheduled session runs to refresh the manager board artifact.

  Scheduled via a systemd user timer — see bin/systemd/README.md for install steps. This file's
  body (below, outside the HTML comments) is fed to `claude -p` verbatim by
  bin/systemd/run-board-mirror.sh, placeholders substituted — it is not just documentation, it
  IS the prompt, so edit the instructions with that in mind.

  run-board-mirror.sh runs board_state.py itself (it's plain bash — no reason to make a headless
  LLM session invoke a script the shell can call directly) and substitutes its JSON output into
  <WRITE_ENTRIES_JSON> below. That keeps this prompt's only job "write this array to the
  artifact", so `claude -p`'s --allowedTools only ever needs to grant Artifact, never Bash.

  What the operator should set before scheduling, because none of it belongs in a shipped file:
    ARTIFACT_URL — the board published from bin/board.html with capabilities {db: {}}
    PWR_ADO_ASSIGNED_TO — only if one person holds more than one ADO identity. Unset, the
      query falls back to WIQL's @Me, which resolves to whichever identity `az` authenticated
      and silently omits every ticket assigned to the other one.
    PWT_REPO_ROOT — recommended. run-board-mirror.sh has no cwd of its own; board_state.py
      resolves the plugin's registry (branch/worktree/short_id/ado_refs per session) against
      this env var first, then falls back to whatever cwd the scheduled run happens to have.
      Unset, the wrong cwd silently nulls out those fields on every session document rather
      than failing loudly — set it to this repo's absolute path to pin the join.
-->

Refresh the manager board. Do exactly this and nothing else.

1. Write these entries to the artifact at `<ARTIFACT_URL>` using the Artifact tool's `write_db`
   with `db_op: "batch"`. Each entry is already shaped as `{op, collection, doc_id, data}` — pass
   them through unchanged. Do not sort, filter or reorder them: `meta/status` is deliberately
   last, because it asserts the rows beside it are current, and a batch that dies halfway must
   never have already claimed a sweep whose rows never landed. A batch takes at most 50 entries —
   if there are more, split them in order, keeping the `meta/status` entry in the final batch.

   <WRITE_ENTRIES_JSON>

2. Report exactly one line, starting with one of these two exact prefixes so a script can tell
   success from failure without parsing prose:
   - `REFRESH_OK: wrote <N> documents, last_ado_sweep=<value>` on success.
   - `REFRESH_FAILED: <error>` if the write fails.

Do not publish the page. Do not edit any file.

<!--
  Scheduling

  Every 5 minutes, on :02/:07/:12/.../:57 rather than the round marks — every job that asks for
  "every 5 minutes" lands on :00/:05/:10/..., and there is no reason to join that queue. See
  bin/systemd/board-mirror.timer's OnCalendar for the actual schedule; keep this note and that
  file in agreement if either changes.

  This was 15 minutes, set by the ADO sweep being the only slow source. The CTO tightened it to
  3 minutes because the board's planned "Refresh" button runs inside the sandboxed artifact page,
  which cannot pull ADO itself — it can only replay whatever this job already wrote — so this
  job's own interval is the ceiling on how fresh a manual refresh can ever look. The owner then
  eased it back to 5 minutes: at 3 minutes this was ~480 `claude -p` sessions a day just to shuttle
  a JSON array into the artifact db, too expensive for a board people glance at a few times a day;
  5 minutes cuts that to ~288 while staying fresh enough. Sessions and escalations still reach the
  board through the manager on events, not through this job; only the ADO-sourced rows depend on
  this cadence.

  KNOWN LIMIT (retired) — CronCreate is session-scoped. The job lived in the Claude session that
  created it: nothing was written to disk, it died when that session exited, and it auto-expired
  after seven days regardless. That is exactly what happened, silently, for most of a day — the
  incident that prompted the move below.

  Replaced by bin/systemd/board-mirror.timer + board-mirror.service, which run independently of
  any Claude session and survive it exiting. See bin/systemd/README.md for install steps and the
  one prerequisite (`loginctl enable-linger`) that step needs and must not be skipped.
-->
