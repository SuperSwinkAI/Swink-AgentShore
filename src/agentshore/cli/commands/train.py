"""``agentshore train`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click


@click.command()
@click.option(
    "--sessions",
    type=click.Path(exists=True),
    default=None,
    help="Path to agentshore.db (default: .agentshore/agentshore.db in current dir)",
)
@click.option("--epochs", type=int, default=4, show_default=True, help="PPO training epochs")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Output checkpoint path (default: ~/.config/swink/agentshore/weights/policy.pt)",
)
@click.option(
    "--source-policy",
    type=click.Path(exists=True),
    default=None,
    help="Warm-start from an existing checkpoint",
)
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory",
)
def train(
    sessions: str | None,
    epochs: int,
    output: str | None,
    source_policy: str | None,
    project: str,
) -> None:
    """Run offline PPO training on accumulated session data."""
    import asyncio

    project_path = Path(project).resolve()

    # Locate agentshore.yaml for config
    config_path = project_path / "agentshore.yaml"
    try:
        from agentshore.config import load_config
        from agentshore.errors import ConfigError

        cfg = load_config(config_path if config_path.exists() else None)
    except (ConfigError, OSError, ValueError) as exc:
        from agentshore.config import load_config

        cfg = load_config(None)
        click.echo(f"Warning: config load failed ({exc}), using defaults.", err=True)

    # Locate database
    db_path = (
        Path(sessions) if sessions is not None else project_path / ".agentshore" / "agentshore.db"
    )
    if not db_path.exists():
        click.echo(f"Error: database not found at {db_path}", err=True)
        raise SystemExit(1)

    # Output path
    if output is not None:
        out_path = Path(output)
    elif cfg.rl.policy_path:
        out_path = Path(cfg.rl.policy_path)
    else:
        from agentshore.paths import GLOBAL_WEIGHTS_DIR
        out_path = GLOBAL_WEIGHTS_DIR / "policy.pt"

    async def _train() -> None:
        from agentshore.data.store import DataStore
        from agentshore.rl.action_space import ACTION_SPACE_VERSION
        from agentshore.rl.cold_start import apply_cold_start_bias
        from agentshore.rl.policy import ActorCritic
        from agentshore.rl.replay import ReplayLoader
        from agentshore.rl.training import PPOUpdater

        store = DataStore(db_path)
        try:
            await store.initialize()

            # Load or cold-start policy
            if source_policy is not None:
                policy = ActorCritic.load(Path(source_policy))
                click.echo(f"Loaded policy from {source_policy}")
            else:
                policy = ActorCritic()
                apply_cold_start_bias(policy)
                click.echo("Initialized policy with cold-start bias")

            updater = PPOUpdater(
                policy,
                lr=cfg.rl.learning_rate,
                clip_eps=cfg.rl.ppo.clip_epsilon,
                value_coef=cfg.rl.ppo.value_loss_coef,
                entropy_coef=cfg.rl.entropy_coef,
                ppo_epochs=epochs,
                mini_batch_size=cfg.rl.ppo.mini_batch_size,
                max_grad_norm=cfg.rl.ppo.max_grad_norm,
            )

            loader = ReplayLoader(store, action_space_version=ACTION_SPACE_VERSION)

            total_updates = 0
            session_count = 0
            async for session_id in loader.iter_compatible_sessions():
                buf = await loader.load_session(session_id)
                if len(buf) == 0:
                    continue
                buf.compute_advantages(
                    0.0,
                    gamma=cfg.rl.gamma,
                    gae_lambda=cfg.rl.ppo.gae_lambda,
                )
                stats = updater.update(buf)
                session_count += 1
                total_updates += 1
                status = "ROLLED_BACK" if stats.rolled_back else "ok"
                click.echo(
                    f"  session={session_id[:8]} steps={len(buf)} "
                    f"policy_loss={stats.policy_loss:.4f} "
                    f"value_loss={stats.value_loss:.4f} "
                    f"entropy={stats.entropy:.4f} [{status}]"
                )

            if session_count == 0:
                click.echo("No compatible sessions found. Nothing to train.")
                return

            out_path.parent.mkdir(parents=True, exist_ok=True)
            policy.save(out_path)
            click.echo(f"\nSaved checkpoint to {out_path}")
            click.echo(f"Trained on {session_count} session(s), {total_updates} buffer(s).")
        finally:
            await store.close()

    asyncio.run(_train())
