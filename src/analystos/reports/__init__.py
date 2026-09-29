"""Report rendering (RPT-001..003): pure functions from an immutable ReportData to a document.

No DB, no network. The same ReportData yields the same markdown/HTML text, the same PDF bytes
(creation date pinned to generated_at), the same PPTX/DOCX bytes (zip entries dated generated_at)
and the same XLSX cell content, so a rendered report's hash can be approved before delivery.
"""
from __future__ import annotations

from typing import Literal

from analystos.contracts.reports import ReportData
from analystos.core.errors import FeatureUnavailable, InvalidInput

ReportFormat = Literal["md", "html", "pdf", "xlsx", "pptx", "docx"]

FORMATS: dict[str, tuple[str, str]] = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
}


# PDF, XLSX, PPTX and DOCX need the `reports` extra (ADR-0025). Markdown and HTML never do. The Office
# formats (N-10) also need their own libraries, checked per format so an older image missing them keeps PDF/XLSX.
EXTRA_FORMATS = ("pdf", "xlsx", "pptx", "docx")
FORMAT_MODULES: dict[str, tuple[str, str]] = {"pptx": ("pptx", "python-pptx"), "docx": ("docx", "python-docx")}


def format_reason(fmt: str) -> str | None:
    """None when `fmt` can be rendered here, else why not and how to add it."""
    from analystos.core.profiles import extra_reason, importable

    if fmt not in EXTRA_FORMATS:
        return None
    reason = extra_reason("reports")
    if reason or fmt not in FORMAT_MODULES:
        return reason
    module, dist = FORMAT_MODULES[fmt]
    if importable(module):
        return None
    return (f"needs {dist} from the `reports` extra; not installed here: {module}. "
            "Install analystos[reports] or use an image that has it")


def unavailable_formats(formats: tuple[str, ...] | list[str]) -> dict[str, str]:
    """format -> why it cannot be rendered on this installation (empty = all can)."""
    return {f: reason for f in formats if (reason := format_reason(f))}


def render(data: ReportData, fmt: ReportFormat) -> tuple[bytes, str, str]:
    """Render `data` as `fmt`; returns (content bytes, MIME type, file extension)."""
    if fmt not in FORMATS:
        raise InvalidInput(f"unsupported report format {fmt!r}; expected one of {sorted(FORMATS)}")
    mime, ext = FORMATS[fmt]
    if fmt in EXTRA_FORMATS:
        from analystos.core.profiles import require_extra

        require_extra("reports", f"{fmt.upper()} reports")
        if reason := format_reason(fmt):
            raise FeatureUnavailable(f"{fmt.upper()} reports are unavailable: {reason}",
                                     details={"extra": "reports", "feature": f"{fmt.upper()} reports",
                                              "missing": [FORMAT_MODULES[fmt][0]]})
    if fmt == "md":
        from analystos.reports.narrative import render_markdown

        return render_markdown(data).encode("utf-8"), mime, ext
    if fmt == "html":
        from analystos.reports.narrative import render_html

        return render_html(data).encode("utf-8"), mime, ext
    if fmt == "pdf":
        from analystos.reports.pdf import render_pdf

        return render_pdf(data), mime, ext
    if fmt == "pptx":
        from analystos.reports.slides import render_pptx

        return render_pptx(data), mime, ext
    if fmt == "docx":
        from analystos.reports.word import render_docx

        return render_docx(data), mime, ext
    from analystos.reports.excel import render_xlsx

    return render_xlsx(data), mime, ext


__all__ = ["EXTRA_FORMATS", "FORMATS", "FORMAT_MODULES", "ReportFormat", "format_reason", "render", "unavailable_formats"]
