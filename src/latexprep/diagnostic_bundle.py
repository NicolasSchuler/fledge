"""Explicitly unverified diagnostic ZIP exports of sanitized reports, logs and diffs."""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .errors import PreparationError
from .finding_view import redact_report
from .findings import Finding
from .html_report import render_redacted_html
from .limits import MIB
from .path_safety import is_relative_posix_path
from .report import Outcome, Report

_TEXT_SUFFIXES = frozenset(
    {".log", ".txt", ".tex", ".bib", ".diff", ".json", ".out", ".err", ".sty", ".cls"}
)
_MAX_ARTIFACT_BYTES = 2 * MIB
_MAX_ARTIFACT_TOTAL = 8 * MIB


def _scratch_text(root: Path, relative: str) -> str:
    if (
        not is_relative_posix_path(relative, characters="literal")
        or PurePosixPath(relative).suffix.lower() not in _TEXT_SUFFIXES
    ):
        raise PreparationError("Diagnostic artifacts require allowlisted relative text-file paths")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    try:
        components = relative.split("/")
        for component in components[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ARTIFACT_BYTES:
            raise PreparationError("Diagnostic artifact is not a bounded regular text file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(_MAX_ARTIFACT_BYTES + 1)
        if len(data) > _MAX_ARTIFACT_BYTES or b"\0" in data:
            raise PreparationError("Diagnostic artifact is oversized or not supported text")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise PreparationError(
            "Diagnostic artifact cannot be read safely as local UTF-8 text"
        ) from error
    finally:
        os.close(directory)
        if descriptor is not None:
            os.close(descriptor)


def export_diagnostic_bundle(
    report: Report,
    destination: Path,
    *,
    scratch_root: Path | None = None,
    artifacts: Sequence[str] = (),
    logs: Mapping[str, str] | None = None,
) -> Path:
    """Create an exclusive ZIP of sanitized, explicitly unverified diagnostic text.

    No submission sources/PDFs are copied implicitly. Explicit scratch artifacts
    must be regular UTF-8 files with allowlisted suffixes; links and binary files
    are rejected. All text is redacted together before any archive is written.
    """
    if destination.exists() or destination.is_symlink():
        raise PreparationError("Diagnostic destination already exists")
    if isinstance(artifacts, str) or any(not isinstance(item, str) for item in artifacts):
        raise PreparationError("Diagnostic artifacts must be a sequence of text paths")
    if len(artifacts) > 100 or len(set(artifacts)) != len(artifacts):
        raise PreparationError("Choose at most 100 distinct diagnostic artifacts")
    if artifacts and scratch_root is None:
        raise PreparationError("Explicit diagnostic artifacts require a scratch root")
    if logs is not None and any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in logs.items()
    ):
        raise PreparationError("Diagnostic logs require text names and contents")
    selected = []
    total = 0
    for index, relative in enumerate(artifacts, 1):
        if not isinstance(relative, str) or scratch_root is None:
            raise PreparationError("Diagnostic artifact paths must be text")
        content = _scratch_text(scratch_root, relative)
        total += len(content.encode("utf-8"))
        if total > _MAX_ARTIFACT_TOTAL:
            raise PreparationError("Diagnostic text artifacts exceed the total size limit")
        selected.append(
            {
                "entry": f"scratch/{index:04d}{PurePosixPath(relative).suffix.lower()}",
                "source": relative,
                "content": content,
            }
        )
    envelope = redact_report(
        replace(
            report,
            outcome=Outcome.BLOCKED,
            scope="Unverified diagnostic export; this is not a submission package",
            findings=[
                *report.findings,
                Finding(
                    "report.diagnostic_bundle",
                    "This diagnostic export is unverified and cannot be used as a submission "
                    "package.",
                    severity="info",
                    status="inconclusive",
                    details={"source_outcome": report.outcome},
                ),
            ],
            execution={
                **report.execution,
                "diagnostic_export": {
                    "source_outcome": report.outcome,
                    "source_scope": report.scope,
                    "logs": dict(logs or {}),
                    "scratch": selected,
                },
            },
        )
    )
    diagnostic = envelope.execution.get("diagnostic_export")
    if not isinstance(diagnostic, dict):
        raise PreparationError("Diagnostic data exceeded safe report redaction limits")
    entries = {
        "README.txt": "UNVERIFIED DIAGNOSTIC BUNDLE\n\nThis archive contains sanitized diagnostic "
        "material. It is not a submission package and has not been rebuilt or verified. "
        "Credential detection is bounded and heuristic; review content before sharing.\n",
        # Report.to_json redacts its input again; the HTML reuses the envelope as redacted.
        "report.json": envelope.to_json(),
        "report.html": render_redacted_html(envelope),
        "logs/stages.json": json.dumps(envelope.stages, indent=2, ensure_ascii=False) + "\n",
        "logs/explicit.json": json.dumps(diagnostic.get("logs", {}), indent=2, ensure_ascii=False)
        + "\n",
    }
    for index, change in enumerate(envelope.changes, 1):
        if change.diff:
            entries[f"diffs/{index:04d}.diff"] = change.diff
    clean_scratch = diagnostic.get("scratch")
    if not isinstance(clean_scratch, list) or len(clean_scratch) != len(selected):
        raise PreparationError("Diagnostic artifacts could not be safely sanitized")
    for index, item in enumerate(clean_scratch):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise PreparationError("Diagnostic artifact could not be safely sanitized")
        # Archive names come from generated control data, never redacted source names.
        entries[selected[index]["entry"]] = item["content"]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, contents.encode("utf-8"))
    try:
        with destination.open("xb") as stream:
            stream.write(output.getvalue())
    except OSError as error:
        raise PreparationError(
            "Cannot write diagnostic bundle to the requested destination"
        ) from error
    return destination
