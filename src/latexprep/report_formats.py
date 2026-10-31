"""Report output formats and the one dispatch commands use to write a final report.

A report is redacted once and every format is rendered from that one redacted copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import assert_never

import click
from rich.console import Console

from .ci_annotations import render_redacted_ci_annotations
from .compact_report import render_redacted_compact
from .finding_view import DEFAULT_RENDER_OPTIONS, RenderOptions
from .html_report import render_redacted_html
from .report import RedactedReport, Report, redact_report, redacted_json
from .terminal_report import print_redacted_report, render_redacted_terminal


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
