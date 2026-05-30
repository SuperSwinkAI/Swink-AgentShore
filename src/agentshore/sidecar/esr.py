"""End-Session-Report builder (DESIGN §5.2).

Adapts :class:`agentshore.reports.collector.ReportDataCollector` output into the
JSON-RPC wire shape consumed by Screen 10 (``EndSessionReportScreen``) and the
``session.completed`` notification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from agentshore.reports.collector import ReportDataCollector

if TYPE_CHECKING:
    from agentshore.data.store import DataStore
    from agentshore.reports.types import EndSessionReportData


class EsrPayload(TypedDict):
    session_id: str
    exit_reason: str
    exit_code: int
    archive_path: str
    report_path: str
    log_path: str | None
    esr_summary: EndSessionReportData


async def build_esr_payload(
    store: DataStore,
    session_id: str,
    *,
    archive_path: str,
    report_path: str,
    log_path: str | None,
    exit_reason: str,
    exit_code: int,
) -> EsrPayload:
    """Build the ``session.stop`` / ``session.completed`` result payload."""
    collector = ReportDataCollector(store)
    summary = await collector.collect_end_session_report(session_id)
    return EsrPayload(
        session_id=session_id,
        exit_reason=exit_reason,
        exit_code=exit_code,
        archive_path=archive_path,
        report_path=report_path,
        log_path=log_path,
        esr_summary=summary,
    )
