"""Saving the report files a command was asked for.

Owns ``--json-report``, ``--html-report`` and ``--diagnostics``: validating that each
names a new file outside the input, and writing them from one redacted copy of the
report. A file that cannot be saved is recorded in the report itself, so the saved JSON
and the printed report agree. Printing the final report is in ``report_formats``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .command_options import record_internal_error
from .defects import is_expected_failure
from .diagnostic_bundle import export_redacted_diagnostic_bundle
from .findings import Finding
from .html_report import render_redacted_html
from .publication import validate_destination
from .report import Outcome, RedactedReport, Report, redact_report, redacted_json


@dataclass(slots=True)
class ReportFiles:
    """New report files outside the input; each is set once its option was validated."""

    json: Path | None = None
    html: Path | None = None
    diagnostics: Path | None = None


class RenderedReport(NamedTuple):
    """The JSON text of a report, and its HTML if that is still the final report."""

    json: str
    html: str | None


def resolve_report_files(
    source: Path,
    *,
    json: Path | None,
    html: Path | None,
    diagnostics: Path | None,
    files: ReportFiles,
) -> None:
    """Validate each requested destination against ``source`` and record it in ``files``."""
    if json:
        files.json = validate_destination(source, json, "--json-report")
    if html:
        files.html = validate_destination(source, html, "--html-report")
    if diagnostics:
        files.diagnostics = validate_destination(source, diagnostics, "--diagnostics")


def record_write_failure(report: Report, option: str, destination: Path, error: Exception) -> None:
    """Record that a requested report file could not be saved, in the report itself."""
    if not is_expected_failure(error):
        record_internal_error(report, error)
        return
    reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    report.outcome = Outcome.ERROR
    report.findings.append(
        Finding(
            "report.write",
            f"Cannot write {option} {destination}: {reason}",
            severity="error",
            status="failed",
            suggestion=f"Choose a {option} path in a folder that exists and is writable, with "
            "no file at that path yet, then rerun the command.",
        )
    )


def save_reports(
    report: Report, files: ReportFiles, preview_images: dict | None
) -> tuple[RedactedReport, RenderedReport]:
    """Save the requested report files; a write failure is recorded in the report itself.

    The report is redacted once and every file is written from that copy. A failure adds
    a finding to ``report``, so the copy is made again and the later files include it.
    Returns the final redacted report with the text of its JSON and, if still valid, HTML.
    """
    public = redact_report(report)
    # Rendered once for the file and reused for stdout unless a later write failure
    # changes the report.
    html_document = None
    # Write the HTML and diagnostics first, so a failure is reflected in the saved JSON.
    if files.html is not None:
        try:
            html_document = render_redacted_html(public, preview_images=preview_images)
            with files.html.open("x", encoding="utf-8") as stream:
                stream.write(html_document)
        except Exception as error:
            record_write_failure(report, "--html-report", files.html, error)
            public, html_document = redact_report(report), None
    if files.diagnostics is not None:
        try:
            export_redacted_diagnostic_bundle(public, files.diagnostics)
        except Exception as error:
            record_write_failure(report, "--diagnostics", files.diagnostics, error)
            public, html_document = redact_report(report), None
    encoded = redacted_json(public)
    if files.json is not None:
        try:
            with files.json.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        except Exception as error:
            record_write_failure(report, "--json-report", files.json, error)
            public, html_document = redact_report(report), None
            encoded = redacted_json(public)
    return public, RenderedReport(encoded, html_document)
