<!--
  The prompt a scheduled session runs to refresh the manager board artifact.

  Cron id: 093cac6a (created 2026-09-07, cadence 7,22,37,52 * * * *)

  What the operator should set before scheduling, because none of it belongs in a shipped file:
    ARTIFACT_URL — the board published from bin/board.html with capabilities {db: {}}
    PWR_ADO_ASSIGNED_TO — only if one person holds more than one ADO identity. Unset, the
      query falls back to WIQL's @Me, which resolves to whichever identity `az` authenticated
      and silently omits every ticket assigned to the other one.
    PWT_REPO_ROOT — recommended. Step 1 has no cwd of its own; board_state.py resolves the
      plugin's registry (branch/worktree/short_id/ado_refs per session) against this env var
      first, then falls back to whatever cwd the scheduled session happens to have. Unset, a
      session with the wrong cwd silently nulls out those fields on every session document
      rather than failing loudly — set it to this repo's absolute path to pin the join.
-->

Refresh the manager board. Do exactly this and nothing else.

1. Run: `python3 <plugin bin dir>/board_state.py`
   It prints a JSON array of write entries on stdout. Each entry is already shaped as
   `{op, collection, doc_id, data}` — pass them through unchanged. Do not sort, filter or
   reorder them: `meta/status` is deliberately last, because it asserts the rows beside it are
   current, and a batch that dies halfway must never have already claimed a sweep whose rows
   never landed.

2. Write those entries to the artifact at `<ARTIFACT_URL>` using the Artifact tool's `write_db`
   with `db_op: "batch"`. A batch takes at most 50 entries — if there are more, split them in
   order, keeping the `meta/status` entry in the final batch.

3. Report one line: how many documents were written, and the value of `last_ado_sweep`.

Do not publish the page. Do not edit any file. If step 1 fails, report the error and stop — a
failed refresh must leave the previous rows standing rather than write partial state.

<!--
  Scheduling

  Every 15 minutes, on the off-minutes 7/22/37/52 rather than the quarter marks — every job that
  asks for "every 15 minutes" lands on :00/:15/:30/:45, and there is no reason to join that queue.
  The cadence is set by the ADO sweep, which is the only slow source; sessions and escalations
  reach the board through the manager on events, not through this job.

  KNOWN LIMIT — CronCreate is session-scoped. The job lives in the Claude session that created
  it: nothing is written to disk, it dies when that session exits, and it auto-expires after
  seven days regardless. That is fine for trying the cadence out, and NOT enough for a board
  meant to keep itself current unattended. For that, run the same command from a systemd user
  timer or a real crontab entry, which survives both.

  What this costs: one Claude session per run, ~96 runs a day. Stop it with CronDelete and the
  id recorded at the top of this file; `CronList` finds it again if that line was lost.
-->
