"""Shared wire-framing discipline for every JSON transport in AgentShore.

Both the IPC NDJSON envelope (:mod:`agentshore.ipc.serializer`) and the
sidecar JSON-RPC stdout writes (:mod:`agentshore.sidecar.server`) frame their
payloads the same way: serialize to a single line, append exactly one ``\\n``,
and guarantee the result is valid JSON. Historically only the IPC path applied
``_json_safe`` + ``allow_nan=False``; the sidecar path used bare ``json.dumps``
and could emit ``Infinity``/``NaN`` — invalid JSON that trips the browser's
parser with ``JSONDecodeError``. This module is the one place that policy lives
so both transports share it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping


def json_safe(value: object) -> object:
    """Return a JSON-safe copy of ``value`` with non-finite floats nulled out.

    ``NaN``/``Infinity``/``-Infinity`` are not valid JSON; they are replaced
    with ``None`` so the encoded payload always parses in a strict reader
    (notably the browser ``JSON.parse``). Containers are copied recursively;
    scalars pass through unchanged.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def frame(obj: object) -> str:
    """Serialize ``obj`` to a single newline-terminated JSON line.

    Applies :func:`json_safe` and ``allow_nan=False`` so the framed line is
    always valid JSON. This is the canonical "serialize, append exactly one
    ``\\n``" rule used by every write site on both transports.
    """
    return json.dumps(json_safe(obj), allow_nan=False) + "\n"


__all__ = ["frame", "json_safe"]
