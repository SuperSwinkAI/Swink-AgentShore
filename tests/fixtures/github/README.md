# GitHub contract fixtures

These JSON files are sanitized captures from the public
`SuperSwinkAI/Swink-AgentShore` repository. `metadata.json` records the exact
read-only commands, capture timestamp, producing GitHub CLI version, and
sanitization policy. The fixtures preserve GitHub's real field names, value
types, nulls, enums, and nested response shapes.

Refresh them deliberately with:

```bash
uv run python scripts/refresh_github_contract_fixtures.py
```

Then run the offline parser contract and the opt-in live read-only check:

```bash
uv run pytest tests/test_github_contract_fixtures.py -p no:xdist
AGENTSHORE_GITHUB_CONTRACT=1 uv run pytest tests/test_github_live_contract.py -p no:xdist
```

The live check issues only `gh api` and `gh pr list` reads. It never mutates the
repository.
