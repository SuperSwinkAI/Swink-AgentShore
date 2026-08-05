"""Refresh sanitized GitHub REST and ``gh pr`` contract fixtures.

All commands are read-only. The selected issue and pull request are stable,
public artifacts in the AgentShore repository whose response shapes exercise
the adapter's issue/PR distinction and linked-issue parsing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from agentshore.github.adapter import _PR_JSON_FIELDS

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "github"


def _run_gh_json(args: list[str]) -> Any:
    completed = subprocess.run(
        ["gh", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _sanitize(value: Any, source_repo: str) -> Any:
    if isinstance(value, list):
        return [_sanitize(item, source_repo) for item in value]
    if isinstance(value, dict):
        clean = {key: _sanitize(item, source_repo) for key, item in value.items()}
        login = clean.get("login")
        if isinstance(login, str):
            if "dependabot" in login or login == "fixture-bot":
                clean["login"] = "fixture-bot"
            elif login in {source_repo.split("/", 1)[0], "fixture-org"}:
                clean["login"] = "fixture-org"
            else:
                clean["login"] = "fixture-user"
            if "name" in clean:
                clean["name"] = "Fixture User"
        if "email" in clean:
            clean["email"] = None
        return clean
    if isinstance(value, str):
        replacements = {
            f"https://github.com/{source_repo}": "https://github.com/example/contract-fixture",
            f"https://api.github.com/repos/{source_repo}": (
                "https://api.github.com/repos/example/contract-fixture"
            ),
            "jwesleye": "fixture-user",
            "app/dependabot": "fixture-bot",
        }
        for original, replacement in replacements.items():
            value = value.replace(original, replacement)
        if value.startswith("https://avatars.githubusercontent.com/"):
            return "https://avatars.githubusercontent.com/u/1?v=4"
    return value


def _write_json(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="SuperSwinkAI/Swink-AgentShore")
    parser.add_argument("--issue", type=int, default=416)
    parser.add_argument("--pr", type=int, default=417)
    args = parser.parse_args()

    issue_command = ["api", f"repos/{args.repo}/issues/{args.issue}"]
    pr_issue_command = ["api", f"repos/{args.repo}/issues/{args.pr}"]
    pr_list_command = [
        "pr",
        "list",
        "--repo",
        args.repo,
        "--state",
        "all",
        "--json",
        _PR_JSON_FIELDS,
        "--limit",
        "1",
    ]
    pr_view_command = [
        "pr",
        "view",
        str(args.pr),
        "--repo",
        args.repo,
        "--json",
        _PR_JSON_FIELDS,
    ]

    issue = _sanitize(_run_gh_json(issue_command), args.repo)
    pull_request_as_issue = _sanitize(_run_gh_json(pr_issue_command), args.repo)
    pr_list = _sanitize(_run_gh_json(pr_list_command), args.repo)
    pr_view = _sanitize(_run_gh_json(pr_view_command), args.repo)

    issue["title"] = "Captured contract issue"
    issue["body"] = "Sanitized issue body."
    pull_request_as_issue["title"] = "Captured contract pull request"
    pull_request_as_issue["body"] = "Sanitized pull request body."
    pr_list[0]["title"] = "Captured dependency update"
    pr_list[0]["body"] = "Sanitized dependency update body."
    pr_view["title"] = "Captured linked-issue pull request"
    pr_view["body"] = "Closes #415 and #416."

    _write_json("issues-rest.json", [issue, pull_request_as_issue])
    _write_json("pr-list.json", pr_list)
    _write_json("pr-view.json", pr_view)

    version = subprocess.run(
        ["gh", "--version"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    _write_json(
        "metadata.json",
        {
            "captured_at": dt.datetime.now(dt.UTC).isoformat(),
            "gh_version": version,
            "source_repository": args.repo,
            "fixtures": {
                "issues-rest.json": [issue_command, pr_issue_command],
                "pr-list.json": pr_list_command,
                "pr-view.json": pr_view_command,
            },
            "sanitization": (
                "Bodies, titles, repository URLs, user names, and logins are replaced; "
                "field names, types, enums, nulls, opaque IDs, and nested shapes are retained."
            ),
        },
    )


if __name__ == "__main__":
    main()
