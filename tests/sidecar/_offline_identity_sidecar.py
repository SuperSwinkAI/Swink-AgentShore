"""Run the sidecar with session.start's identity token gates kept offline.

Subprocess counterpart of ``conftest._offline_identity_token_gates``: tests
that spawn a real sidecar process can't reach it with ``unittest.mock``, and
their fixture identities name GitHub logins that aren't on the test machine.
Launch this file instead of ``python -m agentshore.sidecar``.
"""

from __future__ import annotations

from agentshore.agents import identity as identity_mod

identity_mod.report_identities = lambda cfg: []  # type: ignore[assignment]
identity_mod.report_identity_repo_access = lambda cfg, project_path: []  # type: ignore[assignment]

from agentshore.sidecar.server import run  # noqa: E402

run()
