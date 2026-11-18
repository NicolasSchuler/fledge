"""Saving the report files a command was asked for.

Owns ``--json-report``, ``--html-report`` and ``--diagnostics``: validating that each
names a new file outside the input, and writing them from one redacted copy of the
report. A file that cannot be saved is recorded in the report itself, so the saved JSON
and the printed report agree. Printing the final report is in ``report_formats``.
"""

from __future__ import annotations

import errno
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .command_options import record_internal_error
from .defects import is_expected_failure
from .diagnostic_bundle import export_redacted_diagnostic_bundle
from .errors import PreparationError, RequestError
from .file_safety import write_new_file, write_sibling_temporary
from .findings import Finding
from .html_report import render_redacted_html
from .publication import validate_destination
from .publication_folders import is_fledge_report
from .report import Outcome, RedactedReport, Report, redact_report, redacted_json


class ReportIdentity(NamedTuple):
    """Tells the earlier report that was validated from a file that took its place.

    Attributes:
        device: The device that holds the file (``st_dev``).
        inode: The file's inode number (``st_ino``).
    """

    device: int
    inode: int


class ChangedReportError(FileExistsError):
    """A report destination changed while the job ran, so the new report was kept beside it.

    Args:
        destination: The report file that was not written.

    Attributes:
        kept: The temporary file that holds the new report.
    """

    def __init__(self, destination: Path, kept: Path) -> None:
        super().__init__(
            errno.EEXIST,
            f"it changed while the job ran, so it was left as it is; the new report is in {kept}",
            str(destination),
        )
        self.kept = kept


@dataclass(slots=True)
class ReportFiles:
    """New report files outside the input; each is set once its option was validated.

    Attributes:
        json: The ``--json-report`` destination.
        html: The ``--html-report`` destination.
        diagnostics: The ``--diagnostics`` destination.
        replace_existing: Whether the JSON and HTML destinations may hold an earlier
            Fledge report, as ``--force`` allows; it is then replaced only once the new
            file is completely written. Otherwise a file is created only where none exists.
        earlier: The earlier Fledge report at a JSON or HTML destination when its option
            was validated, by destination; only that file is replaced. A destination
            without one held nothing.
    """

    json: Path | None = None
    html: Path | None = None
    diagnostics: Path | None = None
    replace_existing: bool = False
    earlier: dict[Path, ReportIdentity] = field(default_factory=dict)

    def remember_earlier_report(self, destination: Path) -> None:
        """Record which file is at the validated ``destination``, if any, to replace only it.

        Without ``replace_existing`` the destination holds nothing, so nothing is recorded.
        """
        if not self.replace_existing:
            return
        try:
            status = destination.lstat()
        except FileNotFoundError:
            return
        self.earlier[destination] = ReportIdentity(status.st_dev, status.st_ino)


class RenderedReport(NamedTuple):
    """The JSON text of a report, and its HTML if that is still the final report.

    Attributes:
        json: The JSON text.
        html: The HTML text, or ``None`` when none was rendered or a later write failure
            changed the report.
    """

    json: str
    html: str | None


def resolve_report_files(
    source: Path,
    *,
    json: Path | None,
    html: Path | None,
    diagnostics: Path | None,
    files: ReportFiles,
    force: bool = False,
    output: Path | None = None,
    preview_output: Path | None = None,
) -> None:
    """Validate each requested destination against ``source`` and record it in ``files``.

    Args:
        source: The input project.
        json: The requested ``--json-report`` file, if any.
        html: The requested ``--html-report`` file, if any.
        diagnostics: The requested ``--diagnostics`` file, if any.
        files: Receives each validated destination.
        force: Whether ``--force`` lets the JSON and HTML reports replace an earlier Fledge
            report; the new report then replaces it only once it is complete. A
            diagnostic file is always new.
        output: The requested bundle directory.
        preview_output: The requested preview directory.

    Raises:
        RequestError: A destination overlaps an output directory or cannot be written
            safely outside ``source``.
        PreparationError: Two file destinations are the same.
    """
    reports = [path.resolve() for path in (json, html, diagnostics) if path is not None]
    folders = [path.resolve() for path in (output, preview_output) if path is not None]
    if len(set(reports)) != len(reports):
        raise PreparationError("Output, JSON, HTML and diagnostics destinations must be distinct")
    if any(
        report.is_relative_to(folder) or folder.is_relative_to(report)
        for report in reports
        for folder in folders
    ):
        raise RequestError(
            "Report files must be saved outside the output and preview folders",
            "Choose --json-report, --html-report and --diagnostics paths outside --output "
            "and --preview-output; the output folder receives its own report.json.",
        )
    files.replace_existing = force
    if json:
        files.json = validate_destination(source, json, "--json-report", force=force)
        files.remember_earlier_report(files.json)
    if html:
        files.html = validate_destination(source, html, "--html-report", force=force)
        files.remember_earlier_report(files.html)
    if diagnostics:
        files.diagnostics = validate_destination(source, diagnostics, "--diagnostics")


def record_write_failure(report: Report, option: str, destination: Path, error: Exception) -> None:
    """Record that a requested report file could not be saved, in the report itself.

    An unexpected error is recorded as an internal error instead.

    Args:
        report: The report that receives the finding and the error outcome.
        option: The option that named the file, such as ``--json-report``.
        destination: The file that could not be saved.
        error: Why saving failed.
    """
    if not is_expected_failure(error):
        record_internal_error(report, error)
        return
    reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    if isinstance(error, ChangedReportError):
        suggestion = (
            f"Check what took the place of {destination} while the job ran; keep the new "
            f"report from {error.kept}, or rerun the command."
        )
    else:
        suggestion = (
            f"Choose a {option} path in a folder that exists and is writable, with no file at "
            "that path yet, then rerun the command."
        )
    if report.outcome not in {Outcome.INTERNAL_ERROR, Outcome.CANCELLED}:
        report.outcome = Outcome.ERROR
    report.findings.append(
        Finding(
            "report.write",
            f"Cannot write {option} {destination}: {reason}",
            severity="error",
            status="failed",
            suggestion=suggestion,
        )
    )


def _earlier_report_mode(destination: Path, earlier: ReportIdentity | None) -> int | None:
    """Return the permission bits of the validated earlier report that ``destination`` holds.

    Args:
        destination: The report destination.
        earlier: The earlier report at ``destination`` when it was validated, if any.

    Returns:
        The permission bits, or ``None`` when nothing is at ``destination``.

    Raises:
        FileExistsError: Something else is there: a link, another file, or one that no
            longer begins like a Fledge report.
    """
    try:
        status = destination.lstat()
    except FileNotFoundError:
        return None
    if (
        earlier is None
        or not stat.S_ISREG(status.st_mode)
        or ReportIdentity(status.st_dev, status.st_ino) != earlier
        or not is_fledge_report(destination)
    ):
        raise FileExistsError(errno.EEXIST, "it changed while the job ran", str(destination))
    return stat.S_IMODE(status.st_mode)


def write_report_text(
    destination: Path,
    text: str,
    *,
    replace_existing: bool,
    earlier: ReportIdentity | None = None,
) -> None:
    """Write one report file, never leaving a partial file at ``destination``.

    Args:
        destination: The validated destination.
        text: The complete text of the file.
        replace_existing: Whether an earlier report at ``destination`` may be replaced. The
            text then goes to a temporary file beside it, which takes its place only once
            it is complete, and only while ``destination`` holds nothing or still holds
            ``earlier``; otherwise the file is created only if none exists.
        earlier: The earlier report at ``destination`` when it was validated, if any. The
            temporary file is private until it receives that report's permissions.

    Raises:
        ChangedReportError: Something else took the place of ``destination`` while the job
            ran; it is left as it is, and the temporary file, which the error names, keeps
            the new report.
        OSError: The file could not be written.
    """
    data = text.encode("utf-8")
    if not replace_existing:
        write_new_file(destination, data)
        return
    temporary = write_sibling_temporary(destination, data, private=earlier is not None)
    try:
        # Checked just before the replacement: nothing that appeared meanwhile is replaced.
        mode = _earlier_report_mode(destination, earlier)
    except FileExistsError:
        raise ChangedReportError(destination, temporary) from None
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        if mode is None:
            destination.hardlink_to(temporary)
            temporary.unlink()
        else:
            temporary.chmod(mode)
            temporary.replace(destination)
    except FileExistsError:
        raise ChangedReportError(destination, temporary) from None
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def save_reports(
    report: Report, files: ReportFiles, preview_images: dict | None
) -> tuple[RedactedReport, RenderedReport]:
    """Save the requested report files; a write failure is recorded in the report itself.

    The report is redacted once and every file is written from that copy. A failure adds
    a finding to ``report``, so the copy is made again and the later files include it.
    The HTML and diagnostics files are written before the JSON file, so a failure is
    reflected in the saved JSON.

    Args:
        report: The report to save.
        files: The validated destinations; an unset one is not written.
        preview_images: PDF preview images for the HTML file.

    Returns:
        The final redacted report with the text of its JSON and, if still valid, HTML.
    """
    public = redact_report(report)
    # Rendered once for the file and reused for stdout unless a later write failure
    # changes the report.
    html_document = None
    # Write the HTML and diagnostics first, so a failure is reflected in the saved JSON.
    if files.html is not None and _can_write_report(files, files.html, public.outcome):
        try:
            html_document = render_redacted_html(public, preview_images=preview_images)
            write_report_text(
                files.html,
                html_document,
                replace_existing=files.replace_existing,
                earlier=files.earlier.get(files.html),
            )
        except Exception as error:  # noqa: BLE001 - a failed file becomes a finding with its cause
            record_write_failure(report, "--html-report", files.html, error)
            public, html_document = redact_report(report), None
    if files.diagnostics is not None:
        try:
            export_redacted_diagnostic_bundle(public, files.diagnostics)
        except Exception as error:  # noqa: BLE001 - a failed file becomes a finding with its cause
            record_write_failure(report, "--diagnostics", files.diagnostics, error)
            public, html_document = redact_report(report), None
    encoded = redacted_json(public)
    if files.json is not None and _can_write_report(files, files.json, public.outcome):
        try:
            write_report_text(
                files.json,
                encoded,
                replace_existing=files.replace_existing,
                earlier=files.earlier.get(files.json),
            )
        except Exception as error:  # noqa: BLE001 - a failed file becomes a finding with its cause
            record_write_failure(report, "--json-report", files.json, error)
            public, html_document = redact_report(report), None
            encoded = redacted_json(public)
    return public, RenderedReport(encoded, html_document)


def _can_write_report(files: ReportFiles, path: Path, outcome: Outcome) -> bool:
    """Write a new report for any outcome and replace an earlier one only after success."""
    return path not in files.earlier or outcome.passed or outcome is Outcome.PLANNED
