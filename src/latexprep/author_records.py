"""Explicit, user-configured author-record contracts in the literal preamble.

The user supplies the record-opening command and its required associated commands.
This does not interpret arbitrary author templates, shared author lists or
affiliation indices.
"""

from __future__ import annotations

from pathlib import Path

from .expanded_document import command_end, expand_document
from .file_scan import check_status, location_label, more_suffix
from .literal_text import is_placeholder_text, plain_argument_text
from .models import EvidenceKind, Finding
from .pdf_structure_checks import StructureOptions
from .scheduler import cancellation_point
from .tex_lexer import in_conditional

_RULE = "manuscript.author_records"
_MAX_REPORTED_RECORDS = 100
# Records and fields named in the message; the details list up to _MAX_REPORTED_RECORDS.
_MAX_NAMED_PROBLEMS = 3
_DECLARATION_COMMANDS = {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}
_DYNAMIC_COMMANDS = {"def", "gdef", "xdef", "edef", "csname", "catcode", "let"}


def _problem(item: dict[str, object]) -> str:
    """Name where one confirmed violation is and what is wrong there."""
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


def _close_record(
    record: dict[str, object],
    fields: dict[str, int],
    empty_values: set[object],
    required: tuple[str, ...],
    violations: list[dict[str, object]],
) -> None:
    """Note which required fields one author lacks; a single violation covers all its problems."""
    missing = [name for name in required if not fields.get(name)]
    record["present_fields"] = sorted(name for name, count in fields.items() if count)
    record["missing_fields"] = missing
    empty = record["author_index"] in empty_values
    problems = []
    if empty:
        problems.append("empty or placeholder author value")
    if missing:
        problems.append("missing or empty associated author fields")
    if problems:
        violations.append({**record, "empty_value": empty, "reason": "; ".join(problems)})


def _failure_message(violations: list[dict[str, object]], records: int, author_command: str) -> str:
    """The failed records out of all records, each with its missing fields."""
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


def check_author_records(root: Path, main: str | None, options: StructureOptions) -> list[Finding]:
    """Check sequential top-level literal records before begin{document}/maketitle.

    Each opener starts a new author; fields apply only until the next opener.
    """
    if not options.required_author_fields:
        return []
    document = expand_document(root, main)
    uncertainty = list(document.graph_uncertainty)
    records: list[dict[str, object]] = []
    fields: dict[str, int] = {}
    violations: list[dict[str, object]] = []
    empty_values: set[object] = set()
    ignored_until = -1
    configured = {options.author_command, *options.required_author_fields}

    def finish() -> None:
        if records:
            _close_record(
                records[-1], fields, empty_values, options.required_author_fields, violations
            )

    for command in document.commands:
        cancellation_point()
        if command.start < ignored_until:
            continue
        if command.name in _DECLARATION_COMMANDS:
            # The declaration's literal command token is not an author record.
            ignored_until = command_end(command)
            continue
        if not command.depth and (command.name == "maketitle" or command.begins("document")):
            break
        if command.name in _DYNAMIC_COMMANDS:
            uncertainty.append("dynamically redefined preamble commands")
        if command.name not in configured:
            continue
        location = document.location(command.start).as_dict()
        # A \if… token only matters where it encloses a record: a \newif-defined
        # name or a branch-taking package conditional executes nothing by itself.
        if in_conditional(document.conditionals, command.start):
            uncertainty.append(
                f"{location_label(location)}: author record inside conditional control flow"
            )
            continue
        if command.depth or command.options or len(command.arguments) != 1:
            uncertainty.append(
                f"{location_label(location)}: grouped, optional or ambiguous author-record syntax"
            )
            continue
        value, reasons = plain_argument_text(command)
        uncertainty.extend(reasons)
        nonempty = bool(value and not is_placeholder_text(value))
        if command.name == options.author_command:
            finish()
            fields = {}
            records.append({"author_index": len(records) + 1, **location})
            if not nonempty and not reasons:
                empty_values.add(records[-1]["author_index"])
        elif not records:
            violations.append({**location, "field": command.name, "reason": "field before author"})
        elif nonempty and not reasons:
            fields[command.name] = fields.get(command.name, 0) + 1
    finish()
    if not records:
        violations.append({"reason": "no supported literal author records"})
    reasons = sorted(set(uncertainty))
    # Any interpretation gap withholds every violation, unlike the manuscript policies.
    confirmed = violations if not reasons else []
    message = "Checked each configured sequential literal author record."
    if confirmed:
        message = _failure_message(confirmed, len(records), options.author_command)
    elif reasons:
        message += " Inconclusive: " + reasons[0].rstrip(".") + "."
    incomplete = bool(uncertainty)
    status = check_status(failed=bool(confirmed), incomplete=incomplete)
    return [
        Finding(
            _RULE,
            message,
            severity="error" if confirmed or incomplete else "info",
            status=status,
            path=main,
            evidence_kind=EvidenceKind.DERIVED,
            details={
                "records": records[:_MAX_REPORTED_RECORDS],
                "observed_violations": violations[:_MAX_REPORTED_RECORDS],
                "uncertainty": reasons,
                "author_command": options.author_command,
                "required_fields": list(options.required_author_fields),
                "scope": "User-selected preamble record grammar; no template, identity or "
                "shared-field inference.",
                "violations": confirmed[:_MAX_REPORTED_RECORDS],
                "violation_count": len(confirmed),
                "incomplete": incomplete,
            },
        )
    ]
