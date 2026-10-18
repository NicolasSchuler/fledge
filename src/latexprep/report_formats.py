"""Report output formats and the one dispatch commands use to write a final report."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import assert_never

import click
from rich.console import Console

from .ci_annotations import render_ci_annotations
from .finding_view import DEFAULT_RENDER_OPTIONS, RenderOptions
from .html_report import render_html
from .models import Report
from .terminal_report import print_report, render_compact, render_terminal

# The scope of a report whose request was rejected before or while the job ran.
REQUEST_SCOPE = "request validation"
# The scope of a report whose job was cancelled or hit a defect before it finished.
UNFINISHED_SCOPE = "run did not finish"


class OutputFormat(StrEnum):
    """How a command writes its final report to standard output."""

    TERMINAL = "terminal"
    COMPACT = "compact"
    JSON = "json"
    HTML = "html"
    CI = "ci"


def render_document(
    report: Report,
    output_format: OutputFormat,
    *,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
) -> str:
    """Render the report as text; the terminal format is rendered without styling."""
    match output_format:
        case OutputFormat.TERMINAL:
            return render_terminal(report, options=options)
        case OutputFormat.COMPACT:
            return render_compact(report, options=options)
        case OutputFormat.JSON:
            return report.to_json()
        case OutputFormat.HTML:
            return render_html(report, preview_images=preview_images)
        case OutputFormat.CI:
            return render_ci_annotations(report, options=options)
        case _:
            assert_never(output_format)


def write_report(
    report: Report,
    output_format: OutputFormat,
    *,
    console: Console,
    options: RenderOptions = DEFAULT_RENDER_OPTIONS,
    preview_images: Mapping[str, bytes] | None = None,
    document: str | None = None,
) -> None:
    """Write the final report to standard output.

    The terminal format prints styled text to ``console``; every other format is
    echoed as plain text. ``document`` is text the caller already rendered in
    ``output_format`` from this exact report, such as a saved JSON or HTML file.
    """
    if output_format is OutputFormat.TERMINAL:
        print_report(report, console, options=options)
        return
    if document is None:
        document = render_document(
            report, output_format, options=options, preview_images=preview_images
        )
    # A JSON document already ends with a newline.
    click.echo(document, nl=output_format is not OutputFormat.JSON)
