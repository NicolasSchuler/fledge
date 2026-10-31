"""Layout override candidates (``MAN002``) and required or forbidden line numbers (``MAN017``).

Both policies read how the document sets up its page: spacing, page breaks, type size
and geometry overrides that a venue may forbid, and the ``lineno`` activation that
review copies need and final versions must not have. Layout candidates are advisory
and never infer intent; line numbering recognizes only literal ``lineno`` activation,
not class-specific review modes.
"""

from __future__ import annotations

import re

from .cancellation import cancellation_point
from .expanded_document import ExpandedDocument
from .finding_text import list_locations, plural
from .finding_weight import ADVISORY
from .findings import Finding
from .literal_text import PACKAGE_COMMANDS, first_argument_names, literal_list
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, policy_finding
from .manuscript_vocabulary import FLOAT_ENVIRONMENTS, LAYOUT_COMMANDS, LENGTH_COMMANDS
from .tex_lexer import Command, command_end, definition_spans, in_conditional, in_half_open_span

_LAYOUT = Policy("manuscript.layout_manipulation", "Checked layout override candidates.", ADVISORY)
_LINE_NUMBER_RULE = "manuscript.line_numbers"
_LINE_NUMBER_COMMANDS = {"linenumbers", "runninglinenumbers", "pagewiselinenumbers"}
_LINE_NUMBER_OPTIONS = {"lineno", "linenumbers"}
# Source characters read after a spacing or length command to see whether it sets a value.
_VALUE_LOOKAHEAD_CHARS = 80
# Source characters of a layout command quoted in the details.
_SNIPPET_CHARS = 160


def _is_layout_override(document: ExpandedDocument, command: Command) -> bool:
    """Return whether a command is a layout override candidate.

    Candidates are negative spacing, a change or assignment of a layout length, a float
    placed with ``H`` or ``!``, and every command in ``LAYOUT_COMMANDS``.

    Args:
        document: The expanded document, read after the command for an unbraced value.
        command: The command to classify.

    Returns:
        ``True`` when the command overrides spacing, page breaks, type size or geometry.
    """
    match command.name:
        case "hspace" | "vspace" | "hskip" | "vskip" | "kern":
            value = (
                command.arguments[0].value
                if command.arguments
                else document.text[command.name_end :][:_VALUE_LOOKAHEAD_CHARS]
            )
            return bool(re.match(r"\s*-", value))
        case "setlength" | "addtolength" if command.arguments:
            return command.arguments[0].value.strip().lstrip("\\") in LENGTH_COMMANDS
        case name if name in LENGTH_COMMANDS:
            following = document.text[command_end(command) :][:_VALUE_LOOKAHEAD_CHARS]
            return bool(re.match(r"\s*(?:=\s*)?[-+]?\d", following))
        case "begin" if command.arguments:
            return command.arguments[0].value in FLOAT_ENVIRONMENTS and any(
                "H" in argument.value or "!" in argument.value for argument in command.options
            )
        case name:
            return name in LAYOUT_COMMANDS


def _layout_overrides(document: ExpandedDocument) -> list[dict[str, object]]:
    """Locate the layout override candidates outside conditionals.

    Args:
        document: The expanded document to scan.

    Returns:
        Each candidate's location with its command name and a source snippet of at most
        ``_SNIPPET_CHARS`` characters.
    """
    # A command inside a conditional may never execute, so it is reported as an
    # interpretation gap rather than as a confirmed override.
    return [
        document.located(
            command.start,
            command=command.name,
            source=document.text[
                command.start : min(command_end(command), command.start + _SNIPPET_CHARS)
            ],
        )
        for command in document.commands
        if _is_layout_override(document, command)
        and not in_conditional(document.conditionals, command.start)
    ]


def layout_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Find the layout override candidates when that advisory is switched on.

    Args:
        reading: The expanded document and its document-wide interpretation gaps.
        options: ``check_layout_manipulation`` switches the advisory on.

    Returns:
        One ``manuscript.layout_manipulation`` finding, or an empty list when the advisory
        is off.
    """
    if not options.check_layout_manipulation:
        return []
    outcome = CheckOutcome(_layout_overrides(reading.document), reading.uncertainty)
    return [policy_finding(_LAYOUT, outcome)]


def _declared_packages(command: Command) -> list[str]:
    """List the packages a package declaration names literally; other commands name none."""
    if command.name not in PACKAGE_COMMANDS:
        return []
    return first_argument_names(command) or []


def _line_number_activation(command: Command) -> str | None:
    """Name the literal line-number activation a command performs, if any.

    Args:
        command: The command to classify.

    Returns:
        A description of the activation (a class option, the ``lineno`` package option, a
        line-number command or environment), or ``None``.
    """
    option_names = {name for item in command.options for name in literal_list(item.value) or []}
    environment = (
        command.arguments[0].value.strip()
        if command.name == "begin" and command.arguments
        else None
    )
    packages = _declared_packages(command)
    if command.name == "documentclass" and option_names & _LINE_NUMBER_OPTIONS:
        return "class option " + ", ".join(sorted(option_names & _LINE_NUMBER_OPTIONS))
    if "lineno" in packages and "linenumbers" in option_names:
        return "lineno package option linenumbers"
    if command.name in _LINE_NUMBER_COMMANDS:
        return "\\" + command.name
    if environment in _LINE_NUMBER_COMMANDS:
        return f"{environment} environment"
    return None


def line_number_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Find literal lineno activation; class-specific review modes are not interpreted.

    An activation inside a macro definition, a group or a conditional is an
    interpretation gap rather than an activation.

    Args:
        reading: The expanded document.
        options: ``require_line_numbers`` or ``forbid_line_numbers`` switches the policy on
            and decides which way it points.

    Returns:
        One ``manuscript.line_numbers`` finding, or an empty list when neither option is
        set.
    """
    if not (options.require_line_numbers or options.forbid_line_numbers):
        return []
    document = reading.document
    definitions = definition_spans(document.text, document.commands)
    activations: list[dict[str, object]] = []
    loaded: list[dict[str, object]] = []
    reasons = list(document.graph_uncertainty)
    for command in document.commands:
        cancellation_point()
        kind = _line_number_activation(command)
        if kind is None:
            if "lineno" in _declared_packages(command):
                loaded.append(document.location(command.start).as_dict())
            continue
        location = document.location(command.start)
        where = list_locations([location.as_dict()])
        if in_half_open_span(definitions, command.start):
            reasons.append(f"{where}: {kind} inside a macro definition")
        elif command.depth or in_conditional(document.conditionals, command.start):
            reasons.append(f"{where}: grouped or conditional {kind}")
        else:
            activations.append({**location.as_dict(), "activation": kind})
    details: dict[str, object] = {
        "policy": "require" if options.require_line_numbers else "forbid",
        "activations": activations,
        "lineno_loaded": loaded,
        "scope": "Literal lineno activation (\\linenumbers and its variants, the linenumbers "
        "package option or a lineno/linenumbers class option); class-specific review modes "
        "and macro-generated activation are not interpreted.",
    }
    if options.forbid_line_numbers:
        forbidden = Policy(_LINE_NUMBER_RULE, "No literal line-number activation was found.")
        failure = f"Line numbering is active at {plural(len(activations), 'location')}"
        outcome = CheckOutcome(activations, reasons)
        return [policy_finding(forbidden, outcome, failure=failure, details=details)]
    missing = (
        []
        if activations or reasons
        else [
            {
                "path": document.graph.main,
                "line": None,
                "reason": "no literal line-number activation",
            }
        ]
    )
    required = Policy(_LINE_NUMBER_RULE, "Line numbering is activated literally.")
    outcome = CheckOutcome(missing, [] if activations else reasons)
    failure = "No literal line-number activation was found"
    return [policy_finding(required, outcome, failure=failure, details=details)]
