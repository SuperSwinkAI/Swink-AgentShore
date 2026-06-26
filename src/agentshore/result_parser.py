"""Extract the JSON result block from coding-agent output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from agentshore.state import JsonArtifact, JsonIssueRef, JsonObject, SkillResult

# Upper bound on parsed ``learnings_compacted`` entries. Set well above the
# default ``LearningsConfig.max_entries`` (200) so a legitimate full-store
# re-distillation is never truncated at parse time — the harvester's
# ``top_k(max_entries)`` is the real bound — while still capping a pathological
# array. result_parser is config-agnostic, hence a constant rather than the cfg.
_MAX_COMPACTED_ENTRIES = 500

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str, start: int) -> str | None:
    """Extract a complete JSON object starting at *start* in *text*.

    Handles nested braces and quoted strings so that the extraction is
    reliable even when the JSON contains nested objects or arrays.
    """
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    in_string = False
    escape_next = False
    end = start

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == "\\":
            if in_string:
                escape_next = True
            continue

        if ch == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                return text[start : end + 1]

    return None


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) that wrap JSON blocks.

    Agents frequently wrap their JSON output in markdown fences.  This helper
    normalises the text so that the JSON extractor can find the object.
    """
    lines: list[str] = []
    inside_fence = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside_fence = not inside_fence
            continue
        lines.append(line)

    return "\n".join(lines)


def _json_object(value: object) -> JsonObject | None:
    """Return *value* as a string-keyed JSON object when possible."""
    if not isinstance(value, dict):
        return None

    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _json_object_list(data: JsonObject, key: str) -> list[JsonObject]:
    """Return *data[key]* coerced to a list of string-keyed JSON objects.

    Non-list values yield an empty list; non-object items are dropped.
    """
    raw = data.get(key, [])
    if not isinstance(raw, list):
        return []
    objects: list[JsonObject] = []
    for item in raw:
        obj = _json_object(item)
        if obj is not None:
            objects.append(obj)
    return objects


def _candidate_result_objects(text: str) -> Iterator[JsonObject]:
    """Yield JSON objects in *text* that look like skill results."""
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        raw_json = _extract_json_object(text, idx)
        if raw_json is None:
            continue
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        obj = _json_object(data)
        if obj is None:
            continue
        if isinstance(obj.get("success"), bool):
            yield obj


def _has_balanced_json_object(text: str) -> bool:
    """Return True if *text* contains at least one parseable JSON object.

    Used only on the failure path (no valid candidate found), so any object this
    finds necessarily lacked a top-level boolean ``success`` — the signal that
    distinguishes a JSON near-miss (object present, envelope malformed) from a
    true no-JSON failure (prose / empty). See #229.
    """
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        raw_json = _extract_json_object(text, idx)
        if raw_json is None:
            continue
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if _json_object(data) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_skill_result(output: str) -> SkillResult:
    """Parse the JSON result block from *output* text.

    The search strategy is:

    1. Strip markdown code fences (agents often wrap JSON in ````` blocks).
    2. Find every balanced JSON object whose top-level ``success`` field is a
       boolean.  This accepts compact or pretty-printed JSON and does not
       require a particular key order.
    3. Use the last valid-looking object because agents may echo examples
       before producing their actual result at the end.
    4. Validate the expected fields and return a ``SkillResult``.

    If no valid JSON block is found, returns a ``SkillResult`` with
    ``success=False`` and an ``error`` describing the failure.
    """
    cleaned = _strip_code_fences(output)

    data: JsonObject | None = None
    for candidate in _candidate_result_objects(cleaned):
        data = candidate
    if data is None:
        # desktop-zzt: operators need to distinguish "agent crashed with no
        # output" from "agent ran fine but never emitted the JSON contract".
        # Include the output length and a short tail so the failure mode is
        # diagnosable from the play_completed log line alone.
        output_length = len(output)
        tail = output[-200:] if output else ""
        # Collapse whitespace runs so the tail fits on one log line.
        tail_one_line = " ".join(tail.split())
        # #229: a balanced JSON object that lacked a top-level boolean ``success``
        # is a near-miss, not a no-JSON failure. Distinguish it so operators (and
        # the resume-retry nudge) can name the actual defect. The prefix stays
        # "no valid result block found in agent output (...)" so the base.py retry
        # trigger keys on it unchanged.
        if _has_balanced_json_object(cleaned):
            detail = (
                f"agent produced {output_length} chars with a JSON object but no "
                f"top-level boolean 'success' field; tail: {tail_one_line!r}"
            )
            return SkillResult(
                success=False,
                error=f"no valid result block found in agent output ({detail})",
                missing_success_envelope=True,
            )
        if output_length == 0:
            detail = "agent produced no output"
        elif output_length < 100:
            detail = f"agent produced only {output_length} chars: {tail_one_line!r}"
        else:
            detail = (
                f"agent produced {output_length} chars but no JSON result "
                f"block; tail: {tail_one_line!r}"
            )
        return SkillResult(
            success=False,
            error=f"no valid result block found in agent output ({detail})",
        )

    # Validate required fields.
    if "success" not in data:
        return SkillResult(
            success=False,
            error="result block missing required field: success",
        )

    success = data["success"]
    if not isinstance(success, bool):
        return SkillResult(
            success=False,
            error=f"result block field 'success' is not a boolean: {success!r}",
        )

    # Extract optional fields with safe defaults.
    artifacts_raw = data.get("artifacts", [])
    if not isinstance(artifacts_raw, list):
        artifacts_raw = []
    artifacts: list[JsonArtifact] = []
    for item in artifacts_raw:
        obj = _json_object(item)
        if obj is not None:
            artifacts.append(obj)
        else:
            artifacts.append(str(item))

    issues_raw = data.get("issues_created", [])
    if not isinstance(issues_raw, list):
        issues_raw = []
    issues_created: list[JsonIssueRef] = []
    for item in issues_raw:
        obj = _json_object(item)
        if obj is not None:
            issues_created.append(obj)
            continue
        try:
            issues_created.append(int(item))
        except (TypeError, ValueError):
            continue

    error = data.get("error")
    if error is not None:
        error = str(error)

    # When success=False and the skill left error=null, synthesize the error
    # from verification_failures so play_completed always carries a diagnostic.
    if not success and error is None:
        vf = data.get("verification_failures")
        if isinstance(vf, list) and vf:
            parts: list[str] = []
            for item in vf:
                if isinstance(item, dict):
                    parts.append(str(item.get("detail") or item.get("type") or item))
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                error = "; ".join(parts)

    requested_mutations = _json_object_list(data, "requested_mutations")

    spec_compliance_raw = data.get("spec_compliance")
    spec_compliance: str | None = None
    if isinstance(spec_compliance_raw, str) and spec_compliance_raw:
        spec_compliance = spec_compliance_raw

    blocking_findings = _coerce_blocking(_json_object(data.get("findings_count")))

    prior_verdict_raw = data.get("prior_verdict")
    prior_verdict: str | None = None
    if isinstance(prior_verdict_raw, str) and prior_verdict_raw:
        prior_verdict = prior_verdict_raw

    prior_blocking_findings = _coerce_blocking(_json_object(data.get("prior_findings_count")))

    # ``issues_closed`` is a top-level list of issue numbers the skill closed
    # during the play (agentshore-merge-pr emits it from Closes/Fixes/Resolves
    # references on the merged PR). Coerce strings and floats to int; skip
    # anything else.
    issues_closed_raw = data.get("issues_closed", [])
    if not isinstance(issues_closed_raw, list):
        issues_closed_raw = []
    issues_closed: list[int] = []
    for item in issues_closed_raw:
        if isinstance(item, bool):
            continue  # bool is a subclass of int; reject explicit booleans
        if isinstance(item, int):
            issues_closed.append(item)
            continue
        try:
            issues_closed.append(int(item))
        except (TypeError, ValueError):
            continue

    issue_picked_up = _coerce_int(data.get("issue_picked_up"))
    branch_raw = data.get("branch")
    branch = str(branch_raw).strip() if branch_raw is not None and str(branch_raw).strip() else None
    tests_passed_raw = data.get("tests_passed")
    tests_passed = tests_passed_raw if isinstance(tests_passed_raw, bool) else None

    verification_evidence = _json_object_list(data, "verification_evidence")
    review_patterns = _json_object_list(data, "review_patterns")

    # Extract top-level ``learnings`` array emitted by agents. Each element must
    # be a dict with a non-empty ``pattern`` string; malformed entries are dropped.
    # Normalized to {"pattern": str, "confidence": float, "category": str} and
    # capped at 10 entries.
    from agentshore.core.learnings_harvester import (  # noqa: PLC0415
        DEFAULT_LEARNING_CONFIDENCE,
    )

    learnings_raw = data.get("learnings", [])
    learnings: list[JsonObject] = []
    if isinstance(learnings_raw, list):
        for raw_item in learnings_raw[:10]:
            if not isinstance(raw_item, dict):
                continue
            pattern = raw_item.get("pattern", "")
            if not isinstance(pattern, str) or not pattern:
                continue
            try:
                confidence = float(raw_item.get("confidence", DEFAULT_LEARNING_CONFIDENCE))
            except (TypeError, ValueError):
                confidence = DEFAULT_LEARNING_CONFIDENCE
            category = str(raw_item.get("category", "general"))
            learnings.append({"pattern": pattern, "confidence": confidence, "category": category})

    # Extract the top-level ``learnings_compacted`` array — the wholesale
    # replacement store emitted by the groom re-distillation step. Unlike
    # ``learnings`` (incremental), each entry carries ``merged_from`` (source
    # learning ids absorbed) instead of a ``confidence``; confidence/recency are
    # re-derived deterministically by the harvester. Normalized to
    # {"pattern": str, "category": str, "merged_from": list[str]}. Capped well
    # above max_entries (the harvester's top_k is the real bound) so a full-store
    # compaction is never truncated here, while a pathological array still can't
    # grow unbounded.
    compacted_raw = data.get("learnings_compacted", [])
    learnings_compacted: list[JsonObject] = []
    if isinstance(compacted_raw, list):
        for raw_item in compacted_raw[:_MAX_COMPACTED_ENTRIES]:
            if not isinstance(raw_item, dict):
                continue
            pattern = raw_item.get("pattern", "")
            if not isinstance(pattern, str) or not pattern:
                continue
            category = str(raw_item.get("category", "general"))
            merged_raw = raw_item.get("merged_from", [])
            merged_from = (
                [str(m) for m in merged_raw if isinstance(m, str)]
                if isinstance(merged_raw, list)
                else []
            )
            learnings_compacted.append(
                {"pattern": pattern, "category": category, "merged_from": merged_from}
            )

    return SkillResult(
        success=success,
        artifacts=artifacts,
        issues_created=issues_created,
        requested_mutations=requested_mutations,
        error=error,
        spec_compliance=spec_compliance,
        blocking_findings=blocking_findings,
        prior_verdict=prior_verdict,
        prior_blocking_findings=prior_blocking_findings,
        issues_closed=issues_closed,
        issue_picked_up=issue_picked_up,
        branch=branch,
        tests_passed=tests_passed,
        verification_evidence=verification_evidence,
        review_patterns=review_patterns,
        learnings=learnings,
        learnings_compacted=learnings_compacted,
    )


def _coerce_blocking(findings_obj: JsonObject | None) -> int | None:
    """Pull the ``blocking`` count from a findings_count-shaped object."""
    if findings_obj is None:
        return None
    raw_blocking = findings_obj.get("blocking")
    # bool is a subclass of int; accept only real ints. JSON-shaped input can
    # plausibly carry a numeric string ("3"); accept that too. Anything else
    # is dropped.
    if isinstance(raw_blocking, bool):
        return None
    if isinstance(raw_blocking, int):
        return raw_blocking
    if isinstance(raw_blocking, str):
        try:
            return int(raw_blocking)
        except ValueError:
            return None
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
