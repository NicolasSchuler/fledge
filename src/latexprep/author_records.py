"""Explicit, user-configured author-record contracts in the literal preamble.

The user supplies the record-opening command and its required associated commands.
This does not interpret arbitrary author templates, shared author lists or
affiliation indices. The module owns those two options, ``AuthorRecordOptions``, which a
settings file writes in the ``[structure_checks]`` table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import cancellation_point
from .errors import SettingsError
from .expanded_document import ExpandedDocument
from .finding_text import check_status, location_label, more_suffix
from .finding_weight import POLICY
from .findings import Finding
from .literal_text import is_placeholder_text, plain_argument_text
from .option_config import TextFormat, require_text, require_unique_strings
from .tex_commands import COMMAND_DEFINITION_COMMANDS, PRIMITIVE_DEFINITION_COMMANDS
from .tex_lexer import Command, command_end, in_conditional

_RULE = "manuscript.author_records"
_MAX_REPORTED_RECORDS = 100
# Records and fields named in the message; the details list up to _MAX_REPORTED_RECORDS.
_MAX_NAMED_PROBLEMS = 3
_DYNAMIC_COMMANDS = PRIMITIVE_DEFINITION_COMMANDS | {"csname", "catcode"}
# Associated author-block commands, such as affiliation or email, that must follow each author.
_MAX_AUTHOR_FIELDS = 32
_COMMAND = re.compile(r"[A-Za-z][A-Za-z@]{0,63}")


@dataclass(frozen=True)
class AuthorRecordOptions:
    """The command that opens an author record and the commands each record must contain.

    Construction raises ``SettingsError`` when a command name is malformed, a field is
    repeated or there are too many, or the fields list the author command itself.

    Attributes:
        author_command: The record-opening command, without the backslash.
        required_author_fields: The associated commands, without the backslash, that each
            record must contain; an empty tuple disables the check.
    """

    author_command: str = "author"
    required_author_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(
            "author_command",
            self.author_command,
            expected="a command name without the backslash",
            pattern=_COMMAND,
        )
        require_unique_strings(
            "required_author_fields",
            self.required_author_fields,
            max_items=_MAX_AUTHOR_FIELDS,
            item_format=TextFormat(_COMMAND, "a command name without the backslash"),
        )
        if self.author_command in self.required_author_fields:
            raise SettingsError(
                "required_author_fields must not list the author_command itself",
                ("required_author_fields", "author_command"),
            )


def _problem(item: dict[str, object]) -> str:
    """Name where one confirmed violation is and what is wrong there.

    Args:
        item: A violation from the scan: a record with ``author_index``, ``empty_value``
            and ``missing_fields``, or a field placed before the first record.

    Returns:
        The location and the problems, for the failure message.
    """
    where = location_label(item)
    if "author_index" not in item:
        return f"\\{item['field']} at {where}"
    problems = []
    if item["empty_value"]:
        problems.append("empty or placeholder name")
    missing = item["missing_fields"]
    if isinstance(missing, list) and missing:
        problems.append("missing " + ", ".join(f"\\{name}" for name in missing))
    return f"{where} ({'; '.join(problems)})"


class _Step(StrEnum):
    """What the scan does with one command of the preamble.

    Attributes:
        DEFINITION: Skip the command and the definition it introduces.
        STOP: End the scan.
        READ: Read the command as a possible record opener or associated field.
    """

    DEFINITION = "definition"  # the command being defined is not an author record
    STOP = "stop"  # the title or the body starts: no record can follow
    READ = "read"


def _classify(command: Command) -> _Step:
    """Decide what the scan does with ``command``."""
    if command.name in COMMAND_DEFINITION_COMMANDS:
        return _Step.DEFINITION
    if not command.depth and (command.name == "maketitle" or command.begins("document")):
        return _Step.STOP
    return _Step.READ


def _syntax_problem(document: ExpandedDocument, command: Command, where: str) -> str | None:
    """Explain why ``command`` cannot be read as a plain one-argument record.

    Args:
        document: The expanded document whose conditionals enclose the command or not.
        command: An author-record opener or associated field.
        where: The command's location label, which starts the reason.

    Returns:
        The reason, or ``None`` when the command is a plain one-argument record.
    """
    # A \if… token only matters where it encloses a record: a \newif-defined
    # name or a branch-taking package conditional executes nothing by itself.
    if in_conditional(document.conditionals, command.start):
        return f"{where}: author record inside conditional control flow"
    if command.depth or command.options or len(command.arguments) != 1:
        return f"{where}: grouped, optional or ambiguous author-record syntax"
    return None


@dataclass
class _AuthorScan:
    """Author records in preamble order: each opener starts a record, fields join the open one.

    Attributes:
        author_command: The record-opening command.
        required_fields: The associated commands each record must contain.
        uncertainty: The reasons the scan cannot interpret the preamble with certainty.
        records: The records found, each with its index, location and, once closed, its
            present and missing fields.
        violations: The records and stray fields that break the contract.
        empty_names: The indexes of the records whose author name is empty or a
            placeholder.
        fields: How many times each required field was given for the open record.
    """

    author_command: str
    required_fields: tuple[str, ...]
    uncertainty: list[str]
    records: list[dict[str, object]] = field(default_factory=list)
    violations: list[dict[str, object]] = field(default_factory=list)
    empty_names: set[object] = field(default_factory=set)
    fields: dict[str, int] = field(default_factory=dict)

    def read(self, document: ExpandedDocument) -> None:
        """Read the preamble up to the title or the document body."""
        ignored_until = -1
        for command in document.commands:
            cancellation_point()
            if command.start < ignored_until:
                continue
            step = _classify(command)
            if step is _Step.DEFINITION:
                ignored_until = command_end(command)
            elif step is _Step.STOP:
                break
            else:
                self._read_command(document, command)
        self._close_record()
        if not self.records:
            self.violations.append({"reason": "no supported literal author records"})

    def _read_command(self, document: ExpandedDocument, command: Command) -> None:
        """Open a record, count a field for the open record, or note what cannot be read.

        Args:
            document: The expanded document that locates the command.
            command: One preamble command that is not a definition.
        """
        if command.name in _DYNAMIC_COMMANDS:
            self.uncertainty.append("dynamically redefined preamble commands")
        if command.name != self.author_command and command.name not in self.required_fields:
            return
        location = document.location(command.start).as_dict()
        problem = _syntax_problem(document, command, location_label(location))
        if problem is not None:
            self.uncertainty.append(problem)
            return
        value, reasons = plain_argument_text(command)
        self.uncertainty.extend(reasons)
        nonempty = bool(value and not is_placeholder_text(value))
        if command.name == self.author_command:
            self._close_record()
            self.fields = {}
            self.records.append({"author_index": len(self.records) + 1, **location})
            if not nonempty and not reasons:
                self.empty_names.add(self.records[-1]["author_index"])
        elif not self.records:
            self.violations.append(
                {**location, "field": command.name, "reason": "field before author"}
            )
        elif nonempty and not reasons:
            self.fields[command.name] = self.fields.get(command.name, 0) + 1

    def _close_record(self) -> None:
        """Note which required fields the open record lacks; one violation covers its problems."""
        if not self.records:
            return
        record = self.records[-1]
        missing = [name for name in self.required_fields if not self.fields.get(name)]
        record["present_fields"] = sorted(name for name, count in self.fields.items() if count)
        record["missing_fields"] = missing
        empty = record["author_index"] in self.empty_names
        problems = []
        if empty:
            problems.append("empty or placeholder author value")
        if missing:
            problems.append("missing or empty associated author fields")
        if problems:
            self.violations.append({**record, "empty_value": empty, "reason": "; ".join(problems)})


def _failure_message(violations: list[dict[str, object]], records: int, author_command: str) -> str:
    """Describe the failed records out of all records, each with its missing fields.

    Args:
        violations: The confirmed violations.
        records: How many records the scan found.
        author_command: The record-opening command, for the message.

    Returns:
        The message, which explains what to do when no record was found.
    """
    if not records:
        return (
            f"No literal \\{author_command} author record was found in the preamble, so the "
            "required author fields cannot be checked. Declare each author with "
            f"\\{author_command}{{...}} before \\maketitle, or set "
            "structure_checks.author_command to the command your template uses."
        )
    incomplete = [item for item in violations if "author_index" in item]
    stray = [item for item in violations if "author_index" not in item]
    sections = []
    for label, items in (
        (f"Incomplete author records ({len(incomplete)} of {records})", incomplete),
        (f"Fields placed before the first \\{author_command}", stray),
    ):
        if items:
            named = ", ".join(_problem(item) for item in items[:_MAX_NAMED_PROBLEMS])
            sections.append(f"{label}: {named}{more_suffix(len(items) - _MAX_NAMED_PROBLEMS)}")
    return "; ".join(sections) + "."


def _author_finding(scan: _AuthorScan, main: str | None) -> Finding:
    """Build the finding for a scanned preamble.

    Violations are withheld while anything is unclear.

    Args:
        scan: The finished scan.
        main: The main file the finding names.

    Returns:
        The ``manuscript.author_records`` finding.
    """
    reasons = sorted(set(scan.uncertainty))
    # Any interpretation gap withholds every violation, unlike the manuscript policies.
    confirmed = scan.violations if not reasons else []
    message = "Checked each configured sequential literal author record."
    if confirmed:
        message = _failure_message(confirmed, len(scan.records), scan.author_command)
    elif reasons:
        message += " Inconclusive: " + reasons[0].rstrip(".") + "."
    incomplete = bool(scan.uncertainty)
    status = check_status(failed=bool(confirmed), incomplete=incomplete)
    return Finding(
        _RULE,
        message,
        severity=POLICY.severity(status),
        status=status,
        path=main,
        evidence_kind=POLICY.evidence_kind,
        details={
            "records": scan.records[:_MAX_REPORTED_RECORDS],
            "observed_violations": scan.violations[:_MAX_REPORTED_RECORDS],
            "uncertainty": reasons,
            "author_command": scan.author_command,
            "required_fields": list(scan.required_fields),
            "scope": "User-selected preamble record grammar; no template, identity or "
            "shared-field inference.",
            "violations": confirmed[:_MAX_REPORTED_RECORDS],
            "violation_count": len(confirmed),
            "incomplete": incomplete,
        },
    )


def check_author_records(
    root: Path,
    main: str | None,
    options: AuthorRecordOptions,
    *,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Check sequential top-level literal records before begin{document}/maketitle.

    Each opener starts a new author; fields apply only until the next opener.

    Args:
        root: The project root.
        main: The main file, or ``None`` for the project's single document root.
        options: The record-opening command and the required fields.
        analysed: The source analysis of the same root and main file shared with other
            checks; without one the document is expanded here.

    Returns:
        One finding, or none when no required author fields are configured.
    """
    if not options.required_author_fields:
        return []
    document = reuse_or_analyse(root, main, analysed).expanded
    scan = _AuthorScan(
        options.author_command, options.required_author_fields, list(document.graph_uncertainty)
    )
    scan.read(document)
    return [_author_finding(scan, main)]
