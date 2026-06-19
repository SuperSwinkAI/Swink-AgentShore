"""JSON-RPC 2.0 server sub-package for the AgentShore sidecar.

Public surface lives at :mod:`agentshore.sidecar.server`; this package
contains the internal implementation split across:

- :mod:`.protocol` — wire types, error codes, factory helpers, session state
- :mod:`.serve` — stdio serve loop and entry points
- :mod:`.router` — dispatch table and request routing
- :mod:`.handlers` — per-family dispatcher functions
"""

from __future__ import annotations
