---
name: monitor_run
description: >-
  Monitor an already-running AgentShore session in a given project directory.
  Check in every 30 minutes, surface errors and inefficiencies, file new bugs
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

Babysit a live AgentShore session that the **user already started** in a target
directory. Observes, reports, files bugs, and (only when the session wedges)
stops it. Argument: the project directory, e.g. `/monitor_run /path/to/my-project`.

> **CLI carve-out.** The repo rule forbids invoking `agentshore` CLI subcommands
> from Claude Code. `/monitor_run` is a named exception, and **only for
> `agentshore stop --project <DIR>`** — the graceful auto-stop in Step 5, and
> only for CLI-launched sessions. Never run `agentshore start`, `dashboard`,
> `report`, etc. from this skill.

## Constants

```
DIR             = <the directory argument>
LOG_DIR         = $DIR/.agentshore/logs
CHECKIN_SECONDS = 1800    # 30 minutes
STALE_AGE_S     = 300     # log older than this => likely exited
SNAPSHOT        = <this skill dir>/snapshot.py
PROGRESS        = <this skill dir>/progress.py
STATE_FILE      = <os-temp-dir>/agentshore-monitor-<sanitized-DIR>.json
AGENTSHORE_REPO = <the AgentShore source repo — NOT $DIR>
                  # python3 -c "import agentshore, pathlib; print(pathlib.Path(agentshore.__file__).parents[2])"
WATCH_FILE      = $AGENTSHORE_REPO/tmp/watch_items.md   # persistent, human-editable cross-session watch items
```

`<this skill dir>` = directory holding this `SKILL.md`. `<os-temp-dir>` = `$TMPDIR` / `/tmp`
(macOS/Linux) or `$env:TEMP` (Windows). `<sanitized-DIR>` = `DIR` with path separators
replaced by `_`.

## Cadence

Each invocation runs one check-in and schedules the next via
`ScheduleWakeup(delaySeconds=CHECKIN_SECONDS)` with the same `/monitor_run <DIR>` prompt.
All cross-check-in state lives in `STATE_FILE` so wakeups resume cleanly after context
summaries. When the session exits or is stopped, do not reschedule — print the summary
and finish.

---

## Step 0 — Resolve target and load state

1. Determine `DIR` from the argument; if none, read it from `STATE_FILE`. If neither is
   available, stop and ask the user for a directory.
2. Verify `test -d $DIR/.git` and `test -d $LOG_DIR`. If the log dir is missing, tell
   the user there is nothing to monitor and stop.
3. Load `STATE_FILE` if it exists. First run → initialise:
   `checkin_count=0, prev_ok_plays=null, prev_session_id=null, prev_loop_detected=0,
   idle_streak=0, launch_type=null, filed_issues=[], watch_items=[]`.
   Record `started_at` from `date +%s`.
4. **Derive watch items from AgentShore's recent commits** (first check-in only; reload
   from state on resumes). These are orchestrator regression candidates — **not** commits
   from `$DIR`. Run against `$AGENTSHORE_REPO`:
   ```bash
   git -C "$AGENTSHORE_REPO" log --since="24 hours ago" --oneline --no-merges
   ```
   Derive a short list of subsystems, plays, agent types, or fixes touched in the last
   day that a live session could exercise and break. Persist as `watch_items`. Surface
   once in the first check-in header (`Watching: <items>`).
5. **Load persistent watch items** from `WATCH_FILE` if it exists (`cat "$WATCH_FILE"`).
   These are cross-session patterns previously dispositioned as "watch, don't file yet",
   each with an **escalation trigger**. Merge them with the commit-derived items from
   step 4. On every check-in, evaluate the current log against each entry's escalation
   trigger: if a trigger is met, file/comment per Step 6 and remove the entry from
   `WATCH_FILE`. Surface any persistent-watch matches in the check-in findings.

## Step 1 — Locate the live log

```bash
ls -t "$LOG_DIR"/agentshore-*.log 2>/dev/null | head -1
```

Pin as `LOG_FILE`. If none exists, print `no log yet`, schedule the next wakeup, finish.

## Step 2 — Read the status

```bash
python3 <SNAPSHOT> "$LOG_FILE"   # print verbatim
python3 <PROGRESS> "$LOG_FILE"   # capture JSON
```

**Output rule:** For a healthy session, print only the snapshot block plus a 2-3 line
summary (session id, ok/fail counts, cost, next check-in time). Only expand with detail
when there are errors, a rising idle streak, or bugs to file.

**New session detection:** If `progress.session_id != prev_session_id` (AgentShore
restarted mid-run), reset `prev_ok_plays = progress.ok_plays` and `idle_streak = 0`,
note the new session, and skip the idle comparison this check-in. Update `prev_session_id`.

## Step 3 — Liveness / exit detection

Session has **exited** if either:

- `progress.ended == true` (terminal `session_ended` / `shutdown_complete` /
  `drain_complete` / `session_shutdown` event), **or**
- `progress.last_event_age_s > STALE_AGE_S` — the log has gone stale.

**`last_event_age_s` is the primary liveness signal.** A fresh log means the session
is running regardless of any process check.

**Detect launch type** and store as `launch_type` in `STATE_FILE` on first detection:
```bash
pgrep -fa "agentshore start"   # only finds CLI-launched sessions
```
- pgrep finds a process referencing `$DIR` → `"cli"`
- Log is fresh but pgrep finds nothing → `"desktop"` (desktop-app sessions run as a
  child of the GUI process and never appear in pgrep)

If exited → go to **Step 7 (Summary)**, do not reschedule.

## Step 4 — Errors and inefficiencies

Compute deltas vs the previous check-in. Errors or inefficiencies touching `watch_items`
are regression suspects — lower the filing threshold and name the implicated commit.

**Errors** — investigate when `error_lines`, `traceback_lines`, or `asyncio_unretrieved`
rose since last check-in. Grep `LOG_FILE` for `"level":"error"` / `"level":"critical"` /
`Traceback` to find the first real failure. Distinguish:
- **Transient** (network timeout, rate_limit, subprocess killed, clean non-zero exit the
  orchestrator recovered from) — note it, don't file.
- **AgentShore bug** (traceback into `src/agentshore/`, unhandled asyncio exception,
  persistence corruption) — file per Step 6.

**Inefficiencies** — flag and consider filing when:
- `loop_detected` is rising.
- The same `play_type` fails repeatedly in the last 5 plays.
- A single play_type dominates spend with no alignment progress.
- `all_masked` / `selector_idle` persists with **no running agents** and `ok_plays` flat
  across check-ins — that combination is a genuine wedge. A transient `all_masked`
  between plays while agents are live is normal; do not flag it.

**Never flag:** `gate_rejections`, `refine_task_breakdown` running frequently, or brief
between-play agent idle. These are expected healthy patterns.

## Step 5 — Idle rule (auto-stop)

Skip idle comparison when `prev_ok_plays` is null (first check-in) or when a new session
was detected in Step 2.

`new_ok = progress.ok_plays - prev_ok_plays`

> **Use `ok_plays`, not `play_completed`.** Failed plays increment `play_completed` — an
> orchestrator stuck fast-failing the same play looks like progress by that count but reads
> as idle here. When `new_ok == 0` but `play_completed` is climbing, that's a spin-wedge.

- `new_ok == 0` → idle; `idle_streak += 1`. A rising `loop_detected` confirms wedge.
- `new_ok > 0` → `idle_streak = 0`.

When `idle_streak >= 2` (~60 min of no successful plays), auto-stop — **branch on launch type**:

- **`"cli"`** → run `agentshore stop --project "$DIR"` (graceful drain). Announce why.
- **`"desktop"`** → do NOT run `agentshore stop` (no-op for desktop sessions). Instead
  report the wedge with counters and tell the user to stop the session via the desktop
  app UI.

Then go to **Step 7**, do not reschedule.

## Step 6 — File bugs (dedup first)

```bash
gh issue list --repo SuperSwinkAI/Swink-AgentShore --state open --limit 50
```

Match by symptom against the live list — don't rely on memorized issue numbers. If your
finding matches an open issue, add a comment with fresh evidence (session id, counts,
log path). Skip anything already in `filed_issues`.

Otherwise:
```bash
gh issue create --repo SuperSwinkAI/Swink-AgentShore --label bug \
  --title "<concise symptom>" \
  --body "<what happened, session id, log path $LOG_FILE, relevant counters/costs, suspected cause>"
```

Record the issue number in `filed_issues` and report it in the check-in (`filed #NN — <title>`).

**Watch, don't file (yet).** Some findings aren't ready to file — self-healed transients,
single occurrences adjacent to a known-sensitive area, or patterns whose materiality is
unproven. Instead of filing, append them to `WATCH_FILE` using the format documented at
the top of that file (title, first-seen, symptom, why-watch, **escalation trigger**,
evidence). The escalation trigger is mandatory: it's the condition a later check-in
checks (Step 0.5) to graduate the watch item into a filed issue. Report watch-only
dispositions in the check-in (`watch — <title>`).

## Step 7 — Persist state and schedule (or summarize)

Write `STATE_FILE`: `checkin_count+1`, `prev_ok_plays`, `prev_session_id`,
`prev_loop_detected`, `idle_streak`, `launch_type`, `filed_issues`, `watch_items`.

- **Running, not stopped** → `ScheduleWakeup(delaySeconds=1800, prompt="/monitor_run <DIR>", reason="next AgentShore monitor check-in")`.
- **Exited or auto-stopped** → print Run Summary, `rm -f STATE_FILE`, do not reschedule.

## Run Summary

```
AgentShore Monitor Summary — <DIR>
──────────────────────────────────
Session:          <session_id>
Watched for:      <HH:MM:SS>   (<N> check-ins)
Ended by:         <clean exit | shutdown event | auto-stop (idle x2) | user>
Plays:            <play_completed> completed  (<ok_plays> ok / <fail_plays> fail)
Total cost:       $<from snapshot>
Errors seen:      <count + one-line each, transient vs bug>
Inefficiencies:   <bullet list>
Bugs filed:       <#NN title> ...  (or "none")
Last snapshot:    <final snapshot.py block>
```

If auto-stopped, state plainly that you stopped it (CLI) or surfaced it to the user
(desktop) and why.
