#!/usr/bin/env python
"""Export the shipped warm-start seed policy (``bootstrap_policy.pt``).

A fresh install has no global canonical and no project-local checkpoint, so
without a bundled seed every first session cold-starts from random weights
(``_resolve_policy_path`` step 5 in ``agentshore.core.phases``). This script
snapshots a trained checkpoint into ``src/agentshore/data/bootstrap_policy.pt``
so step 4 resolves and new installs start warm.

Source resolution (no machine-specific paths in the repo):
  1. ``--source PATH``
  2. ``$AGENTSHORE_SEED_SOURCE``
  3. the platform-resolved global canonical
     (``GLOBAL_WEIGHTS_DIR / policy_v<POLICY_VERSION>.pt``)

The source is validated through ``ActorCritic.load`` (which hard-fails on any
action-space / policy / observation version drift) before it is written.

By default the **config head is stripped** (``num_configs == 0``). The config
head encodes which agent variant to spawn, sized to one install's roster; a
seed that carries it shape-mismatches ``act_config`` on an install with a
different roster. The trunk / actor / value heads — the portable "what to do
next" intelligence — are preserved. Pass ``--keep-config-head`` only when the
seed targets installs with an identical roster.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import torch

from agentshore.paths import GLOBAL_WEIGHTS_DIR
from agentshore.rl.action_space import POLICY_VERSION
from agentshore.rl.policy import ActorCritic

ENV_VAR = "AGENTSHORE_SEED_SOURCE"
DEST = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentshore"
    / "data"
    / "bootstrap_policy.pt"
)


def _default_source() -> Path:
    return GLOBAL_WEIGHTS_DIR / f"policy_v{POLICY_VERSION}.pt"


def _resolve_source(cli_source: Path | None) -> Path:
    if cli_source is not None:
        return cli_source.expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return _default_source()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"Trained checkpoint to snapshot. Defaults to ${ENV_VAR} or {_default_source()}.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEST,
        help="Output path for the bundled seed (default: the packaged location).",
    )
    parser.add_argument(
        "--keep-config-head",
        action="store_true",
        help="Preserve the install-specific config head (NOT portable across rosters).",
    )
    args = parser.parse_args(argv)

    source = _resolve_source(args.source)
    if not source.is_file():
        print(f"error: source checkpoint not found: {source}", file=sys.stderr)
        print(f"  set ${ENV_VAR}, pass --source, or train a canonical first.", file=sys.stderr)
        return 1

    # Version gate: raises IncompatibleCheckpointError on any version drift.
    model = ActorCritic.load(source)
    src_num_configs = model.num_configs

    if args.keep_config_head:
        out = model
    else:
        payload = torch.load(source, map_location="cpu", weights_only=True)
        out = ActorCritic(
            obs_dim=int(payload["obs_dim"]),
            num_actions=int(payload["num_actions"]),
            num_configs=0,
        )
        shared_sd = {
            k: v for k, v in payload["state_dict"].items() if not k.startswith("config_head.")
        }
        missing, unexpected = out.load_state_dict(shared_sd, strict=False)
        unexpected = [k for k in unexpected if not k.startswith("config_head.")]
        if unexpected:
            print(f"error: unexpected weights in source: {unexpected}", file=sys.stderr)
            return 1
        missing = [k for k in missing if not k.startswith("config_head.")]
        if missing:
            print(f"error: source is missing shared weights: {missing}", file=sys.stderr)
            return 1

    args.dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(args.dest)

    stripped = "" if args.keep_config_head else "  (config head stripped for portability)"
    print("exported bootstrap seed:")
    print(f"  source:         {source}")
    print(f"  dest:           {args.dest}")
    print(f"  policy_version: {POLICY_VERSION}")
    print(f"  num_configs:    {src_num_configs} -> {out.num_configs}{stripped}")
    print(f"  size:           {args.dest.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
