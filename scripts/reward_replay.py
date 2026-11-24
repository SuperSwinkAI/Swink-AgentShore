#!/usr/bin/env python
"""Offline reward replay: score a candidate reward against recorded plays.

Read-only. Opens a project's ``.agentshore/agentshore.db`` immutably, takes the
reward each play actually received (``rl_experience.reward``), swaps the
concurrency term for a candidate one, adds an optional attributed merge credit,
and reports per session:

* each term's share of total |reward| (current vs candidate)
* mean reward by play type
* correlation between windowed reward and merges over time
* stall check: do progress plays still out-earn cheap plays?

The live reward is untouched. Busy/live agent counts are not persisted, so they
are decoded from the stored ``next_state`` observation (tier-fleet block, with
the per-config block as fallback when a tier saturates). ``--validate`` checks
that decode against a session's ``fleet_concurrency.ndjson``.

Usage::

    uv run python -m scripts.reward_replay <project>/.agentshore/agentshore.db
    uv run python -m scripts.reward_replay DB --session 127dcebf --conc-scale 1.0
    uv run python -m scripts.reward_replay DB --validate path/to/fleet_concurrency.ndjson
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from agentshore.config import RewardConfig
from agentshore.rl.observation import (
    _CONFIG_FEATURES_PER_SLOT,
    _FLEET_FEATURES_PER_TIER,
    _MAX_AGENTS,
    _MAX_TOTAL_AGENTS,
    _NUM_TIERS,
    _S_AGENT_START,
    _S_CONFIG_BLOCK_START,
    MAX_CONFIG_INDEX_SIZE,
    OBSERVATION_DIM,
)
from agentshore.rl.reward import _DISPATCH_PLAYS
from agentshore.state import PlayType

_PROGRESS = {"issue_pickup", "code_review", "merge_pr", "unblock_pr", "run_qa"}
_CLIP = 10.0
_PR_URL = re.compile(r"/pull/(\d+)")


@dataclass
class Play:
    play_id: int
    session_id: str
    play_type: str
    success: bool
    cost: float
    reward: float  # as recorded (clipped)
    busy: int  # reward-time busy count (next_state busy + the completing agent)
    live: int
    saturated: bool
    prs: set[int] = field(default_factory=set)
    issues: set[int] = field(default_factory=set)
    merged_pr: int | None = None
    merged_issues: set[int] = field(default_factory=set)
    credit: float = 0.0


def decode_counts(blob: bytes) -> tuple[int, int, bool]:
    """Return (busy, live, saturated) for the agents in an encoded observation."""
    obs = np.frombuffer(blob, dtype=np.float32)
    tier = obs[_S_AGENT_START : _S_AGENT_START + _NUM_TIERS * _FLEET_FEATURES_PER_TIER]
    tier = tier.reshape(_NUM_TIERS, _FLEET_FEATURES_PER_TIER)
    idle_t, busy_t = tier[:, 0], tier[:, 1]
    busy = int(round(float(busy_t.sum()) * _MAX_TOTAL_AGENTS))
    idle = int(round(float(idle_t.sum()) * _MAX_TOTAL_AGENTS))
    tier_sat = bool((idle_t >= 1.0).any() or (busy_t >= 1.0).any())
    if not tier_sat:
        return busy, busy + idle, False
    cfg = obs[
        _S_CONFIG_BLOCK_START : _S_CONFIG_BLOCK_START
        + MAX_CONFIG_INDEX_SIZE * _CONFIG_FEATURES_PER_SLOT
    ].reshape(MAX_CONFIG_INDEX_SIZE, _CONFIG_FEATURES_PER_SLOT)
    idle_c, busy_c = cfg[:, 0], cfg[:, 1]
    c_busy = int(round(float(busy_c.sum()) * _MAX_AGENTS))
    c_idle = int(round(float(idle_c.sum()) * _MAX_AGENTS))
    cfg_sat = bool((idle_c >= 1.0).any() or (busy_c >= 1.0).any())
    # Each source is a lower bound once saturated; take the larger.
    return max(busy, c_busy), max(busy + idle, c_busy + c_idle), cfg_sat


def current_concurrency(busy: int, live: int, cfg: RewardConfig) -> float:
    """Mirror of the concurrency term in ``rl.reward.compute_reward``."""
    util = busy / live if live > 0 else 0.0
    mult = 4.0 if util >= 1.0 else 2.0 if util >= 0.75 else 1.0 if util >= 0.5 else 0.0
    return cfg.concurrent_agent_bonus * max(0, busy - 1) * mult


def candidate_concurrency(busy: int, live: int, scale: float) -> float:
    """Fleet-size-invariant: pay utilisation, not head count."""
    return scale * (busy / live) if live > 0 else 0.0


def load(db: str, session: str | None) -> list[Play]:
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    q = """
        SELECT p.play_id, p.session_id, p.play_type, p.success, p.dollar_cost,
               p.artifacts, r.reward, r.next_state
        FROM rl_experience r JOIN plays p USING (play_id)
        WHERE (? IS NULL OR p.session_id LIKE ? || '%')
        ORDER BY p.session_id, r.experience_id
    """
    plays: list[Play] = []
    for pid, sid, ptype, ok, cost, arts, reward, nxt in con.execute(q, (session, session)):
        if len(nxt) != OBSERVATION_DIM * 4:
            continue
        busy, live, sat = decode_counts(nxt)
        # Reward counts the just-finished agent as busy (core.helpers._build_reward_signals).
        busy = min(busy + 1, live) if live else busy
        play = Play(pid, sid, ptype, bool(ok), cost or 0.0, reward, busy, live, sat)
        _parse_artifacts(play, arts)
        plays.append(play)
    con.close()
    return plays


def _parse_artifacts(play: Play, raw: str | None) -> None:
    try:
        arts = json.loads(raw) if raw else []
    except ValueError:
        return
    for a in arts if isinstance(arts, list) else []:
        if not isinstance(a, dict):
            continue
        kind = a.get("type")
        pr = a.get("pr", a.get("number"))
        if isinstance(pr, int):
            play.prs.add(pr)
        if isinstance(a.get("url"), str) and (m := _PR_URL.search(a["url"])):
            play.prs.add(int(m.group(1)))
        if isinstance(a.get("issue"), int):
            play.issues.add(a["issue"])
        if kind == "merge" and isinstance(pr, int):
            play.merged_pr = pr
        if kind == "pr_merged_issue_numbers":
            play.merged_issues.update(i for i in a.get("issues_closed", []) if isinstance(i, int))


def attribute(plays: list[Play], credit: float, plan_credit: float) -> None:
    """Credit each successful merge to the earlier plays that touched its PR or issue."""
    for i, m in enumerate(plays):
        if not (m.success and m.play_type == "merge_pr" and m.merged_pr is not None):
            continue
        for p in plays[:i]:
            if p.session_id != m.session_id or not p.success:
                continue
            if (
                p.play_type in {"issue_pickup", "code_review", "unblock_pr"}
                and m.merged_pr in p.prs
            ):
                p.credit += credit
            elif p.play_type == "write_implementation_plan" and p.issues & m.merged_issues:
                p.credit += plan_credit


def candidate_reward(p: Play, cfg: RewardConfig, scale: float) -> float:
    rec = p.reward
    if p.success and PlayType(p.play_type) in _DISPATCH_PLAYS:
        rec = (
            rec
            - current_concurrency(p.busy, p.live, cfg)
            + candidate_concurrency(p.busy, p.live, scale)
        )
    return max(-_CLIP, min(_CLIP, rec + p.credit))


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    return statistics.correlation(xs, ys)


def report(plays: list[Play], cfg: RewardConfig, scale: float, window: int) -> None:
    by_session: dict[str, list[Play]] = defaultdict(list)
    for p in plays:
        by_session[p.session_id].append(p)
    for sid, ps in by_session.items():
        cur = [p.reward for p in ps]
        cand = [candidate_reward(p, cfg, scale) for p in ps]
        conc = [
            current_concurrency(p.busy, p.live, cfg)
            if p.success and PlayType(p.play_type) in _DISPATCH_PLAYS
            else 0.0
            for p in ps
        ]
        cconc = [
            candidate_concurrency(p.busy, p.live, scale)
            if p.success and PlayType(p.play_type) in _DISPATCH_PLAYS
            else 0.0
            for p in ps
        ]
        merges = sum(1 for p in ps if p.play_type == "merge_pr" and p.success)
        print(
            f"\n=== session {sid[:8]}  plays={len(ps)}  merges={merges}  "
            f"cost=${sum(p.cost for p in ps):.0f}  max_live={max(p.live for p in ps)}"
        )
        sat = sum(p.saturated for p in ps)
        clipped = sum(abs(r) >= _CLIP for r in cur)
        print(f"decode saturated rows: {sat}   recorded rewards at clip: {clipped}")

        tot_cur = sum(abs(r) for r in cur) or 1.0
        tot_cand = sum(abs(r) for r in cand) or 1.0
        credit = sum(p.credit for p in ps)
        print("term shares of total |reward|      current   candidate")
        print(
            f"  concurrency                        {sum(conc) / tot_cur:7.0%}   "
            f"{sum(cconc) / tot_cand:7.0%}"
        )
        print(f"  attributed merge credit            {0:7.0%}   {credit / tot_cand:7.0%}")
        print(
            f"  everything else                    {1 - sum(conc) / tot_cur:7.0%}   "
            f"{1 - (sum(cconc) + credit) / tot_cand:7.0%}"
        )

        print("mean reward by play type        n    $/play   current  candidate")
        groups: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(ps):
            groups[p.play_type].append(i)
        for ptype, idx in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(
                f"  {ptype:26s} {len(idx):5d}  {sum(ps[i].cost for i in idx) / len(idx):7.2f}"
                f"  {statistics.fmean(cur[i] for i in idx):8.2f}"
                f"  {statistics.fmean(cand[i] for i in idx):9.2f}"
            )

        w_cur, w_cand, w_merge = [], [], []
        for s in range(0, len(ps), window):
            chunk = range(s, min(s + window, len(ps)))
            w_cur.append(sum(cur[i] for i in chunk))
            w_cand.append(sum(cand[i] for i in chunk))
            w_merge.append(
                float(sum(1 for i in chunk if ps[i].play_type == "merge_pr" and ps[i].success))
            )
        rc, rk = _corr(w_cur, w_merge), _corr(w_cand, w_merge)
        fmt = lambda r: "n/a" if r is None else f"{r:+.2f}"  # noqa: E731
        print(
            f"corr(windowed reward, merges), window={window}: "
            f"current {fmt(rc)}  candidate {fmt(rk)}"
        )

        prog = [i for i, p in enumerate(ps) if p.play_type in _PROGRESS]
        cheap = [i for i, p in enumerate(ps) if p.play_type not in _PROGRESS]
        if prog and cheap:
            for name, rs in (("current", cur), ("candidate", cand)):
                mp = statistics.fmean(rs[i] for i in prog)
                mc = statistics.fmean(rs[i] for i in cheap)
                verdict = "ok" if mp > mc else "STALL RISK: cheap plays pay more"
                print(f"stall check {name:9s}: progress {mp:+.2f}  other {mc:+.2f}  -> {verdict}")


def validate(plays: list[Play], ndjson: str) -> None:
    """Compare decoded counts with the fleet_concurrency.ndjson ground truth."""
    truth = {}
    with open(ndjson, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            truth[rec["play_id"]] = (rec["busy_total"], rec["live_total"])
    by_id = {p.play_id: p for p in plays}
    n = busy_ok = live_ok = 0
    for pid, (busy, live) in truth.items():
        p = by_id.get(pid)
        if p is None:
            continue
        n += 1
        live_ok += p.live == live
        busy_ok += p.busy == min(busy + 1, live)
    print(
        f"validate: {n} plays matched; live exact {live_ok}/{n}; "
        f"busy (+completing agent) exact {busy_ok}/{n}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline reward replay (read-only).")
    ap.add_argument("db")
    ap.add_argument("--session", help="session id prefix (default: all sessions)")
    ap.add_argument(
        "--conc-scale",
        type=float,
        default=1.0,
        help="candidate concurrency bonus at 100%% utilisation",
    )
    ap.add_argument(
        "--merge-credit",
        type=float,
        default=1.0,
        help="credit per merged PR to each pickup/review/unblock that touched it",
    )
    ap.add_argument(
        "--plan-credit", type=float, default=0.5, help="credit to a plan whose issue a merge closed"
    )
    ap.add_argument("--window", type=int, default=50, help="plays per correlation window")
    ap.add_argument("--validate", metavar="NDJSON", help="check decode vs fleet_concurrency.ndjson")
    args = ap.parse_args()

    cfg = RewardConfig()
    plays = load(args.db, args.session)
    if args.validate:
        validate(plays, args.validate)
    attribute(plays, args.merge_credit, args.plan_credit)
    report(plays, cfg, args.conc_scale, args.window)


if __name__ == "__main__":
    main()
