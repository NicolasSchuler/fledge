"""Report output formats and the one dispatch commands use to write a final report.

A report is redacted once and every format is rendered from that one redacted copy. The
places a report names for its reader, the settings file and the files of CI annotations,
are worked out here from the real paths, before redaction generalizes them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import click
from rich.console import Console

from .ci_annotations import render_redacted_ci_annotations
from .compact_report import render_redacted_compact
from .finding_view import DEFAULT_RENDER_OPTIONS, RenderOptions
from .html_report import render_redacted_html
from .report import RedactedReport, Report, redact_report, redacted_json
from .terminal_report import brief_status, print_redacted_report, render_redacted_terminal

# A path that climbs more directories than this from the working directory is shown whole.
_MAX_PARENT_STEPS = 2
# The repository root of a GitHub Actions job, which annotation paths are relative to.
_WORKSPACE_VARIABLE = "GITHUB_WORKSPACE"


class OutputFormat(StrEnum):
    """How a command writes its final report to standard output.

    Attributes:
        TERMINAL: Text for the terminal, printed with styling to the console.
        COMPACT: Short plain records.
        JSON: The JSON report.
        HTML: The HTML report.
        CI: CI annotations.
    """

    TERMINAL = "terminal"
    COMPACT = "compact"
    JSON = "json"
    HTML = "html"
    CI = "ci"


def render_redacted_document(
    report: RedactedReport,
    output_format: OutputFormat,
    *,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
) -> str:
    """Render an already redacted report as text; the terminal format has no styling.

    Args:
        report: The redacted report.
        output_format: The format to render.
        options: Rendering choices for the terminal, compact and CI formats.
        preview_images: PDF preview images for the HTML format.

    Returns:
        The rendered document.
    """
    match output_format:
        case OutputFormat.TERMINAL:
            return render_redacted_terminal(report, options=options)
        case OutputFormat.COMPACT:
            return render_redacted_compact(report, options=options)
        case OutputFormat.JSON:
            return redacted_json(report)
        case OutputFormat.HTML:
            return render_redacted_html(report, preview_images=preview_images)
        case OutputFormat.CI:
            return render_redacted_ci_annotations(report, options=options)
        case _:
            assert_never(output_format)


def render_document(
    report: Report,
    output_format: OutputFormat,
    *,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
) -> str:
    """Render the report as text; the terminal format is rendered without styling.

    Args:
        report: The report to redact and render.
        output_format: The format to render.
        options: Rendering choices, as for :func:`render_redacted_document`.
        preview_images: PDF preview images for the HTML format.

    Returns:
        The rendered document.
    """
    return render_redacted_document(
        redact_report(report), output_format, options=options, preview_images=preview_images
    )


def write_redacted_report(
    report: RedactedReport,
    output_format: OutputFormat,
    *,
    console: Console,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
    document: str | None = None,
) -> None:
    """Write the final, already redacted report to standard output.

    Args:
        report: The redacted report.
        output_format: The format to write. The terminal format prints styled text to
            ``console``; every other format is echoed as plain text.
        console: The console for the terminal format.
        options: Rendering choices, as for :func:`render_redacted_document`.
        preview_images: PDF preview images for the HTML format.
        document: Text the caller already rendered in ``output_format`` from this exact
            report, such as a saved JSON or HTML file; when ``None`` the report is
            rendered here. The terminal format does not use it.
    """
    if output_format is OutputFormat.TERMINAL:
        if (status := brief_status(report)) is not None:
            # A rejected request or a cancelled run says nothing about the paper: one short
            # message on stderr, as for a usage error, and nothing on stdout.
            click.echo(status, err=True)
            return
        print_redacted_report(report, console, options=options)
        return
    if document is None:
        document = render_redacted_document(
            report, output_format, options=options, preview_images=preview_images
        )
    # A JSON document already ends with a newline.
    click.echo(document, nl=output_format is not OutputFormat.JSON)


def write_report(
    report: Report,
    output_format: OutputFormat,
    *,
    console: Console,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
    document: str | None = None,
) -> None:
    """Redact ``report`` once, then write it to standard output as ``write_redacted_report``.

    Args:
        report: The report to redact and write.
        output_format: The format to write.
        console: The console for the terminal format.
        options: Rendering choices, as for :func:`render_redacted_document`.
        preview_images: PDF preview images for the HTML format.
        document: Text already rendered from this exact report, as for
            :func:`write_redacted_report`.
    """
    write_redacted_report(
        redact_report(report),
        output_format,
        console=console,
        options=options,
        preview_images=preview_images,
        document=document,
    )


def working_directory_path(path: str | Path) -> str:
    """Name a file as the user can open it from the working directory.

    Args:
        path: An absolute path.

    Returns:
        The path relative to the working directory when it is inside it or at most
        ``_MAX_PARENT_STEPS`` directories above, otherwise the absolute path.
    """
    absolute = Path(path)
    try:
        relative = Path(os.path.relpath(absolute, Path.cwd()))
    except ValueError:  # another drive on Windows
        return str(absolute)
    if relative.parts.count("..") > _MAX_PARENT_STEPS:
        return str(absolute)
    return relative.as_posix()


def annotation_prefix(source: Path) -> str:
    """Return the directory of a project relative to the repository root, for CI annotations.

    GitHub resolves the file of an annotation relative to the repository root, which is
    ``GITHUB_WORKSPACE`` in a job and the working directory elsewhere, while findings
    name files relative to the project.

    Args:
        source: The input of the run.

    Returns:
        The project directory relative to the root, in POSIX form; empty when the input
        is a ZIP, is the root itself or lies outside it.
    """
    if not source.is_dir():
        return ""
    root = Path(os.environ.get(_WORKSPACE_VARIABLE) or Path.cwd()).resolve()
    try:
        relative = source.resolve().relative_to(root)
    except ValueError:
        return ""
    return "" if relative == Path() else relative.as_posix()
