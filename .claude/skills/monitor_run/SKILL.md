---
name: monitor_run
description: >-
  Monitor an already-running AgentShore session in a given project directory.
  Check in every 20 minutes, surface errors and inefficiencies, file new bugs
  to GitHub as you go, auto-stop the session if it goes idle for two
  consecutive check-ins, and summarize the run when it ends. Does NOT start a
  session — it attaches to one the user already started.
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - ScheduleWakeup
---

# monitor_run

Babysit a live AgentShore session that the **user already started** in a
target directory. This skill never runs `agentshore start` — it observes,
reports, files bugs, and (only when the session wedges) runs `agentshore
stop`. Argument: the project directory, e.g. `/monitor_run
/path/to/my-project`.

> **CLI carve-out.** The repo rule forbids invoking `agentshore` CLI
> subcommands from Claude Code. `/monitor_run` is a named exception, and **only
> for `agentshore stop --project <DIR>`** — the graceful auto-stop in Step 5.
> Never run `agentshore start`, `dashboard`, `report`, etc. from this skill.

## Constants

```
DIR             = <the directory argument>            # project being monitored
LOG_DIR         = $DIR/.agentshore/logs               # NDJSON logs live here
CHECKIN_SECONDS = 1200    # 20 minutes between check-ins
STALE_AGE_S     = 300     # newest log line older than this + no process => exited
SNAPSHOT        = <this skill dir>/snapshot.py         # human-readable readout
PROGRESS        = <this skill dir>/progress.py         # machine-readable counters
STATE_FILE      = <os-temp-dir>/agentshore-monitor-<sanitized-DIR>.json
```

Resolve `<this skill dir>` to the directory holding this `SKILL.md`.
`<os-temp-dir>` = the platform temp directory (`$TMPDIR` or `/tmp` on
macOS/Linux, `$env:TEMP` on Windows). `<sanitized-DIR>` = `DIR` with path
separators replaced by `_` (keeps the state file unique per target, outside
the project tree so the monitored project is never touched).

## How the cadence works

Each invocation runs **one check-in cycle** and then schedules the next with
`ScheduleWakeup(delaySeconds=CHECKIN_SECONDS)`, passing the same `/monitor_run
<DIR>` prompt back so the loop re-enters in 20 minutes. All cross-check-in
state (previous counters, idle streak, issues already filed) lives in
`STATE_FILE`, so each wakeup resumes cleanly even after a context summary. When
the session exits or you auto-stop it, **do not** schedule another wakeup —
print the summary and finish.

---

## Step 0 — Resolve target and load state

1. Determine `DIR` from the argument. If no argument was given, read `DIR` from
   `STATE_FILE` (a resuming wakeup). If neither is available, tell the user the
   skill needs a directory and stop.
2. `test -d $DIR/.git` and `test -d $LOG_DIR` — if the log dir is missing, the
   project has no AgentShore session history; tell the user there's nothing to
   monitor and stop.
3. Load `STATE_FILE` if it exists. First run (no state file) → initialise:
   `checkin_count=0, prev_ok_plays=null, prev_loop_detected=0,
   idle_streak=0, filed_issues=[]`. Record `started_at` from `date +%s`.

## Step 1 — Locate the live log

The newest log in `$LOG_DIR` is the active session:

```bash
ls -t "$LOG_DIR"/agentshore-*.log 2>/dev/null | head -1
```

Pin this path as `LOG_FILE` for the cycle. If no log file exists yet, print
`no log yet`, schedule the next wakeup, and finish — the session may still be
booting.

## Step 2 — Read the status

Run both helpers against `LOG_FILE`:

```bash
python3 <SNAPSHOT> "$LOG_FILE"   # print this readout to the user verbatim
python3 <PROGRESS> "$LOG_FILE"   # capture the JSON line for the logic below
```

Show the user the snapshot block plus a one-line check-in header
(`Check-in #N — HH:MM`). The `progress.py` JSON drives Steps 3–6; its fields
are documented at the top of `progress.py` (`play_completed`, `ok_plays`,
`fail_plays`, `loop_detected`, `error_lines`, `traceback_lines`, `ended`,
`last_event_age_s`, …). Note `play_completed = ok_plays + fail_plays`; the
idle rule (Step 5) keys on `ok_plays`, not `play_completed`.

## Step 3 — Liveness / exit detection

The session has **exited** if any of these hold:

- `progress.ended == true` (a terminal `session_ended` / `shutdown_complete` /
  `drain_complete` / `session_shutdown` event is in the log), **or**
- No matching process is running **and** the log has gone stale:
  ```bash
  pgrep -fa "agentshore start"   # look for one whose cmdline points at $DIR
  ```
  If no `agentshore start` process corresponds to `$DIR` **and**
  `progress.last_event_age_s > STALE_AGE_S`, treat it as exited.

A live process with a fresh log (`last_event_age_s` small) is **running** even
if it's mid-play. If exited → go to **Step 7 (Summary)** and do not reschedule.

## Step 4 — Errors and inefficiencies

Compute deltas vs the previous check-in (`prev_*` from `STATE_FILE`):

**Errors** — investigate when any of these are present or rose since last time:
`error_lines > 0`, `traceback_lines > 0`, `asyncio_unretrieved > 0`. Read the
relevant slice of `LOG_FILE` (grep for `"level": "error"` / `"level":
"critical"` / `Traceback`) to find the first real failure, its source file/line,
and whether it traces into `src/agentshore/`. Distinguish:
- **Transient** (network timeout, `rate_limit`, subprocess killed, clean
  agent non-zero exit that the orchestrator recovered from) — note it, don't
  file.
- **AgentShore bug** (traceback into `src/agentshore/`, unhandled asyncio
  exception, persistence corruption) — file per **Step 6**.

**Inefficiencies** — flag and consider filing when you see, across check-ins:
- `loop_detected` rising — the wedge signal; pairs with the idle rule below.
- High / rising `ppo_selector.all_masked` or `selector_idle` with few new
  `play_completed` — PPO is spinning with nothing valid to pick.
- The same `play_type` failing repeatedly in the snapshot's "Last 5 plays".
- A single play_type dominating spend with no alignment progress.

Do **not** flag the known-healthy patterns: `gate_rejections`,
`refine_task_breakdown` running uncapped, and brief between-play agent idle are
expected — never file or auto-stop on those alone.

## Step 5 — Idle rule (auto-stop)

`new_ok = progress.ok_plays - prev_ok_plays` (skip on the
very first check-in, where `prev_ok_plays` is null — that one only sets
the baseline).

> **Key on `ok_plays`, not `play_completed`.** `play_completed` counts
> *failed* plays too, so an orchestrator stuck re-selecting a play that
> always fails (e.g. an `end_agent` self-deadlock spinning hundreds of
> failures) keeps incrementing `play_completed` and masquerades as healthy
> progress — the idle rule would never fire. `ok_plays` only counts
> successful plays, so a fast-failing spin-wedge correctly reads as idle.
> When `new_ok == 0` but `play_completed` is climbing, you're almost
> certainly looking at exactly this kind of wedge — say so in the check-in.

- A check-in is **idle** when `new_ok == 0` (no *successful* work finished
  since last check-in, regardless of how many failed plays completed). A
  rising `loop_detected` with `new_ok == 0`, or `fail_plays` climbing while
  `ok_plays` is flat, is the same idle condition, more strongly confirmed
  (wedged orchestrator).
- Idle check-in → `idle_streak += 1`. Any real progress (`new_ok > 0`) →
  `idle_streak = 0`.
- When `idle_streak >= 2` (two consecutive idle check-ins), **auto-stop**:
  ```bash
  agentshore stop --project "$DIR"
  ```
  This is a graceful drain and emits an end-of-session report. Announce why you
  stopped (idle for two check-ins; cite the counters). Then go to **Step 7**
  and do not reschedule.

> Idle for two check-ins ≈ ~40 minutes of no *successful* plays. This is the
> documented orchestrator-wedge remedy (an unattended wedged session burns API
> spend; see the loop-detection auto-stop intent, issue #9).

## Step 6 — File bugs as you go (GitHub, dedup first)

For each genuine AgentShore bug or inefficiency from Step 4, file it to GitHub
**immediately**, after checking for duplicates:

1. List open issues once per check-in and match by symptom:
   ```bash
   gh issue list --repo SuperSwinkAI/Swink-AgentShore --state open --limit 50
   ```
   Standing inefficiency issues already cover the recurring findings (refine
   loops, plan-blocked dispatch, merge_pr starvation, and similar). Match by
   symptom against the live list — don't rely on memorized issue numbers, they
   rot as issues close. If your finding matches an open one, add a brief comment
   with the fresh evidence (session id, counts, cost) instead of opening a new
   issue.
2. Also skip anything already in `filed_issues` in `STATE_FILE` (filed earlier
   this run).
3. Otherwise create it:
   ```bash
   gh issue create --repo SuperSwinkAI/Swink-AgentShore --label bug \
     --title "<concise symptom>" \
     --body "<what happened, session id, log path $LOG_FILE, relevant counters/costs, suspected cause>"
   ```
4. Record the issue number/title in `filed_issues` and report it in the
   check-in (`filed #NN — <title>`). Match the register of the existing
   inefficiency issues: concrete symptom, cost/efficiency impact, evidence.

## Step 7 — Persist state and schedule (or summarize)

**Always** write `STATE_FILE` with updated counters before finishing:
`checkin_count+1`, `prev_ok_plays`, `prev_loop_detected`, `idle_streak`,
`filed_issues`.

- **Session still running and not auto-stopped** → schedule the next cycle and
  finish the turn:
  `ScheduleWakeup(delaySeconds=1200, prompt="/monitor_run <DIR>", reason="next AgentShore monitor check-in")`.
- **Session exited (Step 3) or auto-stopped (Step 5)** → do not reschedule.
  Print the **Run Summary**, then delete `STATE_FILE` (`rm -f STATE_FILE`).

## Run Summary

When the run ends, print:

```
AgentShore Monitor Summary — <DIR>
──────────────────────────────────
Session:          <session_id>
Watched for:      <HH:MM:SS>   (<N> check-ins)
Ended by:         <clean exit | shutdown event | auto-stop (idle x2)>
Plays:            <play_completed> completed  (<ok_plays> ok / <fail_plays> fail)
Total cost:       $<from snapshot>
Errors seen:      <count + one-line each, transient vs bug>
Inefficiencies:   <bullet list of what you flagged>
Bugs filed:       <#NN title> ...  (or "none")
Last snapshot:    <the final snapshot.py block>
```

If the session was auto-stopped, state plainly that you stopped it and why.
