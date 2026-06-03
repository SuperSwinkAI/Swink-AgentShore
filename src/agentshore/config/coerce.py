"""Small value-coercion helpers shared across config/YAML readers."""

from __future__ import annotations


def str_or_none(value: object) -> str | None:
    """Return *value* if it is a ``str``, else ``None``.

    The single narrowing boundary for ``dict.get(key)`` reads of optional
    string fields (identity tokens, YAML scalars). Callers pass the already
    looked-up value, e.g. ``str_or_none(d.get("gh_token_env"))``. Non-string
    values (including ``None``) collapse to ``None`` rather than being coerced,
    so a malformed YAML scalar never silently becomes a stringified token.
    """
    return value if isinstance(value, str) else None
