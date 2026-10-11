"""CLI adapter for source-only submission text export."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import click
from rich.console import Console
from rich.text import Text

from .config import load_settings, resolve_config_path
from .models import PreparationError
from .project import ImportLimits, import_project
from .submission_text import read_submission_text, safe_display


@click.command("submission-text")
@click.argument("source", type=click.Path(exists=True, path_type=Path), metavar="INPUT")
@click.option("--main", help="Relative main TeX file inside the project.")
@click.option("--field", type=click.Choice(["title", "abstract", "keywords"]))
@click.option(
    "--json", "json_output", is_flag=True, help="Return all fields and readiness details."
)
@click.option("--config", type=click.Path(path_type=Path), help="Use this settings file.")
@click.option("--isolated", is_flag=True, help="Ignore discovered project settings.")
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
    if isolated and config is not None:
        raise click.UsageError("--isolated cannot be combined with --config")
    try:
        settings = load_settings(
            resolve_config_path(source, config, isolated=isolated), {"main": main}
        )
        with TemporaryDirectory(prefix="fledge-submission-text-") as temporary:
            limits = ImportLimits(settings.max_files, settings.max_bytes, settings.max_depth)
            imported = import_project(source, Path(temporary) / "project", limits)
            fields = read_submission_text(imported.root, settings.main)
    except (PreparationError, OSError) as error:
        click.echo(safe_display(f"Cannot export submission text: {error}"), err=True)
        raise click.exceptions.Exit(4) from error
    if json_output:
        click.echo(json.dumps({"fields": fields}, ensure_ascii=False, indent=2))
    elif field is not None:
        selected = fields[field]
        if selected["status"] == "ready":
            click.echo(selected["draft"])
            return
        click.echo(f"{field}: {selected['status']}; no copy-ready text exported.", err=True)
        for issue in selected["issues"]:
            click.echo(f"  {issue}", err=True)
        raise click.exceptions.Exit(3)
    else:
        console = Console()
        for name, value in fields.items():
            console.print(Text(f"{name.capitalize()} ({value['status']})", style="bold"))
            console.print(Text(str(value["draft"])))
            for candidate in value["candidates"]:
                console.print(Text("Candidate: " + str(candidate)))
            for issue in value["issues"]:
                console.print(Text("Review: " + str(issue)))
    if any(value["status"] == "unresolved" for value in fields.values()):
        raise click.exceptions.Exit(3)
