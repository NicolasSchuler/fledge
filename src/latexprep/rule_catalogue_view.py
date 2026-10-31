"""Terminal views of the check catalogue: the list of checks and the explanation of one.

Shows what a researcher needs to choose or understand a check. The tests that
demonstrate a check are developer documentation and appear only in the JSON form.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .rule_definition import Rule


def print_rule_table(rules: Sequence[Rule], console: Console) -> None:
    """List every given check with its code and title.

    Args:
        rules: The checks to list, in the order given.
        console: The console that prints the table.
    """
    table = Table(title=f"{len(rules)} registered checks", box=None, padding=(0, 2, 0, 0))
    table.add_column("Code", style="bold", no_wrap=True)
    table.add_column("Check")
    for rule in rules:
        table.add_row(Text(rule.code), Text(rule.title))
    console.print(table)
    console.print(Text("Explain a check: fledge rule CODE", style="dim"))


def print_rule_explanation(rule: Rule, console: Console) -> None:
    """Explain one check: what it examines, the suggested fix and its diagnostic name.

    Args:
        rule: The check to explain.
        console: The console that prints the explanation.
    """
    console.print(Text(f"{rule.code}: {rule.title}", style="bold"))
    console.print(Text(rule.description))
    console.print(Text(f"Next: {rule.fix}"))
    console.print(Text(f"Diagnostic name: {rule.name}"))
