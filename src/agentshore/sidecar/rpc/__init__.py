"""JSON-RPC 2.0 server sub-package for the AgentShore sidecar.

Public surface lives at :mod:`agentshore.sidecar.server`; this package
contains the internal implementation split across:

- :mod:`.protocol` — wire types, error codes, factory helpers, session state
- :mod:`.router` — dispatch table and request routing
- :mod:`.handlers` — per-family dispatcher functions

The single stdio serve loop and process entry points live in
:mod:`agentshore.sidecar.server`.
"""

from __future__ import annotations
