"""CLI adapter for source-only submission text export.

Previews the title, abstract and keywords of a paper in the form a submission system
asks for them, or prints one field ready to paste. Reading and converting the text is in
``submission_text``; no TeX compilation or network access is involved.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import click
from rich.text import Text

from .command_options import (
    debug_option,
    main_option,
    reject_config_with_isolated,
    settings_file_options,
)
from .config import load_settings
from .config_discovery import resolve_config_path
from .errors import PreparationError
from .import_budget import ImportLimits
from .project import import_project
from .report import Outcome
from .submission_text import FIELD_NAMES, SubmissionField, read_submission_text
from .terminal_console import stdout_console
from .text_safety import escape_single_line_text

# Increases only when a field of the JSON output is removed, renamed or changes meaning;
# new fields may appear within a version.
SUBMISSION_TEXT_SCHEMA_VERSION = 1


def _read_fields(
    source: Path, main: str | None, config: Path | None, isolated: bool
) -> dict[str, SubmissionField]:
    """Import the project into a temporary folder and read its submission fields."""
    settings = load_settings(resolve_config_path(source, config, isolated=isolated), {"main": main})
    with TemporaryDirectory(prefix="fledge-submission-text-") as temporary:
        limits = ImportLimits.from_settings(settings)
        imported = import_project(source, Path(temporary) / "project", limits)
        return read_submission_text(imported.root, settings.main)


def _print_fields(fields: dict[str, SubmissionField]) -> None:
    """The preview: each field with its status, candidates and review notes."""
    console = stdout_console()
    for name, value in fields.items():
        console.print(Text(f"{name.capitalize()} ({value.status})", style="bold"))
        console.print(Text(value.draft))
        for candidate in value.candidates:
            console.print(Text("Candidate: " + candidate))
        for issue in value.issues:
            console.print(Text("Review: " + issue))


def _print_one_field(name: str, fields: dict[str, SubmissionField]) -> None:
    """Print a ready field as one line; otherwise explain on stderr and exit as blocked."""
    selected = fields[name]
    if selected.status == "ready":
        click.echo(selected.draft)
        return
    click.echo(f"{name}: {selected.status}; no copy-ready text exported.", err=True)
    for issue in selected.issues:
        click.echo(f"  {issue}", err=True)
    raise click.exceptions.Exit(Outcome.BLOCKED.exit_code)


@click.command(
    "submission-text", short_help="Preview title, abstract and keywords for a submission form."
)
@click.argument("source", type=click.Path(exists=True, path_type=Path), metavar="INPUT")
@main_option
@click.option(
    "--field",
    type=click.Choice(FIELD_NAMES),
    help="Print only this field as one line of text ready to paste; the exit code is 3 "
    "when it is not ready.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Return all fields and readiness details."
)
@settings_file_options
@debug_option
def submission_text_command(
    source: Path,
    main: str | None,
    field: str | None,
    json_output: bool,
    config: Path | None,
    isolated: bool,
) -> None:
    """Preview title, abstract and keywords, or export one ready field as a single line.

    INPUT is a project folder or ZIP. No TeX compilation or network access is used.
    """
    if json_output and field is not None:
        raise click.UsageError("--json returns all fields; do not combine it with --field")
    reject_config_with_isolated(config, isolated)
    try:
        fields = _read_fields(source, main, config, isolated)
    except (PreparationError, OSError) as error:
        click.echo(escape_single_line_text(f"Cannot export submission text: {error}"), err=True)
        raise click.exceptions.Exit(Outcome.ERROR.exit_code) from error
    if json_output:
        payload = {
            "schema_version": SUBMISSION_TEXT_SCHEMA_VERSION,
            "fields": {name: asdict(value) for name, value in fields.items()},
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    elif field is not None:
        _print_one_field(field, fields)
        return
    else:
        _print_fields(fields)
    if any(value.status == "unresolved" for value in fields.values()):
        raise click.exceptions.Exit(Outcome.BLOCKED.exit_code)
