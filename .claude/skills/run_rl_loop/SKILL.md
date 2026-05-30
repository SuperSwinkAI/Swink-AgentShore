---
name: run_rl_loop
description: >-
  Start AgentShore in agent mode inside /Users/user/example-repo, run
  for up to 2 hours, monitor logs for errors, auto-remediate failures in the
  AgentShore source on main, re-test, reinstall globally, and restart. Loops
  until 2 hours elapse or the user interrupts.
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
---

# run_rl_loop

Run AgentShore's RL loop against the example-repo project, watching for crashes or
errors, remediating them in source, and restarting — all within a 2-hour wall
clock budget.

## Constants

```
SCRATCH         = /Users/user/example-repo          # the project to run the loop against
AGENTSHORE_SRC  = $(git rev-parse --show-toplevel)  # this AgentShore checkout
DEADLINE        = 7200   # seconds (2 hours)
LOG_FILE        = /tmp/agentshore_rl_loop.log
STATUS_FIRST_AT = 180    # seconds — first snapshot fires this fast (3 min)
STATUS_INTERVAL = 600    # seconds — subsequent snapshots cadence (10 min)
SNAPSHOT_SCRIPT = $AGENTSHORE_SRC/.claude/skills/run_rl_loop/snapshot.py
```

The snapshot reads the agentshore NDJSON log; ``$LOG_FILE`` is what we capture
from ``agentshore start``. No DB path is needed.

## Outer loop

Repeat the following cycle until `elapsed >= DEADLINE` or the user interrupts.
Track wall-clock time with `date +%s` at the start of each iteration.

---

## Cycle Step 1 — Wipe artifacts and health-check before start

AgentShore is pre-BETA; there is no schema migration story yet. State left over from a
prior session can silently break persistence in ways that don't surface as errors
(observed 2026-05-05: an Apr-28 DB caused 17 minutes of writes to vanish into a
0-byte WAL with no exception logged). Always start each cycle from a clean slate.

1. **Stop any running agentshore** so we don't fight an open DB handle:
   - `pgrep -f "agentshore start" >/dev/null && agentshore stop --project $SCRATCH`
   - Poll `kill -0 $AGENTSHORE_PID` for up to 30 seconds (1s sleep each iteration). If still alive at 30s, log `"shutdown_hang_sigkill elapsed=30s"` to `$LOG_FILE` then `pkill -f "agentshore start"`. Record this as an incident — it means the shutdown hang (Bug 2) fired and should be investigated.
2. **Wipe the project's agentshore state** — both the runtime dir and the auto-generated config:
   ```bash
   rm -rf $SCRATCH/.agentshore
   rm -f $SCRATCH/agentshore.yaml
   ```
   `agentshore start` will regenerate `agentshore.yaml` with current defaults on the
   next launch. Skipping this step is the bug we hit on 2026-05-05 — do not skip
   it to "preserve learnings" or training data; archive them out-of-band if you
   need them.
3. **Clear stale sockets** (separate from the project state):
   - `rm -rf ~/.agentshore/sessions/*` — any leftover socket dirs.
4. **Verify the binary is on PATH**: `which agentshore` — bail if missing.
5. **Confirm example-repo exists and is a git repo**: `test -d $SCRATCH/.git` — bail if not.

---

## Cycle Step 2 — Start AgentShore in agent mode

Launch agentshore in the background, capturing all output to `$LOG_FILE`:

```bash
cd $SCRATCH
agentshore start --mode agent --project $SCRATCH > $LOG_FILE 2>&1 &
AGENTSHORE_PID=$!
echo "AgentShore PID: $AGENTSHORE_PID"
```

Wait up to 10 seconds for the socket to appear, polling every second:

```bash
for i in $(seq 1 10); do
  ls ~/.agentshore/sessions/*/socket.sock 2>/dev/null && break
  sleep 1
done
```

If the socket never appears, treat this as a crash and jump to **Cycle Step 4**.

---

## Cycle Step 3 — Monitor the running session

Initialise `LAST_STATUS_TIME=0` (sentinel — no snapshot has fired yet). Poll every 2 minutes. On each poll:

1. **Check liveness**: `kill -0 $AGENTSHORE_PID 2>/dev/null` — if the process is gone, jump to **Cycle Step 4**.
2. **Check deadline**: if `$(date +%s) - START_TIME >= DEADLINE`, run `agentshore stop --project $SCRATCH` and exit the outer loop cleanly.
3. **Scan the log tail** (last 100 lines of `$LOG_FILE`) for error signals using these patterns only (substring matches on `Error:` / `Exception:` / `CRITICAL` over-match agent `output_tail` JSON and produce false positives — do not use them):
   - `"level":"error"` or `"level":"critical"` — AgentShore-emitted structured errors
   - `^Traceback` — Python traceback start (line-anchored to avoid matching agent output)
   - `task exception was never retrieved` — asyncio unhandled exception
4. If error signals are found **and** the process is still running, call `agentshore stop --project $SCRATCH`, wait 3 seconds, then fall through to **Cycle Step 4**.
5. **Status check** — fire a snapshot when either condition is true:
   - First snapshot: `LAST_STATUS_TIME == 0` AND `$(date +%s) - START_TIME >= STATUS_FIRST_AT` (≈ 3 min after launch).
   - Steady-state: `LAST_STATUS_TIME > 0` AND `$(date +%s) - LAST_STATUS_TIME >= STATUS_INTERVAL` (every 10 min thereafter).
   On a snapshot, run the **Status Snapshot** below, then set `LAST_STATUS_TIME=$(date +%s)`.
6. If no errors and process is alive, continue polling.

### Status Snapshot

Run `snapshot.py` as the report; it parses the NDJSON log and prints a
compact summary. The script is the durable surface — when the structured
log shape changes, update `snapshot.py` and the cadence in this skill
stays correct without edits.

```bash
python3 $SNAPSHOT_SCRIPT $LOG_FILE
```

Why log-based rather than reading the SQLite DB: SQLite WAL mode does
not allow safe concurrent reads from a separate process while agentshore
holds the SHM open. ``mode=ro`` fails with ``database disk image is
malformed`` even on a healthy file. The NDJSON log is the authoritative
live readout.

Expected output shape:

```
[STATUS 11:10:51]
  Session 15650501  plays=14 ok=12 fail=2  cost=$0.0312
  Agent claude_code   5e20e090  status=idle  calls=9  cost=$0.0198
  Agent codex         efc338cb  status=busy  calls=4  cost=$0.0114
  Last 5 plays:
    code_review              ok=1
    issue_pickup             ok=0  err=All agents blocked for 'issue_pickup' — anti-confirmation, exclude…
    consolidate_learnings    ok=1
  Top events:
    play_completed               14
    agent_status_changed         11
    cli_dispatch_done            10
    cli_dispatch_start           10
    loop_detected                3
```

Failed plays show a truncated `error`, and the **Top events** block
surfaces high-frequency signals like `loop_detected` and
`learnings_load_failed` so behavioural problems are visible without
hand-grepping the log. If the log file doesn't exist yet (very early
in boot), the script prints `[STATUS] no log yet` and exits 0; treat
that as a non-error signal that the snapshot fired before any output
was flushed.

---

## Cycle Step 4 — Diagnose the failure

Read the full `$LOG_FILE` and identify:
- The first traceback or critical error
- The source file and line number (extract from the traceback)
- Whether it is a known transient (network timeout, subprocess killed) or a code bug

**Transient signals** (do not remediate — just restart):
- `httpx.ConnectTimeout`, `httpx.ReadTimeout`
- `asyncio.TimeoutError` without a traceback pointing into `src/agentshore/`
- `KeyboardInterrupt`
- Clean exit (process exited 0 — session completed normally)

If it is a transient or clean exit, skip to **Cycle Step 6** (reinstall not needed — just restart or exit).

**Code bug signals** (remediate):
- Any traceback with a frame in `src/agentshore/`
- `AttributeError`, `TypeError`, `KeyError`, `AssertionError`, `RuntimeError` from agentshore source
- `aiosqlite` errors that trace back to agentshore code
- `mypy` or `ruff` failures (if detected in logs)

---

## Cycle Step 5 — Remediate the bug in source

Work in `$AGENTSHORE_SRC` on the `main` branch:

1. **Confirm branch**: `git -C $AGENTSHORE_SRC branch --show-current` — if not `main`, warn but continue.
2. **Read the failing file** identified in the traceback.
3. **Apply the minimal fix** — do not refactor beyond the failing code path.
4. **Run ruff**: `cd $AGENTSHORE_SRC && uv run ruff check src/ tests/ && uv run ruff format src/ tests/`
   - If ruff fails, fix the lint errors before proceeding.
5. **Run mypy**: `cd $AGENTSHORE_SRC && uv run mypy src/`
   - If mypy fails, fix the type errors before proceeding.
6. **Run the targeted test** for the fixed module (e.g., `uv run pytest tests/test_<module>.py -q`).
   If the targeted test passes, run the full suite: `uv run pytest tests/ -q --tb=short`
   - If tests fail, fix them before proceeding.
7. **Commit the fix**:
   ```bash
   git -C $AGENTSHORE_SRC add -p   # stage only the fix
   git -C $AGENTSHORE_SRC commit -m "fix: <one-line description of what was fixed>"
   ```

---

## Cycle Step 6 — Reinstall agentshore globally

After any remediation (or if a reinstall is warranted after a transient):

```bash
cd $AGENTSHORE_SRC
uv tool install --reinstall --editable .
```

Verify the new binary is live: `agentshore --version`

---

## Cycle Step 7 — Loop or exit

- If `$(date +%s) - START_TIME < DEADLINE`: go back to **Cycle Step 1**.
- If deadline reached: print a summary and exit.
- If the same error recurs 3 times in a row without a successful fix: stop looping, print the repeated error, and ask the user for guidance.

---

## Summary output

After the loop ends (deadline, user interrupt, or repeated failure), print:

```
RL Loop Summary
───────────────
Wall time:        <HH:MM:SS>
Cycles completed: <N>
Clean exits:      <N>
Errors found:     <N>
  - Transient:    <N>
  - Code bugs:    <N> (fixed: <N>, unresolved: <N>)
Last log tail:    (last 20 lines of $LOG_FILE)
```
