"""Required literal metadata and ORCID identifiers (``MAN008``, ``MAN009``), and the reader.

Title, author, affiliation, email and corresponding-author declarations are read from
the expanded document. ``read_literal_metadata`` is also the source of the values the
PDF metadata check compares with: it returns only declarations that are unique, literal
and not placeholders, with the reasons the rest stay unresolved. The policies claim
presence only, never per-author completeness or identity; an ORCID checksum shows that
an identifier is well formed, not that it belongs to the author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .expanded_document import ExpandedDocument, expand_document
from .finding_text import plural
from .findings import Finding
from .literal_text import is_placeholder_text, plain_argument_text
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import METADATA_COMMANDS, ORCID_COMMANDS
from .tex_lexer import in_conditional
from .uncertainty_reasons import CONDITIONAL_FLOW, context_reasons

_REQUIRED_METADATA = Policy(
    "manuscript.required_metadata", "Checked configured literal metadata presence."
)
_ORCID = Policy("manuscript.orcid", "Checked literal ORCID syntax and checksums.")
_ORCID_IDENTIFIER = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", re.ASCII)
_ORCID_URL_PREFIX = re.compile(r"^https://orcid\.org/")
# An ORCID has 15 digits followed by one check character (ISO 7064 MOD 11-2).
_ORCID_PAYLOAD_DIGITS = 15
_ORCID_MODULUS = 11
_ORCID_X_VALUE = 10  # the check value that is written as the letter X


@dataclass(slots=True)
class MetadataValue:
    """One declaration of a metadata field and what is unclear about it."""

    path: str | None
    line: int | None
    value: str
    uncertainty: list[str]
    placeholder: bool

    def as_dict(self) -> dict[str, object]:
        """The declaration as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "value": self.value,
            "uncertainty": self.uncertainty,
            "placeholder": self.placeholder,
        }


def _metadata(document: ExpandedDocument) -> tuple[dict[str, list[MetadataValue]], list[str]]:
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
    root: Path, main: str | None, fields: tuple[str, ...] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Return canonical, unambiguous literal values and any interpretation gaps.

    Only title/author/affiliation/email/correspondingauthor commands are supported.
    Multiple declarations may represent overwrites or repeated authors and are
    consequently not returned as a resolved PDF metadata value. When fields are
    selected, only their values and field-specific uncertainty are returned;
    uncertainty about the source graph still applies to every selection.
    """
    metadata, reasons = _metadata(expand_document(root, main))
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
    """Check the ISO 7064 MOD 11-2 character of a syntactically valid ORCID identifier."""
    digits = identifier.replace("-", "")
    total = 0
    for digit in digits[:_ORCID_PAYLOAD_DIGITS]:
        total = (total + int(digit)) * 2
    check = (_ORCID_MODULUS + 1 - total % _ORCID_MODULUS) % _ORCID_MODULUS
    return digits[-1] == ("X" if check == _ORCID_X_VALUE else str(check))


def _orcid_finding(reading: Reading) -> Finding:
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
    """The required-metadata and ORCID policies that are switched on."""
    results = []
    if options.required_metadata:
        results.append(_required_metadata_finding(reading.document, options.required_metadata))
    if options.check_orcid:
        results.append(_orcid_finding(reading))
    return results
