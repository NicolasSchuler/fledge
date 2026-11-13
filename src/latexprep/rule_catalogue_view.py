"""Terminal views of the check catalogue: the list of checks and the explanation of one.

Shows what a researcher needs to choose or understand a check, including whether it runs
by default, only once configured, always, or only when selected as a suggestion. The
tests that demonstrate a check are developer documentation and appear only in the JSON
form.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import check_policy
from .config import Settings, toml_table
from .rule_definition import Rule
from .rules import MANDATORY_CODES

# How a check is switched on; the catalogue page (docs/checks.md) uses the same groups.
ALWAYS = "always"
DEFAULT = "default"
OPT_IN = "opt-in"
SUGGESTION = "suggestion"
# Opt-in checks switched on outside the activation tables of the selection policy. The
# catalogue generator, scripts/generate_check_docs.py, holds the same entry, and a test
# checks that both classify every rule alike.
_ACTIVATED_ELSEWHERE: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "FMT001": (None, ("apply_formatting",)),
}
_ACTIVATION_HELP = (
    "default: runs without settings; opt-in: runs once configured (fledge rule CODE says "
    "how); suggestion: style advice that runs only when selected and never changes the "
    "outcome; always: a guard that ignoring its code does not switch off."
)


class RuleActivation(NamedTuple):
    """How a check is switched on.

    Attributes:
        kind: ``ALWAYS``, ``DEFAULT``, ``OPT_IN`` or ``SUGGESTION``.
        enabled_by: For an opt-in check, the settings that switch it on, written as a
            settings file spells them (``check_orcid in [manuscript_checks]``); otherwise
            empty.
    """

    kind: str
    enabled_by: tuple[str, ...] = ()


def _activation_entries(code: str) -> list[tuple[str | None, tuple[str, ...]]]:
    """List the (table, fields) pairs that switch the opt-in check ``code`` on, if any."""
    entries = [
        (group, tuple(names)) for group, names in check_policy.opt_in_activation().get(code, ())
    ]
    if not entries and code in _ACTIVATED_ELSEWHERE:
        entries.append(_ACTIVATED_ELSEWHERE[code])
    return entries


def rule_activation(rule: Rule, defaults: Settings) -> RuleActivation:
    """Classify a check as always enforced, on by default, opt-in or a suggestion.

    Args:
        rule: The check.
        defaults: The settings without any configuration.

    Returns:
        The kind of the check and, for an opt-in check, the settings that enable it.
    """
    if rule.code in MANDATORY_CODES:
        return RuleActivation(ALWAYS)
    if rule.suggestion:
        return RuleActivation(SUGGESTION, (f"--select ALL --select {rule.code}",))
    entries = _activation_entries(rule.code)
    if not entries:
        return RuleActivation(DEFAULT)
    values = defaults.top_level_values()
    group, names = entries[0]
    if any(values[name] if group is None else getattr(values[group], name) for name in names):
        return RuleActivation(DEFAULT)
    tables = " or ".join(dict.fromkeys(f"[{toml_table(item)}]" for item, _ in entries if item))
    return RuleActivation(
        OPT_IN, tuple(f"{name} in {tables}" if tables else name for name in names)
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
        "enabled_by": list(activation.enabled_by),
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
    if activation.kind == SUGGESTION:
        return (
            "Runs: only when selected by its code, family or prefix; to add it to the other "
            f"checks, use {activation.enabled_by[0]}. A suggestion never changes the outcome."
        )
    return f"Runs: only once configured; set {' or '.join(activation.enabled_by)}."


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
