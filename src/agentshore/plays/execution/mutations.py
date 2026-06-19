"""Idempotency-key builder for external mutations."""

from __future__ import annotations

import hashlib
import json


def build_idempotency_key(session_id: str, mutation: dict[str, object]) -> str:
    """Build a globally-unique idempotency key for an external mutation.

    The key is a 16-character hex prefix of the SHA-256 digest of
    ``{"session": session_id, **mutation}`` (keys sorted for stability).

    ``session_id`` is always embedded so that cross-session runs cannot
    collide: a pending row from a killed session cannot block the same
    mutation in a fresh session.

    Raises ``ValueError`` if *session_id* is empty.
    """
    if not session_id:
        raise ValueError("session_id must not be empty when building an idempotency key")
    key_payload = {"session": session_id, **mutation}
    return hashlib.sha256(json.dumps(key_payload, sort_keys=True).encode()).hexdigest()[:16]
