"""Report rendering (RPT-001..003): pure functions from an immutable ReportData to a document.

No DB, no network. The same ReportData yields the same markdown/HTML text, the same PDF bytes
(creation date pinned to generated_at) and the same XLSX cell content, so a rendered report's hash
can be approved before delivery.
"""
from __future__ import annotations

from typing import Literal

from analystos.contracts.reports import ReportData
from analystos.core.errors import InvalidInput

ReportFormat = Literal["md", "html", "pdf", "xlsx"]

FORMATS: dict[str, tuple[str, str]] = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
}


def render(data: ReportData, fmt: ReportFormat) -> tuple[bytes, str, str]:
    """Render `data` as `fmt`; returns (content bytes, MIME type, file extension)."""
    if fmt not in FORMATS:
        raise InvalidInput(f"unsupported report format {fmt!r}; expected one of {sorted(FORMATS)}")
    mime, ext = FORMATS[fmt]
    if fmt == "md":
        from analystos.reports.narrative import render_markdown

        return render_markdown(data).encode("utf-8"), mime, ext
    if fmt == "html":
        from analystos.reports.narrative import render_html

        return render_html(data).encode("utf-8"), mime, ext
    if fmt == "pdf":
        from analystos.reports.pdf import render_pdf

        return render_pdf(data), mime, ext
    from analystos.reports.excel import render_xlsx

    return render_xlsx(data), mime, ext


__all__ = ["FORMATS", "ReportFormat", "render"]
