"""`fledge init`: write a starter or preset fledge.toml without prompting."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import click

from .config import load_settings, resolve_config_path
from .models import PreparationError
from .presets import PRESETS, preset_names, preset_text

CONFIG_NAME = "fledge.toml"
# Matches the CLI's "error" outcome; usage errors use the CLI's exit code 64.
WRITE_ERROR_EXIT_CODE = 4

STARTER = """\
# Fledge project settings. Every value is a constraint you supply for this
# submission; Fledge maintains no venue rules. Uncomment only what applies.
# `fledge rules` lists check codes; `fledge init --list` shows example presets.

# Relative path of the document to build when the project has several roots.
# main = "main.tex"
# PDF001: total pages, including references and appendices. Replace 0 with your limit.
# max_pages = 0

[checks]
select = ["ALL"] # Codes, families such as "TEX", or prefixes such as "BIB1".
ignore = [] # For example ["TEX001"] to skip unfinished-text markers.
"""


class InitError(click.ClickException):
    exit_code = WRITE_ERROR_EXIT_CODE


def _print_presets() -> None:
    width = max(len(name) for name in PRESETS)
    click.echo("Presets (starting points, not certifications of venue compliance):")
    for name in preset_names():
        click.echo(f"  {name.ljust(width)}  {PRESETS[name]}")
    click.echo("")
    click.echo("Copy one for editing:     fledge init --preset NAME")
    click.echo("Apply one beneath config: fledge check INPUT --preset NAME")


def _nearest_config(directory: Path) -> Path | None:
    try:
        return resolve_config_path(directory)
    except PreparationError:
        return None


def _next_steps(directory: Path) -> list[str]:
    resolved = directory.resolve()
    source = "." if resolved == Path.cwd().resolve() else str(directory)
    output = os.path.relpath(resolved.parent / f"{resolved.name}-submission")
    return [
        f"  fledge check {shlex.quote(source)}",
        f"  fledge prepare {shlex.quote(source)} --output {shlex.quote(output)}",
    ]


@click.command("init")
@click.argument(
    "directory",
    required=False,
    default=".",
    metavar="[DIRECTORY]",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--preset",
    type=click.Choice(preset_names()),
    help="Start from an example preset instead of the minimal starter file.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing fledge.toml.")
@click.option("--list", "list_presets", is_flag=True, help="List the presets and exit.")
def init_command(directory: Path, preset: str | None, force: bool, list_presets: bool) -> int:
    """Write fledge.toml into DIRECTORY (default: the current directory).

    Presets switch on generic checks and leave venue values, such as page
    limits and identity terms, as commented placeholders for you to fill in.
    They are starting points, not certifications of compliance. An existing
    fledge.toml is kept unless --force is given.
    """
    if list_presets:
        if preset is not None or force:
            raise click.UsageError("--list cannot be combined with --preset or --force")
        _print_presets()
        return 0
    target = directory / CONFIG_NAME
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise InitError(f"{target} is not a regular file; refusing to write through it")
    if target.exists() and not force:
        raise InitError(f"{target} already exists; pass --force to overwrite it")
    if preset is not None:
        text = f"# Written by `fledge init --preset {preset}`.\n" + preset_text(preset)
    else:
        text = STARTER
    previous = _nearest_config(directory)
    try:
        with target.open("w" if force else "x", encoding="utf-8") as stream:
            stream.write(text)
        load_settings(target, {})
    except (OSError, PreparationError) as error:
        raise InitError(f"Cannot write a valid {target}: {error}") from error
    current = _nearest_config(directory)
    origin = f" from the {preset} preset" if preset is not None else ""
    click.echo(f"Wrote {target}{origin}.")
    if current is not None and current != target.resolve():
        click.echo(
            f"Warning: {current} takes precedence over {CONFIG_NAME} here; "
            f"move or merge it, or pass --config {target}.",
            err=True,
        )
    elif previous is not None and previous != target.resolve():
        click.echo(f"Note: this file now takes precedence over {previous}.")
    if preset is not None:
        click.echo(
            "Fill in the commented placeholders from your venue's current instructions; "
            "the preset does not certify compliance."
        )
    else:
        click.echo("Uncomment the settings that apply to your submission.")
    click.echo("Next steps:")
    for line in _next_steps(directory):
        click.echo(line)
    return 0
