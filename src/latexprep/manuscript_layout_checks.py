"""Layout override candidates (``MAN002``) and required or forbidden line numbers (``MAN017``).

Both policies read how the document sets up its page: spacing, page breaks, type size
and geometry overrides that a venue may forbid, and the ``lineno`` activation that
review copies need and final versions must not have. Layout candidates include the
usual ways of squeezing a paper under a page limit: smaller margins through geometry
options or the fullpage and savetrees packages, small caption skips and fonts, compact
headings, zero display skips and float fractions that let floats fill a page. They are
advisory and never infer intent; line numbering recognizes only literal ``lineno``
activation, not class-specific review modes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .cancellation import cancellation_point
from .expanded_document import ExpandedDocument
from .finding_text import list_locations, plural
from .finding_weight import ADVISORY
from .findings import Finding
from .literal_text import PACKAGE_COMMANDS, first_argument_names, key_value_items, literal_list
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, policy_finding
from .manuscript_vocabulary import (
    CAPTION_FONT_KEYS,
    CAPTION_SKIP_KEYS,
    COMPACT_HEADING_OPTIONS,
    DISPLAY_SKIP_LENGTHS,
    FLOAT_ENVIRONMENTS,
    FLOAT_FRACTIONS,
    GEOMETRY_AREA_KEYS,
    LAYOUT_COMMANDS,
    LAYOUT_PACKAGES,
    LENGTH_COMMANDS,
    SMALL_TYPE_SIZES,
)
from .tex_commands import PRIMITIVE_DEFINITION_COMMANDS
from .tex_lexer import (
    CONTROL_SEQUENCE,
    Command,
    command_end,
    definition_spans,
    in_conditional,
    in_half_open_span,
    skip_space,
)

_LAYOUT = Policy("manuscript.layout_manipulation", "Checked layout override candidates.", ADVISORY)
_LINE_NUMBER_RULE = "manuscript.line_numbers"
_LINE_NUMBER_COMMANDS = {"linenumbers", "runninglinenumbers", "pagewiselinenumbers"}
_LINE_NUMBER_OPTIONS = {"lineno", "linenumbers"}
# Source characters read after a spacing or length command to see whether it sets a value.
_VALUE_LOOKAHEAD_CHARS = 80
# Source characters read before a length for the \setlength or \addtolength that sets it.
_LENGTH_COMMAND_LOOKBEHIND_CHARS = 32
# \setlength or \addtolength, then only white space up to the end of the text read.
_LENGTH_COMMAND_BEFORE = re.compile(r"\\(setlength|addtolength)\s*\Z")
# Source characters of a layout command quoted in the details.
_SNIPPET_CHARS = 160
# A caption skip below this many points squeezes the caption against its float; the
# caption package's default is 10pt.
_SMALL_SKIP_POINTS = 3.0
# TeX units in points; em and ex assume a 10pt font, the size of ordinary body text.
_POINTS_PER_UNIT = {
    "pt": 1.0,
    "bp": 72.27 / 72,
    "mm": 72.27 / 25.4,
    "cm": 72.27 / 2.54,
    "in": 72.27,
    "pc": 12.0,
    "dd": 1238 / 1157,
    "cc": 12 * 1238 / 1157,
    "sp": 1 / 65536,
    "em": 10.0,
    "ex": 4.3,
}
# A signed decimal number at the start of a TeX length, with its unit when there is one.
_LEADING_LENGTH = re.compile(r"\s*([-+]?)\s*(\d+(?:\.\d*)?|\.\d+)?\s*([a-z]{2})?")
# TeX's internal names for a zero length, at the start of a value.
_ZERO_LENGTH = re.compile(r"\s*\\z@(?:skip)?(?![A-Za-z@])")
# The optional equals sign of a direct length assignment such as ``\textwidth=5in``.
_ASSIGNMENT = re.compile(r"\s*(?:=\s*)?")
# Commands that redefine a macro, whose name follows them braced or bare.
_REDEFINITIONS = frozenset({"renewcommand", *PRIMITIVE_DEFINITION_COMMANDS})


def _leading_points(value: str) -> tuple[bool, float | None]:
    r"""Read the sign and size of the length a TeX value starts with.

    Args:
        value: The value as written, such as ``-2pt``, ``0pt plus 1pt`` or ``\z@``.

    Returns:
        Whether the value is negative, and its natural size in points: zero for TeX's zero
        lengths, ``None`` when it starts with no number or with an unknown unit.
    """
    if _ZERO_LENGTH.match(value):
        return False, 0.0
    match = _LEADING_LENGTH.match(value)
    negative = bool(match and match.group(1) == "-")
    if match is None or match.group(2) is None:
        return negative, None
    number = float(match.group(2))
    if number == 0:
        return negative, 0.0
    unit = _POINTS_PER_UNIT.get(match.group(3) or "")
    return negative, number * unit if unit is not None else None


def _zero_or_negative(value: str) -> bool:
    """Return whether a TeX length value is zero or negative."""
    negative, points = _leading_points(value)
    return negative or points == 0


def _small_length(value: str) -> bool:
    """Return whether a TeX length value is negative or below ``_SMALL_SKIP_POINTS``."""
    negative, points = _leading_points(value)
    return negative or (points is not None and points < _SMALL_SKIP_POINTS)


def _small_type_size(value: str) -> bool:
    """Return whether a caption font value names the footnote size or a smaller one."""
    return any(word in SMALL_TYPE_SIZES for word in re.split(r"[\s,{}]+", value))


def _squeezes_captions(items: Iterable[tuple[str, str | None]]) -> bool:
    r"""Return whether caption keys set a small skip or a small font.

    Args:
        items: The ``key=value`` items of a caption package option list or of
            ``\captionsetup``.

    Returns:
        ``True`` for a skip key with a small value or a font key with a small type size.
    """
    return any(
        value is not None
        and (
            (key in CAPTION_SKIP_KEYS and _small_length(value))
            or (key in CAPTION_FONT_KEYS and _small_type_size(value))
        )
        for key, value in items
    )


def _negative_spacing(document: ExpandedDocument, command: Command) -> bool:
    """Return whether a spacing command moves backwards; an unbraced value is read too.

    Args:
        document: The expanded document, read after the command for an unbraced value.
        command: The spacing command.

    Returns:
        ``True`` when its value starts with a minus sign.
    """
    value = (
        command.arguments[0].value
        if command.arguments
        else document.text[command.name_end : command.name_end + _VALUE_LOOKAHEAD_CHARS]
    )
    return bool(re.match(r"\s*-", value))


def _sets_layout_length(name: str, value: str, *, added: bool = False) -> bool:
    """Return whether giving the length ``name`` the value ``value`` overrides the layout.

    Args:
        name: The length, without its backslash.
        value: The value, or the amount added to the length.
        added: Whether ``value`` is added to the length instead of replacing it.

    Returns:
        ``True`` for any value of a page-geometry length, and for a display skip set to
        zero or less, or reduced.
    """
    if name in LENGTH_COMMANDS:
        return True
    if name not in DISPLAY_SKIP_LENGTHS:
        return False
    return _leading_points(value)[0] if added else _zero_or_negative(value)


def _length_setting(_document: ExpandedDocument, command: Command) -> bool:
    r"""Return whether ``\setlength`` or ``\addtolength`` with a braced length overrides the layout.

    Args:
        _document: The expanded document, unused.
        command: The ``\setlength`` or ``\addtolength`` command.

    Returns:
        ``True`` as :func:`_sets_layout_length` decides it.
    """
    if not command.arguments:
        return False
    target = command.arguments[0].value.strip().lstrip("\\")
    value = command.arguments[1].value if len(command.arguments) > 1 else ""
    return _sets_layout_length(target, value, added=command.name == "addtolength")


def _length_command_before(document: ExpandedDocument, command: Command) -> str | None:
    r"""Name the ``\setlength`` or ``\addtolength`` written right before a length, if any.

    Only a few characters before the length are read, so many lengths stay linear.

    Args:
        document: The expanded document.
        command: The length command.

    Returns:
        ``setlength`` or ``addtolength`` when the length is its unbraced first argument, as
        in ``\setlength\textwidth{5in}``; ``None`` otherwise.
    """
    before = document.text[max(0, command.start - _LENGTH_COMMAND_LOOKBEHIND_CHARS) : command.start]
    match = _LENGTH_COMMAND_BEFORE.search(before)
    return match.group(1) if match else None


def _length_assignment(document: ExpandedDocument, command: Command) -> bool:
    r"""Return whether a length command is given a value: ``\textwidth=5in``.

    A braced group right after the length is its value only when ``\setlength`` or
    ``\addtolength`` stands before the length, as in ``\setlength\textwidth{5in}``; a
    group after a length used as a value, as in ``\vbox to\textheight{...}``, is not.
    Without a group the source after the command is read. A page-geometry length counts
    with a numeric value, a display skip only with zero or less.

    Args:
        document: The expanded document, read around the command for the command that sets
            it and for an unbraced value.
        command: A page-geometry length or a display skip.

    Returns:
        ``True`` for a layout length override.
    """
    if command.arguments:
        setter = _length_command_before(document, command)
        return setter is not None and _sets_layout_length(
            command.name, command.arguments[0].value, added=setter == "addtolength"
        )
    end = command_end(command)
    following = document.text[end : end + _VALUE_LOOKAHEAD_CHARS]
    assignment = _ASSIGNMENT.match(following)
    value = following[assignment.end() :] if assignment else following
    if command.name in LENGTH_COMMANDS:
        return bool(re.match(r"[-+]?\d", value))
    return _zero_or_negative(value)


def _forced_float(_document: ExpandedDocument, command: Command) -> bool:
    r"""Return whether ``\begin`` opens a float placed with ``H`` or ``!``."""
    return bool(command.arguments) and (
        command.arguments[0].value in FLOAT_ENVIRONMENTS
        and any("H" in argument.value or "!" in argument.value for argument in command.options)
    )


def _squeezing_package(_document: ExpandedDocument, command: Command) -> bool:
    """Return whether a package declaration shrinks margins, captions or heading space.

    Args:
        _document: The expanded document, unused.
        command: A package declaration.

    Returns:
        ``True`` for fullpage or savetrees, geometry with margin or text-area options,
        caption with a small skip or font, and titlesec with ``compact``.
    """
    names = set(first_argument_names(command) or ())
    items = [item for option in command.options for item in key_value_items(option.value)]
    keys = {key for key, _ in items}
    return bool(
        names & LAYOUT_PACKAGES
        or ("geometry" in names and keys & GEOMETRY_AREA_KEYS)
        or ("caption" in names and _squeezes_captions(items))
        or ("titlesec" in names and keys & COMPACT_HEADING_OPTIONS)
    )


def _squeezing_caption_setup(_document: ExpandedDocument, command: Command) -> bool:
    r"""Return whether ``\captionsetup`` sets a small caption skip or font."""
    return bool(command.arguments) and _squeezes_captions(
        key_value_items(command.arguments[0].value)
    )


def _float_fraction_definition(document: ExpandedDocument, command: Command) -> bool:
    r"""Return whether a redefinition changes a float placement parameter such as ``\topfraction``.

    Args:
        document: The expanded document, read after the command for a bare macro name.
        command: A redefinition command.

    Returns:
        ``True`` when the redefined macro, braced or bare, is in ``FLOAT_FRACTIONS``.
    """
    if command.arguments:
        defined = command.arguments[0].value.strip().lstrip("\\")
    else:
        token = CONTROL_SEQUENCE.match(document.text, skip_space(document.text, command.name_end))
        defined = token.group(1) if token else ""
    return defined in FLOAT_FRACTIONS


# How each command that may be a candidate is judged; any other command is one when it is
# a length given a value or a command in ``LAYOUT_COMMANDS``.
_OVERRIDE_TESTS: dict[str, Callable[[ExpandedDocument, Command], bool]] = {
    **dict.fromkeys(("hspace", "vspace", "hskip", "vskip", "kern"), _negative_spacing),
    **dict.fromkeys(("setlength", "addtolength"), _length_setting),
    **dict.fromkeys(PACKAGE_COMMANDS, _squeezing_package),
    **dict.fromkeys(_REDEFINITIONS, _float_fraction_definition),
    "begin": _forced_float,
    "captionsetup": _squeezing_caption_setup,
}


def _is_layout_override(document: ExpandedDocument, command: Command) -> bool:
    """Return whether a command is a layout override candidate.

    Candidates are negative spacing, a change or assignment of a layout length, a zero
    or negative display skip, a float placed with ``H`` or ``!``, a package or caption
    setting that squeezes margins, captions or headings, a redefined float fraction, and
    every command in ``LAYOUT_COMMANDS``.

    Args:
        document: The expanded document, read after the command for an unbraced value.
        command: The command to classify.

    Returns:
        ``True`` when the command overrides spacing, page breaks, type size or geometry.
    """
    test = _OVERRIDE_TESTS.get(command.name)
    if test is not None:
        return test(document, command)
    if command.name in LENGTH_COMMANDS or command.name in DISPLAY_SKIP_LENGTHS:
        return _length_assignment(document, command)
    return command.name in LAYOUT_COMMANDS


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
    overrides = []
    for command in document.commands:
        cancellation_point()
        if not _is_layout_override(document, command) or in_conditional(
            document.conditionals, command.start
        ):
            continue
        snippet_end = min(command_end(command), command.start + _SNIPPET_CHARS)
        overrides.append(
            document.located(
                command.start,
                command=command.name,
                source=document.text[command.start : snippet_end],
            )
        )
    return overrides


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
