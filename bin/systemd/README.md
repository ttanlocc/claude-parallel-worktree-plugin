# board-mirror systemd user timer

Runs the refresh described in `bin/board-mirror.md` every 5 minutes, independent of any Claude
session — replaces the session-scoped `CronCreate` job that died when its session exited (see
board-mirror.md's Scheduling section for what that cost).

## 0. Prerequisite — enable linger (needs a higher-privileged account, MANDATORY)

```
loginctl enable-linger azureuser
```

This needs root or sudo — a normal user cannot enable-linger for themselves. **Do not skip this
step or treat it as optional.** Without it, `systemctl --user` services are tied to a logind
session: they stop the moment the last login session for this user ends (SSH disconnect,
terminal closed), which is exactly the failure this timer exists to fix, just delayed until
logout instead of session exit. Confirm it took effect:

```
loginctl show-user azureuser -p Linger
```

Expect `Linger=yes`. If you cannot run `loginctl enable-linger` yourself, ask whoever has root on
this box to run it, and don't consider this setup done until `Linger=yes` comes back — a timer
installed without it will look correct today and die at the next logout.

## 1. Config file (outside the repo, real values only here)

```
mkdir -p ~/.config/board-mirror
cp bin/systemd/board-mirror.env.example ~/.config/board-mirror/env
chmod 600 ~/.config/board-mirror/env
$EDITOR ~/.config/board-mirror/env   # fill in ARTIFACT_URL and PWT_REPO_ROOT for real
```

Leave `CLAUDE_CODE_ENTRYPOINT=claude-desktop` as the example file sets it — don't delete it and
don't change the value. Without it, `claude -p` has no `Artifact` tool at all (not a denied
permission, the tool doesn't exist for that session), and every run dies at the write step with
the misleading `"Artifact tool is not available in this session"`. If you ever see that exact
message in the journal, this is the first thing to check.

`run-board-mirror.sh` also keeps a snapshot of what it has written at
`~/.config/board-mirror/last-writes.json`, next to `env` — not something you create, it writes
itself as each 50-document batch lands, and only sends changed documents (plus deletes for ones
that dropped out) from then on. Delete it to force a full resync on the next run; losing it just
costs one full-size run spread over a few runs, it does not lose data. Point
`BOARD_MIRROR_SNAPSHOT` somewhere else for a dry run — never let a test write this file, since it
is what tells the next real run "already synced".

If you hold more than one ADO identity, also set `PWR_ADO_ASSIGNED_TO` in that file to a
comma-separated list of all of them. Left unset, the WIQL query falls back to `@Me`, which
matches only the identity `az` is currently logged in as — tickets under any other identity
of the same person go missing from the board with no error.

## 2. Symlink the run script and unit files

```
ln -sf "$(pwd)/bin/systemd/run-board-mirror.sh" ~/.config/board-mirror/run-board-mirror.sh
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/bin/systemd/board-mirror.service" ~/.config/systemd/user/board-mirror.service
ln -sf "$(pwd)/bin/systemd/board-mirror.timer" ~/.config/systemd/user/board-mirror.timer
```

Symlinks, not copies: re-running these two commands after a `git pull` is the entire upgrade
path, no reinstall step needed.

## 3. Enable and start

```
systemctl --user daemon-reload
systemctl --user enable --now board-mirror.timer
```

## 4. Verify

```
systemctl --user list-timers board-mirror.timer
systemctl --user status board-mirror.service
journalctl --user -u board-mirror.service -n 50
```

A healthy `list-timers` row shows a `NEXT` a few minutes out and a `LAST`/`PASSED` from the run
before it. `status` on the service after a run shows `Active: inactive (dead)` with
`Main PID: ... (code=exited, status=0/SUCCESS)` — `oneshot` units go back to inactive between
runs, that's expected, not a failure. A failed refresh (bad env, `board_state.py` erroring inside
the session, a malformed `claude -p` reply) shows as `status=1/FAILURE` here and the reason in the
journal line above it — see `run-board-mirror.sh` for what each exit path logs.

### What the board reads: two clocks, two meanings

The refresh writes two `meta` documents, at opposite ends of the run, and they answer different
questions. A page that reads only one of them will get the other one's answer wrong.

| document | written | fields | means |
|---|---|---|---|
| `meta/pump` | first batch, **every run** | `ran_at` (epoch seconds), `batches_pending` (int ≥ 1) | the pump is alive and started a run at `ran_at`, with `batches_pending` batches to get through. `batches_pending == 1` means this run expects to finish. |
| `meta/status` | last batch, **only a run that finished** | `written_at`, `last_ado_sweep`, `last_session_scan`, `pump_period_s`, … | every row beside it is complete as of these stamps. |

So: **`meta/pump.ran_at` is the liveness signal, `meta/status.written_at` is the completeness
signal.** A staleness alarm must be sized off `ran_at` — that is the one that moves every 5
minutes. `written_at` legitimately sits still for the length of a backlog drain, and treating
that as "the pump has stopped" raises a false alarm on a healthy pump, which is worse than no
alarm at all because people learn to ignore it. Use `batches_pending > 1` to say the board is
still catching up rather than to say it is dead.

`meta/pump` deliberately carries no "as of" clock. It rides the first batch, so it lands before
the data does; if it stamped a sweep time, a partial run would be claiming rows it has not
written yet — the exact thing `meta/status` going last exists to prevent.

A journal line reading `run-board-mirror: PARTIAL: wrote 50 documents in 1 of 4 batches, ...` is
NOT a failure and exits 0. After a large change the refresh is split into 50-document batches and
a run does as many as fit in its budget, recording each one as it lands; the next fire picks up
the rest, and the backlog shrinks every run until a plain `REFRESH_OK` line comes back. Only that
`REFRESH_OK` line means the board is fully current — `PARTIAL` deliberately never writes
`meta/status`, so the board keeps showing the older sweep rather than claiming a fresh one over
rows that have not landed yet. Several `PARTIAL` runs in a row are expected after a big change;
`PARTIAL` runs that never reach `REFRESH_OK` are not, and mean the backlog is growing faster than
one run can drain it.

To force one run without waiting for the timer: `systemctl --user start board-mirror.service`.
If a run is already in flight, the second one logs `another run holds ... — stepping aside` and
exits 0 rather than racing it: two runs reading the same snapshot compute their diffs against
states that have already moved apart, and write over each other.
