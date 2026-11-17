r"""Metadata, ORCID identifiers and accessibility metadata (``MAN008``, ``MAN009``, ``MAN019``).

Title, author, affiliation, email and corresponding-author declarations are read from
the expanded document. ``read_literal_metadata`` is also the source of the values the
PDF metadata check compares with: it returns only declarations that are unique, literal
and not placeholders, with the reasons the rest stay unresolved. The policies claim
presence only, never per-author completeness or identity; an ORCID checksum shows that
an identifier is well formed, not that it belongs to the author. ``accessibility_metadata``
reads the ``\DocumentMetadata`` declaration that asks LaTeX for a tagged PDF with a
document language; it is not a test of the PDF's accessibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .analysed_source import AnalysedSource, reuse_or_analyse
from .expanded_document import ExpandedDocument
from .finding_text import plural
from .finding_weight import Weight
from .findings import EvidenceKind, Finding
from .literal_text import is_placeholder_text, key_value_items, plain_argument_text, plain_text
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_float_references import unread_text
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import METADATA_COMMANDS, ORCID_COMMANDS
from .tex_commands import COMMAND_DEFINITION_COMMANDS, TYPESET_ARGUMENT_COMMANDS
from .tex_lexer import Argument, Command
from .tex_spans import (
    ARGUMENT_CONDITIONALS,
    MacroDefinition,
    conditional_branches,
    defined_names,
    in_conditional,
    macro_definitions,
    opens_conditional,
)
from .uncertainty_reasons import CONDITIONAL_FLOW, context_reasons, grouped_reason

_REQUIRED_METADATA = Policy(
    "manuscript.required_metadata", "Checked configured literal metadata presence."
)
_ORCID = Policy("manuscript.orcid", "Checked literal ORCID syntax and checksums.")
# Missing accessibility metadata warns; a declaration that could not be read only informs.
_DOCUMENT_METADATA = Policy(
    "manuscript.document_metadata",
    "The document asks for a tagged PDF with a document language.",
    Weight("warning", "info", EvidenceKind.DERIVED),
)
# What a \DocumentMetadata declaration needs for an accessible PDF, as reported.
DOCUMENT_METADATA_COMMAND = "\\DocumentMetadata declaration"
LANGUAGE_KEY = "lang key"
TAGGING_KEY = "tagging=on or a PDF/UA pdfstandard"
_ORCID_IDENTIFIER = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", re.ASCII)
_ORCID_URL_PREFIX = re.compile(r"^https://orcid\.org/")
# An ORCID has 15 digits followed by one check character (ISO 7064 MOD 11-2).
_ORCID_PAYLOAD_DIGITS = 15
_ORCID_MODULUS = 11
_ORCID_X_VALUE = 10  # the check value that is written as the letter X
# Venue kits that declare the title and the authors with commands of their own, as ICML's
# \icmltitle{…} and \icmlauthor{Name}{affiliation key} and asmeconf's \SetAuthors{…} do;
# the first argument is the field.
_VENUE_METADATA_COMMANDS = {"title": {"icmltitle"}, "author": {"icmlauthor", "SetAuthors"}}


@dataclass(slots=True)
class MetadataValue:
    r"""One declaration of a metadata field and what is unclear about it.

    Attributes:
        path: The source file of the declaration, or ``None``.
        line: The line of the declaration, or ``None``.
        value: The plain text of the declaration's argument.
        uncertainty: Why the argument or the control flow around it is unclear.
        placeholder: Whether the value is placeholder text.
        start: The offset of the declaration in the expanded document.
        literal: Whether the declaration gives literal text that a command beside it
            cannot take away: its only unclear constructs are commands of the class or
            packages, such as llncs's ``\inst``, not macros the project defines. Its value
            may still be unclear for a comparison with the PDF's properties.
        present: Whether it is literal, not placeholder text, and runs where it stands.
    """

    path: str | None
    line: int | None
    value: str
    uncertainty: list[str]
    placeholder: bool
    start: int
    literal: bool = False
    present: bool = False

    def as_dict(self) -> dict[str, object]:
        """Render the declaration as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "value": self.value,
            "uncertainty": self.uncertainty,
            "placeholder": self.placeholder,
        }


def _metadata(document: ExpandedDocument) -> tuple[dict[str, list[MetadataValue]], list[str]]:
    """Read every declaration of each metadata field.

    A declaration is literal when text in it stands whatever the constructs beside it
    are (:class:`_StandingText`); a command the scan does not know leaves the value
    unclear for a comparison but does not take that text away.

    Args:
        document: The expanded document to read.

    Returns:
        The declarations of every field in ``METADATA_COMMANDS`` and its venue spellings,
        in source order, and the document's source-graph gaps.
    """
    values: dict[str, list[MetadataValue]] = {name: [] for name in METADATA_COMMANDS}
    declared = {
        command: name
        for name, commands in METADATA_COMMANDS.items()
        for command in commands | _VENUE_METADATA_COMMANDS.get(name, set())
    }
    standing = _StandingText(document)
    for command in document.commands:
        name = declared.get(command.name)
        if name is None or command.named:
            continue
        value, unknown = _argument_text(document, command)
        placeholder = is_placeholder_text(value)
        kept = standing.of(command)
        literal = bool(kept)
        context = context_reasons(command, document.conditionals, "metadata")
        unknown.extend(context)
        location = document.location(command.start)
        values[name].append(
            MetadataValue(
                location.path,
                location.line,
                value,
                unknown,
                placeholder,
                command.start,
                literal,
                literal and not placeholder and not is_placeholder_text(kept) and not context,
            )
        )
    # Metadata lives in the preamble, so only the declarations' own arguments and the
    # control flow around each declaration bear on this reading; the source-graph gaps
    # matter only where a field was not found.
    return values, list(document.graph_uncertainty)


# A word that stands for a macro parameter while the macro's replacement text is read.
_PARAMETER_WORD = "fledgemacroparameter"
# A replacement text that ends in a control word may read the groups after the use.
_ENDS_IN_CONTROL_WORD = re.compile(r"\\(?:[A-Za-z@]+|.)\s*\Z", re.S)
_CONDITIONAL_WORDS = frozenset({"else", "or", "fi"})
# A conditional with an \else has two branches.
_THEN_AND_ELSE = 2
# Commands of the classes that mark an author with a footnote, affiliation or
# corresponding-author key: elsarticle's \corref, \fnref and \tnoteref, llncs's \inst.
_AUTHOR_MARKS = frozenset({"corref", "fnref", "tnoteref", "inst"})
# Commands of packages and the kernel that typeset only some of their arguments, or none:
# conditionals with braced branches, such as etoolbox's \iftoggle{blind}{Anonymous}{Name},
# and the kernel's argument droppers.
_SELECTING_COMMANDS = ARGUMENT_CONDITIONALS | {
    "@gobble",
    "@gobbletwo",
    "@gobblefour",
    "@firstoftwo",
    "@secondoftwo",
    "phantom",
    "hphantom",
    "vphantom",
}


class _PrintedArguments(NamedTuple):
    """Which braced arguments of a use one definition of a macro is known to typeset.

    Attributes:
        parameters: How many braced arguments the definition reads.
        typed: The indexes, below ``parameters``, of the arguments its replacement text
            typesets whichever way its conditionals go.
        trailing: Whether groups after those arguments stay ordinary text: they do unless
            the replacement text ends in a control word that may read them.
    """

    parameters: int
    typed: frozenset[int]
    trailing: bool

    def prints(self, index: int) -> bool:
        """Return whether the argument at ``index`` of a use is typeset."""
        return index in self.typed if index < self.parameters else self.trailing


class _StandingText:
    r"""The literal text of a declaration that no construct in it can take away.

    A command of the class or a package, such as llncs's ``\inst`` or ``\orcidlink``, and
    a macro the project defines without arguments stand beside the text and leave it in
    place. A conditional may skip its text, so it is left out, and so are the arguments
    of a package conditional with braced branches (``\iftoggle``) and of a command that
    drops arguments (``\@gobble``); a stray ``\else`` or
    ``\fi`` and a project environment leave nothing certain. A project macro keeps the
    text of an argument only when every definition of it typesets that argument in each
    branch of its conditionals, as ``\newcommand\rev[1]{\ifshow{\color{blue}#1}\else#1\fi}``
    does; ``\newcommand\hide[1]{}`` drops it, and a macro whose definition Fledge cannot
    read may drop any argument. Inside such a definition a parameter counts as typeset
    only in a bare group or in the argument of a command that typesets its arguments
    (``TYPESET_ARGUMENT_COMMANDS``): what another command does with it is not known.

    Args:
        document: The expanded document.
    """

    def __init__(self, document: ExpandedDocument) -> None:
        self._document = document
        self._project = defined_names(document.commands)
        self._definitions: dict[str, list[MacroDefinition]] | None = None
        self._optional: set[int] = set()
        # ``None`` marks a macro whose arguments may all be dropped, also while it is read.
        self._printed: dict[str, list[_PrintedArguments] | None] = {}

    def of(self, command: Command) -> str:
        """Return the standing text of a declaration's first argument, whitespace collapsed.

        Text without a letter, such as a lone affiliation number, is no name or title, so
        it gives an empty result.
        """
        if not command.arguments:
            return ""
        argument = command.arguments[0]
        raw = self._blanked(argument.start, argument.end)
        if raw is None:
            return ""
        text, _ = plain_text(raw)
        return " ".join(text.split()) if any(char.isalpha() for char in text) else ""

    def _blanked(self, start: int, end: int) -> str | None:
        """Return the text between two offsets with its conditionals and dropped arguments blank.

        The identifier of an ORCID command and the key of an author mark are blank too:
        they are typeset, but they are no name.

        Args:
            start: The first offset.
            end: The offset just past the text.

        Returns:
            The text, as long as before, or ``None`` when nothing in it is certain.
        """
        document = self._document
        raw = list(document.text[start:end])
        blanked = [
            (begin, stop + len("\\fi"))
            for begin, stop in document.conditionals
            if start <= begin and stop + len("\\fi") <= end
        ]
        for begin, stop in blanked:
            raw[begin - start : stop - start] = " " * (stop - begin)
        for command in document.between(start, end):
            if any(begin <= command.start < stop for begin, stop in blanked):
                continue
            if self._uncertain(command):
                return None
            marks = command.arguments[:1] if _no_name(command) else []
            for argument in [*self._dropped(command), *marks]:
                stop = min(end, argument.end + 1)
                raw[argument.start - 1 - start : stop - start] = " " * (stop - argument.start + 1)
                blanked.append((argument.start - 1, stop))
        return "".join(raw)

    def _uncertain(self, command: Command) -> bool:
        """Return whether a command outside every closed conditional leaves nothing certain."""
        if command.named:
            return False
        # A stray branch word, or a command whose name is built where it stands.
        if opens_conditional(command) or command.name in _CONDITIONAL_WORDS | {"csname"}:
            return True
        return (
            command.name == "begin"
            and bool(command.arguments)
            and f"begin{{{command.arguments[0].value.strip()}}}" in self._project
        )

    def _dropped(self, command: Command, *, in_definition: bool = False) -> list[Argument]:
        """Return the arguments of a command that it may not typeset.

        Args:
            command: The command, in a declaration or in a replacement text.
            in_definition: Whether the command stands in the replacement text of a project
                macro, where only commands known to typeset their arguments keep them.

        Returns:
            The arguments that may be dropped.
        """
        if command.named:
            return []
        if command.name not in self._project:
            unknown = in_definition and command.name not in TYPESET_ARGUMENT_COMMANDS
            return list(command.arguments) if unknown or command.name in _SELECTING_COMMANDS else []
        definitions = self._printed_arguments(command.name)
        if definitions is None:
            return list(command.arguments)
        return [
            argument
            for index, argument in enumerate(command.arguments)
            if not all(definition.prints(index) for definition in definitions)
        ]

    def _printed_arguments(self, name: str) -> list[_PrintedArguments] | None:
        """Read which arguments each definition of a project macro typesets, once per name."""
        if name in self._printed:
            return self._printed[name]
        self._printed[name] = None
        if self._definitions is None:
            self._definitions, self._optional = _definitions_of(self._document)
        definitions = self._definitions.get(name)
        if not definitions:
            return None
        printed = [self._read_definition(definition) for definition in definitions]
        self._printed[name] = printed
        return printed

    def _read_definition(self, definition: MacroDefinition) -> _PrintedArguments:
        """Read which braced arguments one definition typesets."""
        # With an optional first argument, #1 is the option and the braced ones follow it.
        shift = 1 + int(definition.start in self._optional)
        typed = frozenset(
            index
            for index in range(definition.parameters)
            if self._always_typed(definition.start, definition.end, index + shift)
        )
        body = self._document.text[definition.start : definition.end]
        return _PrintedArguments(
            definition.parameters, typed, not _ENDS_IN_CONTROL_WORD.search(body)
        )

    def _always_typed(self, start: int, end: int, number: int) -> bool:
        """Return whether replacement text typesets parameter ``number`` whichever branch runs.

        Args:
            start: Where the replacement text, or a branch of it, starts.
            end: The offset just past it.
            number: The parameter number, as in ``#1``.

        Returns:
            ``True`` when the parameter is typeset outside every conditional, or in both
            branches of one; dropped by nothing the literal scan or a project macro drops.
        """
        document = self._document
        commands = document.between(start, end)
        raw = list(document.text[start:end])
        blanked: list[tuple[int, int]] = []
        for index, command in enumerate(commands):
            if command.named or any(begin <= command.start < stop for begin, stop in blanked):
                continue
            if opens_conditional(command):
                found = conditional_branches(commands, index)
                stop = end if found is None else min(end, found[1])
                branches = [] if found is None else found[0]
                if len(branches) == _THEN_AND_ELSE and all(
                    self._always_typed(begin, finish, number) for begin, finish in branches
                ):
                    return True
                raw[command.start - start : stop - start] = " " * (stop - command.start)
                blanked.append((command.start, stop))
                continue
            if self._uncertain(command):
                return False
            for argument in self._dropped(command, in_definition=True):
                stop = min(end, argument.end + 1)
                raw[argument.start - 1 - start : stop - start] = " " * (stop - argument.start + 1)
                blanked.append((argument.start - 1, stop))
        marked = re.sub(rf"(?<!#)#{number}(?!\d)", f" {_PARAMETER_WORD} ", "".join(raw))
        text, _ = plain_text(marked)
        return _PARAMETER_WORD in text.split()


def _no_name(command: Command) -> bool:
    r"""Return whether a command's first argument is typeset but names no one.

    An ORCID command's identifier, and the key of an author mark such as Elsevier's
    ``\corref{cor1}`` or llncs's ``\inst{1}``, stand beside a name; alone, they are no
    author.
    """
    return not command.named and (_orcid_command(command) or command.name in _AUTHOR_MARKS)


def _definitions_of(
    document: ExpandedDocument,
) -> tuple[dict[str, list[MacroDefinition]], set[int]]:
    r"""Return the definitions of each name and the bodies that take an optional argument.

    Args:
        document: The expanded document.

    Returns:
        The readable definitions by name, and the offsets where the replacement text of a
        definition with an optional first argument (``\newcommand\x[2][default]{…}``)
        starts.
    """
    commands = document.commands
    definitions: dict[str, list[MacroDefinition]] = {}
    for definition in macro_definitions(document.text, commands):
        definitions.setdefault(definition.name, []).append(definition)
    optional = set()
    for index, command in enumerate(commands[:-1]):
        named = commands[index + 1]
        if command.name not in COMMAND_DEFINITION_COMMANDS or named.named_by != command.name:
            continue
        if named.arguments:
            options, body = named.options, named.arguments[0]
        elif len(command.arguments) > 1:
            options, body = command.options, command.arguments[1]
        else:
            continue
        if len(options) > 1:
            optional.add(body.start)
    return definitions, optional


def _argument_text(document: ExpandedDocument, command: Command) -> tuple[str, list[str]]:
    r"""Return a declaration's literal text and the constructs that leave it unclear.

    A conditional inside the argument whose branches type no text, such as
    ``\title{Study\ifdefined\final\thanks{Funding}\fi}``, leaves the rest as the value;
    one that may type text keeps the value unclear.

    Args:
        document: The expanded document.
        command: The declaration.

    Returns:
        The text and its unresolved constructs, as ``plain_argument_text`` gives them.
    """
    value, unknown = plain_argument_text(command)
    if not unknown or not command.arguments:
        return value, unknown
    argument = command.arguments[0]
    raw = list(document.text[argument.start : argument.end])
    blanked = False
    for start, end in document.conditionals:
        if not argument.start <= start < end < argument.end:
            continue
        stop = end + len("\\fi")
        if not _types_nothing(document, start, stop):
            return value, unknown
        raw[start - argument.start : stop - argument.start] = " " * (stop - start)
        blanked = True
    if not blanked:
        return value, unknown
    outside, unclear = plain_text("".join(raw))
    if outside.strip() and not unclear:
        return " ".join(outside.split()), []
    return value, unknown


def _types_nothing(document: ExpandedDocument, start: int, stop: int) -> bool:
    r"""Return whether a conditional's text holds nothing but its control words and notes.

    Args:
        document: The expanded document.
        start: The offset of the conditional's opening ``\if…``.
        stop: The offset just after its ``\fi``.

    Returns:
        ``True`` when, without its control words and named operands, the conditional
        gives no literal text and nothing unclear, as a lone ``\thanks`` does.
    """
    raw = list(document.text[start:stop])
    for command in document.between(start, stop):
        if command.named or opens_conditional(command) or command.name in {"else", "or", "fi"}:
            end = command.name_end
            raw[command.start - start : end - start] = " " * (end - command.start)
    text, unclear = plain_text("".join(raw))
    return not text.strip() and not unclear


def read_literal_metadata(
    root: Path,
    main: str | None,
    fields: tuple[str, ...] | None = None,
    *,
    analysed: AnalysedSource | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return canonical, unambiguous literal values and any interpretation gaps.

    Only title/author/affiliation/email/correspondingauthor commands are supported.
    Multiple declarations may represent overwrites or repeated authors and are
    consequently not returned as a resolved PDF metadata value.

    Args:
        root: The project root to analyse.
        main: The main file as the caller named it; ``None`` selects the single document
            root.
        fields: The fields to read; when given, only their values and field-specific
            uncertainty are returned, while uncertainty about the source graph still
            applies to every selection. ``None`` reads every field.
        analysed: The source analysis of the same root and main file, shared with other
            checks.

    Returns:
        The value of each field with a single unclear-free, non-placeholder, non-empty
        declaration, and the sorted, distinct interpretation gaps.
    """
    metadata, reasons = _metadata(reuse_or_analyse(root, main, analysed).expanded)
    values: dict[str, str] = {}
    for key, declarations in metadata.items():
        if fields is not None and key not in fields:
            continue
        if len(declarations) > 1:
            reasons.append(f"multiple {key} declarations have unresolved combination semantics")
        for declaration in declarations:
            reasons.extend(declaration.uncertainty)
        if len(declarations) == 1:
            only = declarations[0]
            if not only.uncertainty and not only.placeholder and only.value:
                values[key] = only.value
    return values, sorted(set(reasons))


def _branch_alternatives(document: ExpandedDocument, items: list[MetadataValue]) -> bool:
    r"""Return whether two declarations are the two branches of one ``\if…\else…\fi``.

    Then one of them runs whichever branch is taken, as with
    ``\ifdefined\cameraready\author{…}\else\author{Anonymous}\fi``. The conditional
    must not lie inside another one, and each declaration must give literal text.

    Args:
        document: The expanded document.
        items: The declarations of one field.

    Returns:
        Whether the declarations are such alternatives.
    """
    grouped = grouped_reason("metadata")
    if len(items) != 2 or any(not item.literal or grouped in item.uncertainty for item in items):
        return False
    first, second = items[0].start, items[1].start
    commands = document.commands
    for index, command in enumerate(commands):
        if command.start >= first:
            break
        if not opens_conditional(command) or in_conditional(document.conditionals, command.start):
            continue
        found = conditional_branches(commands, index)
        if found is None or len(found[0]) != 2:
            continue
        (then_start, then_end), (else_start, else_end) = found[0]
        if (
            then_start <= first < then_end
            and else_start <= second < else_end
            and not _nested_opener(commands, then_start, first)
            and not _nested_opener(commands, else_start, second)
        ):
            return True
    return False


def _nested_opener(commands: list[Command], start: int, offset: int) -> bool:
    """Return whether another conditional opens between a branch's start and a declaration."""
    return any(
        start <= command.start < offset and opens_conditional(command) for command in commands
    )


def _required_metadata_finding(
    document: ExpandedDocument,
    metadata: dict[str, list[MetadataValue]],
    graph_gaps: list[str],
    required: tuple[str, ...],
) -> Finding:
    """Check that each required metadata field has a literal, non-placeholder value.

    A field declared in both branches of one conditional is declared whichever branch
    runs; when one branch gives placeholder text, such as an anonymous review author,
    the result stays open and says so. A part of the project that was not read could
    declare a field that was not found, but cannot take away one that was, so the
    source-graph gaps matter only while a required field is unresolved.

    Args:
        document: The expanded document to read.
        metadata: The declarations of each field, as :func:`_metadata` reads them.
        graph_gaps: The source-graph gaps of the document.
        required: The metadata fields that must be present.

    Returns:
        The ``manuscript.required_metadata`` finding, whose details list every metadata
        field.
    """
    reasons: list[str] = []
    resolved: set[str] = set()
    for name in required:
        items = metadata[name]
        last = items[-1] if items else None
        if last is not None and last.present:
            # A clear last declaration proves the field declared whether declarations
            # add up, as acmart, amsart and REVTeX take one \author per author, or the
            # last one replaces the others, as \title does.
            resolved.add(name)
            continue
        if _branch_alternatives(document, items):
            resolved.add(name)
            reasons.extend(
                f"one branch of the conditional around {name} gives placeholder text "
                f"{item.value!r} at {item.path}:{item.line}"
                for item in items
                if item.placeholder or not item.value
            )
            continue
        # Without a clear last declaration, every unclear one may be the missing field.
        reasons.extend(reason for item in items for reason in item.uncertainty)
        if len(items) > 1 and any(item.value and not item.placeholder for item in items):
            reasons.append(f"multiple {name} declarations have unresolved combination semantics")
    if not resolved.issuperset(required):
        reasons.extend(graph_gaps)
    # Absent metadata could be declared by any construct the scan could not
    # read, so only a fully interpreted preamble confirms that it is missing.
    missing = (
        [
            {"field": name, "reason": "missing, empty or placeholder literal metadata"}
            for name in required
            if not any(item.value and not item.placeholder for item in metadata[name])
        ]
        if not reasons
        else []
    )
    count = plural(len(required), "required metadata field")
    # The details list every metadata field, so an unclear optional field is
    # reported evidence too and keeps the result from being waived.
    other_gaps = any(
        item.uncertainty
        for name, items in metadata.items()
        if name not in resolved
        for item in items
    )
    return policy_finding(
        _REQUIRED_METADATA,
        CheckOutcome(missing, reasons, other_gaps),
        failure=f"{len(missing)} of {count} missing or empty",
        named=listing("Missing", (str(item["field"]) for item in missing)),
        details={
            "metadata": {
                name: [item.as_dict() for item in items] for name, items in metadata.items()
            },
            "required": list(required),
            "coverage": "Document-level presence; per-author completeness "
            "and identity are not established.",
        },
    )


def _orcid_checksum_valid(identifier: str) -> bool:
    """Check the ISO 7064 MOD 11-2 character of a syntactically valid ORCID identifier.

    Args:
        identifier: The identifier without URL prefix, in ``0000-0000-0000-000X`` form.

    Returns:
        ``True`` when the last character is the check character of the first 15 digits.
    """
    digits = identifier.replace("-", "")
    total = 0
    for digit in digits[:_ORCID_PAYLOAD_DIGITS]:
        total = (total + int(digit)) * 2
    check = (_ORCID_MODULUS + 1 - total % _ORCID_MODULUS) % _ORCID_MODULUS
    return digits[-1] == ("X" if check == _ORCID_X_VALUE else str(check))


def _orcid_command(command: Command) -> bool:
    r"""Return whether a command takes an ORCID iD: ``\orcid``, llncs's ``\orcidID`` and others.

    Any command whose name holds "orcid" counts, so a class or project macro that passes
    an identifier on is read too.
    """
    return command.name in ORCID_COMMANDS or "orcid" in command.name.casefold()


def _orcid_finding(document: ExpandedDocument, *, authors_found: bool) -> Finding:
    r"""Check the syntax and checksum of each literal ORCID identifier.

    Every command that takes an ORCID iD is read, in either branch of a conditional: a
    checksum is wrong whichever branch runs. An argument that is not literal, such as a
    macro or the ``#1`` of a definition, is an interpretation gap. Other commands do not
    hide an identifier from this check: one that passes it on carries it as literal text
    in a command whose name holds "orcid". An ``https://orcid.org/`` prefix is accepted.
    Identifiers sit with the authors, so once an author declaration was read, a construct
    that the source checks only suspect of hiding something is no gap. Text that was not
    read at all, such as a file that could not be included, may still hold an identifier.

    Args:
        document: The expanded document.
        authors_found: Whether a literal author declaration runs where it stands.

    Returns:
        The ``manuscript.orcid`` finding, failed for every invalid identifier.
    """
    identifiers = []
    invalid = []
    reasons = unread_text(document, [])[0] if authors_found else list(document.graph_uncertainty)
    for command in document.commands:
        if command.named or not _orcid_command(command) or not command.arguments:
            continue
        value = command.arguments[0].value.strip()
        if "\\" in value or "#" in value:
            reasons.append(f"ORCID in \\{command.name} is not a literal argument")
            continue
        identifier = _ORCID_URL_PREFIX.sub("", value)
        syntax = bool(_ORCID_IDENTIFIER.fullmatch(identifier))
        checksum = syntax and _orcid_checksum_valid(identifier)
        item = document.located(
            command.start, identifier=value, syntax_valid=syntax, checksum_valid=checksum
        )
        identifiers.append(item)
        if not syntax or not checksum:
            invalid.append(item)
    count = plural(len(identifiers), "literal ORCID identifier")
    return policy_finding(
        _ORCID,
        CheckOutcome(invalid, reasons),
        failure=f"{len(invalid)} of {count} invalid",
        details={
            "identifiers": identifiers,
            "identity": "Checksum validity does not establish ownership or identity.",
        },
    )


def metadata_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Run the required-metadata and ORCID policies that are switched on.

    Args:
        reading: The expanded document and its document-wide interpretation gaps.
        options: ``required_metadata`` names the required fields, and ``check_orcid``
            switches on the ORCID policy.

    Returns:
        The required-metadata finding and then the ORCID finding, each when switched on.
    """
    if not options.required_metadata and not options.check_orcid:
        return []
    document = reading.document
    metadata, graph_gaps = _metadata(document)
    results = []
    if options.required_metadata:
        results.append(
            _required_metadata_finding(document, metadata, graph_gaps, options.required_metadata)
        )
    if options.check_orcid:
        found = any(item.present for item in metadata["author"])
        results.append(_orcid_finding(document, authors_found=found))
    return results


@dataclass(frozen=True, slots=True)
class AccessibilityMetadata:
    r"""What the ``\DocumentMetadata`` declaration says about a tagged, language-tagged PDF.

    Attributes:
        location: The path and line of the declaration, or of the main file without a line
            when there is none.
        missing: What is missing, in the order ``DOCUMENT_METADATA_COMMAND``,
            ``LANGUAGE_KEY``, ``TAGGING_KEY``; empty when nothing is.
        uncertainty: Why the declaration could not be read; when there is any, ``missing``
            is empty.
        offset: Where the declaration is in the expanded text, or else the class
            declaration before which it belongs; ``None`` without either.
    """

    location: dict[str, object]
    missing: list[str]
    uncertainty: list[str]
    offset: int | None = None


def _asks_for_tagging(items: list[tuple[str, str | None]]) -> bool:
    """Return whether ``key=value`` items switch tagging on or name a PDF/UA standard."""
    for key, value in items:
        words = (value or "").casefold().replace(" ", "")
        if key == "tagging" and words == "on":
            return True
        if key == "pdfstandard" and any(item.startswith("ua") for item in words.split(",")):
            return True
    return False


def accessibility_metadata(document: ExpandedDocument) -> AccessibilityMetadata:
    r"""Read the ``\DocumentMetadata`` declaration before ``\documentclass``.

    LaTeX reads the declaration only before the class. It asks for a tagged PDF with
    ``tagging=on`` or a PDF/UA ``pdfstandard``, and ``lang`` names the document language.
    A declaration inside a group or conditional, one with a nonliteral argument, a
    conditional before the class, and a document without a literal class declaration
    leave the result unclear.

    Args:
        document: The expanded document.

    Returns:
        What the declaration lacks, or why it could not be read.
    """
    main = {"path": document.graph.main, "line": None}
    class_offset = next(
        (command.start for command in document.commands if command.name == "documentclass"),
        None,
    )
    if class_offset is None:
        # Without the class declaration the preamble cannot be told from the body.
        return AccessibilityMetadata(
            main, [], document.graph_uncertainty or ["no literal \\documentclass declaration"]
        )
    declaration = next(
        (
            command
            for command in document.commands
            if command.name == "DocumentMetadata" and command.start < class_offset
        ),
        None,
    )
    reasons: list[str] = []
    if declaration is None:
        if any(start < class_offset for start, _ in document.conditionals):
            reasons.append(f"{CONDITIONAL_FLOW} before \\documentclass")
        missing = [] if reasons else [DOCUMENT_METADATA_COMMAND]
        return AccessibilityMetadata(main, missing, reasons, class_offset)
    location = document.located(declaration.start)
    reasons.extend(context_reasons(declaration, document.conditionals, "DocumentMetadata"))
    value = declaration.arguments[0].value if declaration.arguments else ""
    if "\\" in value or not declaration.arguments:
        reasons.append("DocumentMetadata is not a literal argument")
    if reasons:
        return AccessibilityMetadata(location, [], reasons, declaration.start)
    items = key_value_items(value)
    missing = [
        *([] if any(key == "lang" and item for key, item in items) else [LANGUAGE_KEY]),
        *([] if _asks_for_tagging(items) else [TAGGING_KEY]),
    ]
    return AccessibilityMetadata(location, missing, [], declaration.start)


def document_metadata_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    r"""Require a ``\DocumentMetadata`` declaration for a tagged PDF with a language.

    Args:
        reading: The expanded document.
        options: ``require_document_metadata`` switches the policy on.

    Returns:
        One ``manuscript.document_metadata`` finding, or an empty list when the policy is
        off.
    """
    if not options.require_document_metadata:
        return []
    metadata = accessibility_metadata(reading.document)
    violations = [{**metadata.location, "missing": metadata.missing}] if metadata.missing else []
    return [
        policy_finding(
            _DOCUMENT_METADATA,
            CheckOutcome(violations, metadata.uncertainty),
            failure="The document does not ask for a tagged PDF with a language",
            named=listing("Missing", metadata.missing),
            details={
                "scope": "The literal \\DocumentMetadata declaration before \\documentclass; "
                "the PDF's tagging and accessibility are not established.",
            },
        )
    ]
