"""Archive RPC helpers (DESIGN §5.2).

Pure async functions that back the ``archive.list``, ``archive.fetch_report``,
and ``archive.fetch_logs`` JSON-RPC methods. Errors surface as
:class:`ArchiveError` with a ``code`` mapping onto JSON-RPC error codes:

* ``-32602`` (INVALID_PARAMS) for malformed input or unknown ``archive_id``.
* ``-32004`` (ERR_REPORT_NOT_FOUND) when an archive exists but its report or
  log file is absent on disk.
* ``-32603`` (INTERNAL_ERROR) for unexpected I/O failures.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from agentshore.data.store import DataStore


INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
ERR_REPORT_NOT_FOUND = -32004


class ArchiveError(Exception):
    """Raised by archive RPC helpers; carries a JSON-RPC error code."""

    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


class ArchiveListEntry(TypedDict):
    archive_id: str
    session_id: str
    archive_path: str
    total_cost: float
    final_alignment: float
    total_plays: int
    created_at: str


class ReportSection(TypedDict):
    id: str
    title: str


class FetchReportResult(TypedDict):
    html_path: str
    sections: list[ReportSection]


class FetchLogsResult(TypedDict):
    lines: list[str]


class LogRange(TypedDict):
    start: int
    end: int


async def list_archives(store: DataStore) -> list[ArchiveListEntry]:
    """Return all archive rows as dicts, newest first."""
    records = await store.list_archives()
    return [
        ArchiveListEntry(
            archive_id=r.archive_id,
            session_id=r.session_id,
            archive_path=r.archive_path,
            total_cost=r.total_cost,
            final_alignment=r.final_alignment,
            total_plays=r.total_plays,
            created_at=r.created_at,
        )
        for r in records
    ]


async def fetch_report(
    store: DataStore,
    archive_id: str,
    *,
    report_path_override: str | None = None,
) -> FetchReportResult:
    """Return ``{html_path, sections}`` for a stored archive.

    ``report_path_override`` lets tests inject a known HTML fixture without
    placing files on disk under the live archive directory. Production callers
    omit it; the report path is then derived from ``archive_path``/``report.html``.
    """
    if not isinstance(archive_id, str) or not archive_id:
        raise ArchiveError("archive_id must be a non-empty string", code=INVALID_PARAMS)

    if report_path_override is not None:
        html_path = Path(report_path_override)
    else:
        record = await store.get_archive(archive_id)
        if record is None:
            raise ArchiveError(f"archive not found: {archive_id}", code=INVALID_PARAMS)
        html_path = Path(record.archive_path) / "report.html"

    if not html_path.exists():
        raise ArchiveError(
            f"report not found on disk: {html_path}",
            code=ERR_REPORT_NOT_FOUND,
        )

    try:
        content = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchiveError(f"failed to read report: {exc}", code=INTERNAL_ERROR) from exc

    sections = _parse_sections(content)
    return FetchReportResult(html_path=str(html_path), sections=sections)


async def fetch_logs(
    store: DataStore,
    archive_id: str,
    *,
    range_: LogRange | dict[str, int] | None = None,
    log_path_override: str | None = None,
) -> FetchLogsResult:
    """Return ``{lines}`` for a stored archive's session log.

    ``range`` is ``{start, end}`` 1-indexed inclusive; omitted ⇒ first 200
    lines. Malformed range raises ``ArchiveError`` with INVALID_PARAMS.
    """
    if not isinstance(archive_id, str) or not archive_id:
        raise ArchiveError("archive_id must be a non-empty string", code=INVALID_PARAMS)

    start, end = _resolve_range(range_)

    if log_path_override is not None:
        log_path = Path(log_path_override)
    else:
        record = await store.get_archive(archive_id)
        if record is None:
            raise ArchiveError(f"archive not found: {archive_id}", code=INVALID_PARAMS)
        log_path = Path(record.archive_path) / "session.log"

    if not log_path.exists():
        raise ArchiveError(
            f"logs not found on disk: {log_path}",
            code=ERR_REPORT_NOT_FOUND,
        )

    try:
        with log_path.open(encoding="utf-8") as fh:
            lines: list[str] = []
            for idx, raw in enumerate(fh, start=1):
                if idx < start:
                    continue
                if idx > end:
                    break
                lines.append(raw.rstrip("\n"))
    except OSError as exc:
        raise ArchiveError(f"failed to read logs: {exc}", code=INTERNAL_ERROR) from exc

    return FetchLogsResult(lines=lines)


def _resolve_range(range_: object) -> tuple[int, int]:
    if range_ is None:
        return 1, 200
    if not isinstance(range_, dict):
        raise ArchiveError("range must be an object", code=INVALID_PARAMS)
    start = range_.get("start")
    end = range_.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ArchiveError("range.start and range.end must be integers", code=INVALID_PARAMS)
    if start < 1 or end < start:
        raise ArchiveError(
            "range must satisfy 1 <= start <= end",
            code=INVALID_PARAMS,
        )
    return start, end


class _SectionExtractor(HTMLParser):
    """Pull ``<section id="X"><h2>Title</h2>`` anchors out of an ESR HTML doc."""

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[ReportSection] = []
        self._pending_id: str | None = None
        self._in_h2: bool = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            attrs_dict = dict(attrs)
            section_id = attrs_dict.get("id")
            self._pending_id = section_id if isinstance(section_id, str) else None
            self._in_h2 = False
            self._title_buf = []
        elif tag == "h2" and self._pending_id is not None:
            self._in_h2 = True
            self._title_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._in_h2 and self._pending_id is not None:
            title = "".join(self._title_buf).strip()
            self.sections.append(ReportSection(id=self._pending_id, title=title))
            self._pending_id = None
            self._in_h2 = False
            self._title_buf = []
        elif tag == "section":
            self._pending_id = None
            self._in_h2 = False
            self._title_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._title_buf.append(data)


def _parse_sections(html: str) -> list[ReportSection]:
    parser = _SectionExtractor()
    parser.feed(html)
    parser.close()
    return parser.sections
