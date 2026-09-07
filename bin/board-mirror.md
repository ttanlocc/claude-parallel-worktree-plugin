<!--
  The prompt a scheduled session runs to refresh the manager board artifact.

  Cron id: (recorded on first schedule — see "Scheduling" at the bottom)

  Two things the operator must set before scheduling, because neither belongs in a shipped file:
    ARTIFACT_URL — the board published from bin/board.html with capabilities {db: {}}
    PWR_ADO_ASSIGNED_TO — only if one person holds more than one ADO identity. Unset, the
      query falls back to WIQL's @Me, which resolves to whichever identity `az` authenticated
      and silently omits every ticket assigned to the other one.
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

  Every 15 minutes, via CronCreate from a session. The cadence is set by the ADO sweep, which is
  the only slow source; sessions and escalations reach the board through the manager on events,
  not through this job.

  What this costs: one Claude session per run, ~96 runs a day. Stop it with CronDelete and the
  id recorded at the top of this file; `CronList` finds it again if that line was lost.
-->
