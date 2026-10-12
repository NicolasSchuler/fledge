"""Click option helpers shared by the command adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import click

F = TypeVar("F", bound=Callable[..., object])


def compose_options(*decorators: Callable[[F], F]) -> Callable[[F], F]:
    """Apply option decorators as if they were stacked in the listed order."""

    def apply(function: F) -> F:
        for decorator in reversed(decorators):
            function = decorator(function)
        return function

    return apply


# --config and --isolated, which select the settings file of every command.
settings_file_options = compose_options(
    click.option(
        "--config",
        type=click.Path(path_type=Path),
        help="Use this TOML settings file instead of discovering project configuration.",
    ),
    click.option(
        "--isolated", is_flag=True, help="Ignore discovered configuration and use defaults."
    ),
)


def reject_config_with_isolated(config: Path | None, isolated: bool) -> None:
    if isolated and config is not None:
        raise click.UsageError("--isolated cannot be combined with --config")
