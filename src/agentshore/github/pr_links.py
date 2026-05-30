"""Pull-request to issue-link inference utilities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_BARE_ISSUE_RE = re.compile(r"#(?P<number>\d+)\b")
_CLOSING_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|closing|fix(?:e[sd])?|resolve[sd]?)\b",
    re.IGNORECASE,
)
_AGENTSHORE_BRANCH_RE = re.compile(r"(?:^|/)agentshore/(?P<number>\d+)(?=$|[-_/])")
_ISSUE_URL_RE = re.compile(r"/issues/(?P<number>\d+)(?:$|[/?#])")


@dataclass(frozen=True, slots=True)
class PullRequestIssueLinks:
    """Normalized issue links inferred from one pull request."""

    issue_numbers: tuple[int, ...]
    provenance: dict[int, tuple[str, ...]]

    @property
    def primary_issue_number(self) -> int | None:
        return self.issue_numbers[0] if self.issue_numbers else None


def canonical_issue_numbers(values: Iterable[object]) -> tuple[int, ...]:
    """Return stable, positive issue numbers from mixed external values."""

    numbers: list[int] = []
    seen: set[int] = set()
    for raw in values:
        number = _coerce_issue_number(raw)
        if number is None or number in seen:
            continue
        numbers.append(number)
        seen.add(number)
    return tuple(numbers)


def issue_numbers_for_pr(pr: object) -> tuple[int, ...]:
    """Return all issue numbers linked to a PR-like object."""

    linked = _link_values(getattr(pr, "linked_issue_numbers", ()))
    return canonical_issue_numbers((getattr(pr, "issue_number", None), *linked))


def infer_pr_issue_links(
    *,
    issue_number: object = None,
    linked_issue_numbers: Iterable[object] = (),
    closing_issue_references: object = None,
    body: object = None,
    branch: object = None,
) -> PullRequestIssueLinks:
    """Infer issue links from fields that GitHub or external PR sources provide."""

    provenance: dict[int, list[str]] = {}

    def add(raw: object, source: str) -> None:
        number = _coerce_issue_number(raw)
        if number is None:
            return
        provenance.setdefault(number, [])
        if source not in provenance[number]:
            provenance[number].append(source)

    add(issue_number, "explicit_field")
    for raw in _link_values(linked_issue_numbers):
        add(raw, "linked_issue_numbers")
    for raw in _closing_reference_numbers(closing_issue_references):
        add(raw, "github_closing_reference")
    for raw in _body_closing_numbers(body):
        add(raw, "body_closes_keyword")
    for raw in _agentshore_branch_numbers(branch):
        add(raw, "agentshore_branch_prefix")

    ordered = tuple(provenance)
    return PullRequestIssueLinks(
        issue_numbers=ordered,
        provenance={number: tuple(sources) for number, sources in provenance.items()},
    )


def _coerce_issue_number(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("#"):
            text = text[1:]
        if text.isdecimal():
            number = int(text)
            return number if number > 0 else None
    return None


def _link_values(raw: object) -> tuple[object, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    try:
        return tuple(raw)  # type: ignore[arg-type]
    except TypeError:
        return (raw,)


def _closing_reference_numbers(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, list):
        return ()
    numbers: list[object] = []
    for item in raw:
        if isinstance(item, dict):
            numbers.append(item.get("number"))
            numbers.append(item.get("issue_number"))
            issue = item.get("issue")
            if isinstance(issue, dict):
                numbers.append(issue.get("number"))
            url = item.get("url")
            if isinstance(url, str):
                match = _ISSUE_URL_RE.search(url)
                if match:
                    numbers.append(match.group("number"))
        else:
            numbers.append(item)
    return canonical_issue_numbers(numbers)


def _body_closing_numbers(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, str):
        return ()
    numbers: list[str] = []
    for line in raw.splitlines():
        if _CLOSING_KEYWORD_RE.search(line):
            numbers.extend(match.group("number") for match in _BARE_ISSUE_RE.finditer(line))
    return canonical_issue_numbers(numbers)


def _agentshore_branch_numbers(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, str):
        return ()
    return canonical_issue_numbers(
        match.group("number") for match in _AGENTSHORE_BRANCH_RE.finditer(raw)
    )
