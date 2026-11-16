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

from .analysed_source import AnalysedSource, reuse_or_analyse
from .expanded_document import ExpandedDocument
from .finding_text import plural
from .finding_weight import Weight
from .findings import EvidenceKind, Finding
from .literal_text import is_placeholder_text, key_value_items, plain_argument_text, plain_text
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import METADATA_COMMANDS, ORCID_COMMANDS
from .tex_lexer import Command
from .tex_spans import conditional_branches, defined_names, in_conditional, opens_conditional
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

    Args:
        document: The expanded document to read.

    Returns:
        The declarations of every field in ``METADATA_COMMANDS``, in source order, and the
        document's source-graph gaps.
    """
    values: dict[str, list[MetadataValue]] = {name: [] for name in METADATA_COMMANDS}
    # Metadata lives in the preamble, so only the declarations' own arguments and the
    # control flow around each declaration bear on this reading.
    reasons = list(document.graph_uncertainty)
    project = defined_names(document.commands)
    for command in document.commands:
        for name, commands in METADATA_COMMANDS.items():
            if command.name not in commands:
                continue
            value, unknown = _argument_text(document, command)
            placeholder = is_placeholder_text(value)
            literal = bool(value) and all(
                item.startswith("\\") and item.removeprefix("\\") not in project for item in unknown
            )
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
                    literal and not placeholder and not context,
                )
            )
    return values, reasons


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


def _required_metadata_finding(document: ExpandedDocument, required: tuple[str, ...]) -> Finding:
    """Check that each required metadata field has a literal, non-placeholder value.

    A field declared in both branches of one conditional is declared whichever branch
    runs; when one branch gives placeholder text, such as an anonymous review author,
    the result stays open and says so.

    Args:
        document: The expanded document to read.
        required: The metadata fields that must be present.

    Returns:
        The ``manuscript.required_metadata`` finding, whose details list every metadata
        field.
    """
    metadata, reasons = _metadata(document)
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


def _orcid_finding(reading: Reading) -> Finding:
    """Check the syntax and checksum of each literal ORCID identifier.

    Every command that takes an ORCID iD is read, in either branch of a conditional: a
    checksum is wrong whichever branch runs. An argument that is not literal is an
    interpretation gap. Other commands do not hide an identifier from this check: one
    that passes it on carries it as literal text in a command whose name holds "orcid".
    An ``https://orcid.org/`` prefix is accepted.

    Args:
        reading: The expanded document and its document-wide interpretation gaps.

    Returns:
        The ``manuscript.orcid`` finding, failed for every invalid identifier.
    """
    document = reading.document
    identifiers = []
    invalid = []
    reasons = list(document.graph_uncertainty)
    for command in document.commands:
        if command.named or not _orcid_command(command) or not command.arguments:
            continue
        value = command.arguments[0].value.strip()
        if "\\" in value:
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
    results = []
    if options.required_metadata:
        results.append(_required_metadata_finding(reading.document, options.required_metadata))
    if options.check_orcid:
        results.append(_orcid_finding(reading))
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
