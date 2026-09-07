# Session messaging — design spec

Date: 2026-09-07
Status: draft (design), pre-implementation

## Problem

Every manager turn today spawns a fresh `claude -p --resume <session_id>` process. The session id
is stable — the manager genuinely is one long-lived conversation, exactly as
`2026-08-28-engineering-manager-design.md` specified — but the **process** is not. `--resume`
replays the whole transcript on every call.

Measured 2026-09-07 on the live manager session:

```
session_id : 39f8c352-…  (one session since 2026-08-28, 9 days)
transcript : 37 MB, 65,009 lines
one turn   : 4m30s to answer the single word "pong"
```

That cost is structural, not a model setting. Lowering effort helps at the margin; the replay does
not go away. Meanwhile the manager cannot reach a worker that is *currently running* at all —
`--resume` only lands on a session that has stopped — so a worker heading down the wrong path
cannot be redirected until it finishes.

The 2026-08-25 spec recorded the constraint that forced this shape:

> **There is no `claude send` subcommand.** No supported way to push a message into a *live*
> session from outside. This is why the manager is spawned fresh per report rather than kept
> running: a long-lived manager would need exactly the mechanism that does not exist.

That finding was correct for the CLI and still is. It is no longer correct for the harness: a
Claude Code session now has `ListAgents` + `SendMessage`, which address other sessions on the same
machine by name. The premise the current architecture rests on has expired.

## Verified feasibility (spikes run 2026-09-07 — settled, do not re-test)

Run with throwaway `claude --bg` sessions in a scratch dir; every claim below is an observed file
written by the session under test, not an inference.

- **A `claude --bg` session has both `SendMessage` and `ListAgents`.** Confirmed by a probe session
  reporting its own tool availability. This is the gate: without it nothing else matters.
- **An idle (`state: done`) background session receives a message and wakes to act on it.** Target
  finished its prompt, went idle, then wrote `RECEIVED|spike-sender|SPIKE1-PING-7fe2` after a
  message arrived.
- **Delivery is asynchronous, 10–35 s in observation.** The first check ~0 s after the sender
  returned found nothing; the file appeared on a later poll. **`SendMessage` returning
  `{"success": true, "msg_id": …}` means queued, not processed.**
- **A message delivered mid-tool does not cost the target its current work.** Target was inside a
  90-second `sleep`; it recorded
  `RECEIVED|spike-busy-sender|SPIKE2-MIDTOOL-a91c|Step 2 (sleep 90 running in background)` **and**
  still completed all three steps of its original prompt. This is the capability `--resume` cannot
  offer at all.
- **A second message to the same session, ~12 minutes idle, also lands.** Both pings are in the
  target's file.
- **A `cmew`/tmux session did NOT appear in `ListAgents`** ~4 minutes after `cmew new`, while every
  `claude --bg` and every interactive session did. Unresolved: registration delay vs. tmux sessions
  not registering at all. **Do not design on cmew until this is settled** — `--bg` is proven and is
  already what `parallel-task.sh` uses.
- **A background session can sit in `waiting` (blocked on a permission prompt) and a message to it
  still reports success.** Two spike sessions ended there. `ListAgents` exposes the state
  (`spike-again [11eb55] · bg · waiting`), so it is detectable — but only if something looks.

## Scope

**In:** manager becomes a live session; manager↔worker traffic moves to `SendMessage`; manager
reads `ListAgents` for worker liveness.

**Out:** the ledger. `escalations.jsonl` and `assignments.jsonl` stay exactly as they are, and every
message that changes state still writes a record. `SendMessage` is transport; it leaves no history,
and the history is what makes this system debuggable. Two bugs were diagnosed from those files on
2026-09-07 alone — a `blocked_on_credentials` escalation mis-scored P1, and a two-day OAuth failure
chain. Neither would have been reconstructable from a message bus.

**Out:** worker spawning. `claude --bg` already works, is already used, and is untouched.

## Architecture

```
today                              proposed
─────                              ────────
person → dashboard                 person → dashboard
  → Python daemon                    → Python daemon
    → claude -p --resume  (4m30s)      → SendMessage         (queued, ~10-35s)
      → manager (cold)                   → manager (LIVE session, warm context)
        → claude --resume  (worker         → SendMessage      (reaches a RUNNING worker)
           must be stopped)                  → worker
                                    worker → ledger append → daemon poll → manager
```

The inversion: the manager stops being a subprocess the daemon invokes and becomes a peer the
daemon messages. Python shrinks to ledger + dashboard + delivery.

### What has to be true for the manager to be a live session

It must be started as `claude --bg -n manager` and stay resident. Three consequences:

1. **Nobody is holding its stdout.** Its replies do not come back to the caller. Today
   `ask_result()` returns `(ok, text)` and callers branch on it. After the change, a reply arrives
   later, out of band. Every caller that reads the manager's answer synchronously has to be
   rewritten around the ledger instead.
2. **It can die.** A resident session is a process; the daemon must detect its absence
   (`ListAgents` / `claude agents --json`) and restart it, re-briefing from the charter.
3. **`flock` stops being the concurrency model.** Serialization came free from one-process-at-a-time.
   A live session receiving two messages handles them in its own order; anything that needs
   ordering has to say so in the message.

## Components

### `bin/manager_session.py` — rewritten

`ask_result()` currently: acquire flock → build argv → subprocess → parse JSON → return `(ok, text)`.

Becomes: append the outgoing turn to the chat log → deliver → return `(queued, msg_id)`. The reply
is no longer this function's to return.

`ask_argv()` survives for one purpose only: **bootstrap**, when no live manager exists and one must
be started with the charter. The `--resume` path stays as the documented fallback for when
messaging is unavailable, behind an env flag, because a broken transport must not leave the CTO
with no manager at all.

### Delivery — the piece Python cannot do

`SendMessage` is a harness tool. `manager_daemon.py` is plain Python; it cannot call it. This is the
same class of constraint the 2026-08-25 spec hit from the other direction, and it must not be
hand-waved.

Two candidate shapes, to be decided before implementation:

- **A: the manager polls.** Python only appends to the ledger; the live manager session watches it
  and messages workers itself. Python needs no new capability at all — but the manager must run a
  polling loop indefinitely, and a session that is polling is a session burning tokens.
- **B: a thin sender session.** Python spawns a short-lived `claude --bg` whose only job is one
  `SendMessage`, the way the spikes did it. Costs a process per message; needs no long-running
  poller. The spikes prove it works; the cost has not been measured.

Recommendation: **B for the daemon's outbound traffic** (few messages, each already worth a process
today), **A never for the whole system** — a permanently polling manager is the token-burn version
of the problem being solved.

### Worker liveness

The manager reads `ListAgents` before messaging and branches on state:

| state | meaning | manager does |
|---|---|---|
| `idle` / `done` | finished its turn | message it; it will wake |
| `running` | mid-work | message it; work is not lost (spike 2) |
| `waiting` | blocked on a permission prompt | **do not** treat as working — escalate |
| absent | dead or never started | re-dispatch |

`waiting` is the new one. Today nothing distinguishes a blocked worker from a busy one, and a
message to it succeeds while nothing happens.

### `bin/assignments.py`, `bin/escalations.py` — unchanged

Deliberately. See Scope.

## Errors and degradation

- **Message queued but never processed.** `success` is not delivery. Every message that expects an
  outcome gets a ledger record with a deadline; the daemon's existing tick already chases records
  past their ETA, so this needs no new mechanism — only that messages keep writing records.
- **Manager session gone.** Detect via `ListAgents`; restart with the charter; announce the restart
  in the chat log so the CTO knows context was lost.
- **Transport unavailable.** Fall back to `claude -p --resume`. Slower, proven, still correct.

## Testing

- Spike-equivalent tests cannot run in CI (they need real sessions). Keep the transport behind a
  port so the ledger/decision logic stays testable with a fake sender, matching how `ask` is
  already injected in `test_dashboard.py`.
- One end-to-end smoke run, manual, before switching the default.

## Known ceilings

**Reply latency replaces reply blocking.** 10–35 s to deliver is not instant; it is just not 4m30s,
and it does not grow with transcript length.

**Ordering is not guaranteed.** Two messages sent close together may be handled in either order.
Anything order-dependent must be one message.

**`cmew` is unproven.** Not visible to `ListAgents` in the one test run. Needs its own spike before
any design depends on it.

**Live-session cost is unmeasured.** A resident manager holds context; whether that is cheaper than
replaying 37 MB depends on how the harness bills an idle session. Measure before committing.

## Files

- `bin/manager_session.py` — rewritten around queued delivery
- `bin/manager_daemon.py` — manager liveness + restart; sender shape B
- `docs/superpowers/specs/2026-08-25-manager-tier-design.md` — annotate: the "no mechanism exists"
  finding is superseded for the harness, still true for the CLI
- ledger modules — untouched
