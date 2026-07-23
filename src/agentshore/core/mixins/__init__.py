"""Orchestrator mixin package — each module groups related methods.

Mixins do not define ``__init__`` and do not call ``super().__init__``.
They access ``self._store``, ``self._runtime.cfg``, etc. via the type
annotations declared on :class:`agentshore.core.base._OrchestratorBase` which
is the rightmost entry in :class:`agentshore.core.orchestrator.Orchestrator`'s
MRO and the only base that supplies ``__init__``.
"""

from __future__ import annotations
