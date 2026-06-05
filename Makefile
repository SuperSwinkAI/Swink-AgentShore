.PHONY: help export-bootstrap-policy

help:  ## Show available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  %-26s %s\n", $$1, $$2}'

export-bootstrap-policy:  ## Snapshot a trained canonical into the shipped warm-start seed (override source via AGENTSHORE_SEED_SOURCE)
	uv run python scripts/export_bootstrap_policy.py
