"""Generated YAML wire-schema helpers backed by frozen config dataclasses."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, cast

from pydantic import ConfigDict, TypeAdapter, ValidationError, with_config

from agentshore.errors import ConfigError

_WIRE_CONFIG = ConfigDict(extra="forbid")


def _adapter(model: type[Any]) -> TypeAdapter[Any]:
    """Generate a strict Pydantic adapter for a stdlib dataclass."""
    with_config(_WIRE_CONFIG)(model)
    return TypeAdapter(model)


def mapping(value: object, path: str) -> Mapping[str, object]:
    """Require a YAML mapping at *path* and return it without coercion."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")
    return value


def structure[ConfigT](
    model: type[ConfigT], raw: Mapping[str, object], path: str
) -> ConfigT:
    """Validate and construct one frozen config dataclass from YAML input."""
    try:
        return cast("ConfigT", _adapter(model).validate_python(dict(raw)))
    except ValidationError as exc:
        detail = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in detail["loc"])
        field_path = ".".join(part for part in (path, location) if part)
        raise ConfigError(f"{field_path}: {detail['msg']}") from None


def reject_unknown_fields(
    model: type[Any],
    raw: Mapping[str, object],
    path: str,
    *,
    allow: frozenset[str] = frozenset(),
    exclude: frozenset[str] = frozenset(),
) -> None:
    """Reject keys outside the dataclass-generated wire field set.

    Hand-written parsers call this before applying range checks, aliases, or
    legacy migrations that cannot be represented by direct construction.
    """
    known = {field.name for field in dataclasses.fields(model)} - exclude | allow
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        raise ConfigError(f"{path}.{unknown[0]}: unexpected configuration field")
