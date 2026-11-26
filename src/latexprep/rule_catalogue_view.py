"""Terminal views of the check catalogue: the list of checks and the explanation of one.

Shows what a researcher needs to choose or understand a check, including whether it runs
by default, only once configured, always, or only when selected as a suggestion. The
tests that demonstrate a check are developer documentation and appear only in the JSON
form.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .config import Settings
from .rule_catalogue import (
    ALWAYS,
    DEFAULT,
    SUGGESTION,
    RuleActivation,
    activation_options,
    rule_activation,
)
from .rule_definition import Rule

_ACTIVATION_HELP = (
    "default: runs without settings; opt-in: runs once configured or requested "
    "(fledge rule CODE says how); suggestion: style advice that runs only when selected and "
    "never changes the outcome; always: a guard that ignoring its code does not switch off."
)


def rule_payload(rule: Rule, defaults: Settings) -> dict[str, object]:
    """Return one check as plain data for the JSON forms of ``fledge rules`` and ``fledge rule``.

    Args:
        rule: The check.
        defaults: The settings without any configuration, which decide its activation.

    Returns:
        The fields of the rule with its ``activation`` and the settings that enable it.
    """
    activation = rule_activation(rule, defaults)
    return {
        **rule.to_dict(),
        "activation": activation.kind,
        "enabled_by": list(activation_options(activation)),
    }


def print_rule_table(rules: Sequence[Rule], console: Console, *, defaults: Settings) -> None:
    """List the given checks with their code, how they are switched on and their title.

    Args:
        rules: The checks to list, in the order given.
        console: The console that prints the table.
        defaults: The settings without any configuration, which decide the activation.
    """
    noun = "check" if len(rules) == 1 else "checks"
    table = Table(title=f"{len(rules)} registered {noun}", box=None, padding=(0, 2, 0, 0))
    table.add_column("Code", style="bold", no_wrap=True)
    table.add_column("Runs", no_wrap=True)
    table.add_column("Check")
    for rule in rules:
        table.add_row(Text(rule.code), Text(rule_activation(rule, defaults).kind), Text(rule.title))
    console.print(table)
    console.print(Text(_ACTIVATION_HELP, style="dim"))
    console.print(Text("Explain a check: fledge rule CODE", style="dim"))


def _activation_line(activation: RuleActivation) -> str:
    """Say in one line when a check runs."""
    if activation.kind == ALWAYS:
        return "Runs: always, whenever its stage runs; ignoring its code does not switch it off."
    if activation.kind == DEFAULT:
        return "Runs: by default; [checks] ignore or --ignore turns it off."
    options = activation_options(activation)
    if activation.kind == SUGGESTION:
        return (
            "Runs: only when selected by its code, family or prefix; to add it to the other "
            f"checks, use {options[0]}. A suggestion never changes the outcome."
        )
    line = (
        f"Runs: only with {activation.command}."
        if activation.command
        else f"Runs: only once configured; set {' or set '.join(options)}."
    )
    return f"{line} {activation.note.replace('`', '')}" if activation.note else line


def print_rule_explanation(rule: Rule, console: Console, *, defaults: Settings) -> None:
    """Explain one check: what it examines, when it runs, the suggested fix and its name.

    Args:
        rule: The check to explain.
        console: The console that prints the explanation.
        defaults: The settings without any configuration, which decide when it runs.
    """
    console.print(Text(f"{rule.code}: {rule.title}", style="bold"))
    console.print(Text(rule.description))
    console.print(Text(_activation_line(rule_activation(rule, defaults))))
    console.print(Text(f"Next: {rule.fix}"))
    if rule.reference_url:
        console.print(Text(f"Reference: {rule.reference_url}"))
    console.print(Text(f"Diagnostic name: {rule.name}"))
