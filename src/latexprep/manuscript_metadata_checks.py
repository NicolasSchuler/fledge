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
from .literal_text import is_placeholder_text, key_value_items, plain_argument_text
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import METADATA_COMMANDS, ORCID_COMMANDS
from .tex_lexer import in_conditional
from .uncertainty_reasons import CONDITIONAL_FLOW, context_reasons

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
    """One declaration of a metadata field and what is unclear about it.

    Attributes:
        path: The source file of the declaration, or ``None``.
        line: The line of the declaration, or ``None``.
        value: The plain text of the declaration's argument.
        uncertainty: Why the argument or the control flow around it is unclear.
        placeholder: Whether the value is placeholder text.
    """

    path: str | None
    line: int | None
    value: str
    uncertainty: list[str]
    placeholder: bool

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
        document's presence uncertainty.
    """
    values: dict[str, list[MetadataValue]] = {name: [] for name in METADATA_COMMANDS}
    # Metadata lives in the preamble, so only the declarations' own arguments and
    # control flow that could add or remove a declaration bear on this reading.
    reasons = list(document.presence_uncertainty)
    for command in document.commands:
        for name, commands in METADATA_COMMANDS.items():
            if command.name not in commands:
                continue
            value, unknown = plain_argument_text(command)
            unknown.extend(context_reasons(command, document.conditionals, "metadata"))
            location = document.location(command.start)
            values[name].append(
                MetadataValue(
                    location.path, location.line, value, unknown, is_placeholder_text(value)
                )
            )
    return values, reasons


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


def _required_metadata_finding(document: ExpandedDocument, required: tuple[str, ...]) -> Finding:
    """Check that each required metadata field has a literal, non-placeholder value.

    Args:
        document: The expanded document to read.
        required: The metadata fields that must be present.

    Returns:
        The ``manuscript.required_metadata`` finding, whose details list every metadata
        field.
    """
    metadata, reasons = _metadata(document)
    for name in required:
        reasons.extend(reason for item in metadata[name] for reason in item.uncertainty)
        if len(metadata[name]) > 1:
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
    other_gaps = any(item.uncertainty for items in metadata.values() for item in items)
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


def _orcid_finding(reading: Reading) -> Finding:
    """Check the syntax and checksum of each literal ORCID identifier.

    An ORCID command without a literal argument, or inside a conditional, is an
    interpretation gap. An ``https://orcid.org/`` prefix is accepted.

    Args:
        reading: The expanded document and its document-wide interpretation gaps.

    Returns:
        The ``manuscript.orcid`` finding, failed for every invalid identifier.
    """
    document = reading.document
    identifiers = []
    invalid = []
    reasons = list(reading.uncertainty)
    for command in document.commands:
        if command.name not in ORCID_COMMANDS:
            continue
        value = command.arguments[0].value.strip() if command.arguments else ""
        if "\\" in value or not command.arguments:
            reasons.append("ORCID is not a literal argument")
            continue
        if in_conditional(document.conditionals, command.start):
            reasons.append(f"ORCID declared inside {CONDITIONAL_FLOW}")
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
