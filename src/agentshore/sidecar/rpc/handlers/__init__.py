"""Per-family RPC handler dispatcher functions.

Each sub-module exposes one ``_dispatch_*`` function that owns all
methods in its family.  The router imports them into :mod:`..router` and
registers them in the :data:`HANDLERS` dispatch table.
"""

from __future__ import annotations
