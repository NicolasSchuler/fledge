"""Explicitly unverified diagnostic ZIP exports of sanitized reports and diffs.

The bundle holds only what a redacted report already contains: the JSON and HTML
reports, the stage list and the diffs of proposed changes. No submission source or PDF
is copied, and the archive says plainly that it is not a submission package.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

from .errors import PreparationError
from .findings import Finding
from .html_report import render_redacted_html
from .report import Outcome, RedactedReport, Report, redact_report, redacted_json

_README = (
    "UNVERIFIED DIAGNOSTIC BUNDLE\n\nThis archive contains sanitized diagnostic "
    "material. It is not a submission package and has not been rebuilt or verified. "
    "Credential detection is bounded and heuristic; review content before sharing.\n"
)
# Archive entries carry a fixed time and mode, so equal reports give equal archives.
_ENTRY_TIME = (1980, 1, 1, 0, 0, 0)
_ENTRY_MODE = 0o100644 << 16


def _envelope(public: RedactedReport) -> RedactedReport:
    """The report labelled as an unverified export of ``public``, with its outcome blocked.

    The additions are fixed text and the already redacted outcome and scope, so the
    envelope needs no second redaction.
    """
    return RedactedReport(
        replace(
            public,
            outcome=Outcome.BLOCKED,
            scope="Unverified diagnostic export; this is not a submission package",
            findings=[
                *public.findings,
                Finding(
                    "report.diagnostic_bundle",
                    "This diagnostic export is unverified and cannot be used as a submission "
                    "package.",
                    severity="info",
                    status="inconclusive",
                    details={"source_outcome": public.outcome},
                ),
            ],
            execution={
                **public.execution,
                "diagnostic_export": {
                    "source_outcome": public.outcome,
                    "source_scope": public.scope,
                },
            },
        )
    )


def _entries(envelope: RedactedReport) -> dict[str, str]:
    """The text of every archive entry, by entry name."""
    entries = {
        "README.txt": _README,
        "report.json": redacted_json(envelope),
        "report.html": render_redacted_html(envelope),
        "logs/stages.json": json.dumps(envelope.stages, indent=2, ensure_ascii=False) + "\n",
    }
    for index, change in enumerate(envelope.changes, 1):
        if change.diff:
            entries[f"diffs/{index:04d}.diff"] = change.diff
    return entries


def _archive(entries: dict[str, str]) -> bytes:
    """A deflated ZIP of ``entries`` in name order."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=_ENTRY_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _ENTRY_MODE
            archive.writestr(info, contents.encode("utf-8"))
    return output.getvalue()


def export_redacted_diagnostic_bundle(public: RedactedReport, destination: Path) -> Path:
    """Create an exclusive ZIP of the unverified diagnostic text of a redacted report.

    Raises ``PreparationError`` if ``destination`` exists or cannot be written.
    """
    if destination.exists() or destination.is_symlink():
        raise PreparationError("Diagnostic destination already exists")
    data = _archive(_entries(_envelope(public)))
    try:
        with destination.open("xb") as stream:
            stream.write(data)
    except OSError as error:
        raise PreparationError(
            "Cannot write diagnostic bundle to the requested destination"
        ) from error
    return destination


def export_diagnostic_bundle(report: Report, destination: Path) -> Path:
    """Redact ``report``, then export it as ``export_redacted_diagnostic_bundle`` does."""
    return export_redacted_diagnostic_bundle(redact_report(report), destination)
