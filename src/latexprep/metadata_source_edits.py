"""Explicit literal metadata edits in copied TeX sources, and the final PDF recheck.

``plan_metadata_sanitization`` proposes all-or-nothing replacements of ``\\title``,
``\\author``, ``\\email``, ``\\affiliation`` and selected ``\\hypersetup`` keys, and only
where the unique declaration holds the exact expected old value. Generated, ambiguous or
conditional declarations block the whole proposal. ``check_sanitized_pdf_metadata`` then
compares freshly inspected PDF properties with the edits, because a source proposal is
never proof that the rebuilt PDF is clean.

Does not own: the edit options (``metadata_privacy``) or image metadata scans
(``image_metadata``).
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .file_safety import read_regular_file
from .file_scan import inventory_files
from .findings import Finding, has_blockers
from .metadata_privacy import (
    MetadataEdit,
    MetadataPrivacyOptions,
    is_literal_metadata_text,
    normalize_for_matching,
)
from .report import Change, ChangeKind
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    blank_regions,
    comment_starts,
    conditional_spans,
    in_conditional,
    read_group,
    scan_commands,
    source_regions,
)
from .text_edits import OverlappingEdits, TextEdit, apply_text_edits, unified_diff_text, with_bom

# Problems named in a message; the finding's details list every one.
_MAX_LISTED_PROBLEMS: Final = 3
# Commands that can change a declaration while TeX runs, so its source text is not final.
_DYNAMIC_COMMANDS: Final = frozenset(
    {"catcode", "csname", "scantokens", "ExplSyntaxOn", "directlua"}
)
# A hyperref setting is ``key``, ``key=value`` or ``key={value}``. A key without a value is a
# yes/no option such as ``colorlinks`` or ``hidelinks``.
_HYPERSETUP_KEY: Final = re.compile(r"([A-Za-z]+)\s*(=\s*)?")
_PAGE_COUNT: Final = re.compile(r"[1-9][0-9]*")
# A report row describing one problem of the final PDF; its keys are part of the report.
Violation = dict[str, object]


class Span(NamedTuple):
    """A half-open range of source text."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class PlannedEdit:
    """The proposal for one source file: its original bytes, edited bytes and change record."""

    original: bytes
    content: bytes
    change: Change


@dataclass
class MetadataSanitizationPlan:
    """All-or-nothing source proposals, keyed by project-relative path, with their findings."""

    planned: dict[str, PlannedEdit] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    @property
    def contents(self) -> dict[str, bytes]:
        return {name: edit.content for name, edit in self.planned.items()}

    @property
    def originals(self) -> dict[str, bytes]:
        return {name: edit.original for name, edit in self.planned.items()}

    @property
    def changes(self) -> list[Change]:
        return [edit.change for edit in self.planned.values()]


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _hypersetup_values(text: str, start: int, end: int) -> list[tuple[str, Span]]:
    """Locate plain keys with braced or unbraced literal values, retaining source offsets.

    Keys without a value are accepted and skipped; they hold no text to edit.
    """
    result = []
    cursor = start
    while cursor < end:
        while cursor < end and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor == end:
            break
        match = _HYPERSETUP_KEY.match(text, cursor, end)
        if match is None:
            raise PreparationError(
                f"\\hypersetup on line {_line_of(text, cursor)} has a setting that is not "
                "written as key, key=value or key={value}"
            )
        key = match[1]
        cursor = _skip_spaces(text, match.end(), end)
        if match[2] is None:
            if cursor < end and text[cursor] != ",":
                raise PreparationError(
                    f"hyperref key {key} is followed by text that is neither '=' nor ','"
                )
            continue
        value, cursor = _read_hypersetup_value(text, key, cursor, end)
        result.append((key, value))
    return result


def _skip_spaces(text: str, cursor: int, end: int) -> int:
    while cursor < end and text[cursor].isspace():
        cursor += 1
    return cursor


def _read_hypersetup_value(text: str, key: str, cursor: int, end: int) -> tuple[Span, int]:
    """The span of one braced or unbraced value starting at ``cursor``, and where to resume."""
    if cursor < end and text[cursor] == "{":
        value = read_group(text, cursor)
        if value is None or value.end > end:
            raise PreparationError(f"The braced value of hyperref key {key} is not closed")
        after = _skip_spaces(text, value.end + 1, end)
        if after < end and text[after] != ",":
            raise PreparationError(
                f"The braced value of hyperref key {key} is followed by text before the next comma"
            )
        return Span(value.start, value.end), value.end + 1
    stop = text.find(",", cursor, end)
    stop = end if stop < 0 else stop
    trimmed = stop
    while trimmed > cursor and text[trimmed - 1].isspace():
        trimmed -= 1
    return Span(cursor, trimmed), stop


def _selected_command_problem(command: Command, text: str) -> str | None:
    """Why a selected declaration cannot be edited safely, or None when it is a plain one."""
    name = f"\\{command.name}"
    if command.depth:
        return f"{name} is inside braces or an environment, so its scope is not certain"
    if command.options:
        return f"{name} has an optional argument, which this edit does not support"
    if len(command.arguments) != 1:
        return f"{name} does not have exactly one braced argument"
    if text.startswith("\\" + command.name + "*", command.start):
        return f"{name} is used in its starred form, which this edit does not support"
    return None


def _scan_source(text: str) -> list[Command]:
    """The commands of ``text``, refusing sources whose declarations cannot be trusted."""
    regions, unclosed = source_regions(text)
    if unclosed:
        raise PreparationError("An unclosed literal region prevents metadata editing")
    try:
        commands = scan_commands(blank_regions(text, regions), comment_starts(regions))
    except ParseLimitExceeded:
        raise PreparationError("Source exceeds supported metadata parsing limits") from None
    dynamic = next((c.name for c in commands if c.name in _DYNAMIC_COMMANDS), None)
    if dynamic is not None:
        raise PreparationError(
            f"\\{dynamic} can change metadata while TeX runs, so the declared values are "
            "not certain"
        )
    return commands


def _collect_declarations(
    text: str, commands: list[Command], edits: Sequence[MetadataEdit]
) -> dict[str, list[Span]]:
    """The value spans of each selected field's declarations, up to the end of the document."""
    by_command = {edit.command: edit.field for edit in edits if edit.command is not None}
    by_key = {edit.hyperref_key: edit.field for edit in edits if edit.hyperref_key is not None}
    targets: dict[str, list[Span]] = {edit.field: [] for edit in edits}
    # Only a conditional that encloses a selected declaration makes its old value
    # unestablished; an unrelated \if…\fi elsewhere in the file does not.
    conditionals = conditional_spans(commands)
    for command in commands:
        if command.name == "endinput" or command.ends("document"):
            if command.depth:
                raise PreparationError("Grouped document termination prevents metadata editing")
            break
        if command.name not in by_command and not (command.name == "hypersetup" and by_key):
            continue
        if in_conditional(conditionals, command.start):
            raise PreparationError(
                f"\\{command.name} is inside conditional TeX (\\if ... \\fi), so which value "
                "applies is not certain"
            )
        problem = _selected_command_problem(command, text)
        if problem is not None:
            raise PreparationError(problem)
        argument = command.arguments[0]
        if command.name == "hypersetup":
            for key, span in _hypersetup_values(text, argument.start, argument.end):
                if key in by_key:
                    targets[by_key[key]].append(span)
        else:
            targets[by_command[command.name]].append(Span(argument.start, argument.end))
    return targets


def _edit_spans(text: str, edits: Sequence[MetadataEdit]) -> list[TextEdit]:
    """The text edits that apply ``edits`` to ``text``; raises when any precondition fails."""
    targets = _collect_declarations(text, _scan_source(text), edits)
    replacements = []
    for edit in edits:
        spans = targets[edit.field]
        if len(spans) != 1:
            found = "was not found" if not spans else f"was declared {len(spans)} times"
            raise PreparationError(f"{edit.label} {found}; exactly one declaration is required")
        span = spans[0]
        actual = text[span.start : span.end]
        if not is_literal_metadata_text(actual) or actual != edit.expected_before:
            raise PreparationError(
                f"{edit.label} does not hold the expected old value; the source changed or "
                "expected_before is wrong"
            )
        replacement = edit.replacement
        if edit.hyperref_key is not None and text[span.start - 1 : span.start] != "{":
            replacement = "{" + replacement + "}"
        replacements.append(TextEdit(span.start, span.end, replacement))
    return replacements


def _plan_file(name: str, original: bytes, selected: Sequence[MetadataEdit]) -> PlannedEdit | None:
    """The proposal for one file, or None when the edits would leave it unchanged."""
    bom = original.startswith(codecs.BOM_UTF8)
    text = original.decode("utf-8-sig")
    try:
        prepared = apply_text_edits(text, _edit_spans(text, selected))
    except OverlappingEdits:
        raise PreparationError("Metadata edits address overlapping source text") from None
    if prepared == text:
        return None
    change = Change(
        name,
        kind=ChangeKind.METADATA_SANITIZATION,
        reason="Explicitly selected literal metadata replacements; final PDF recheck required.",
        diff=unified_diff_text(name, text, prepared),
    )
    return PlannedEdit(original, with_bom(prepared, bom).encode("utf-8"), change)


def _matched_finding(name: str, selected: Sequence[MetadataEdit]) -> Finding:
    return Finding(
        "privacy.metadata_sanitization",
        "Selected literal metadata matched its old-value "
        "preconditions; final rebuilt PDF metadata still requires verification.",
        severity="info",
        status="passed",
        path=name,
        details={
            "fields": [edit.field for edit in selected],
            "final_pdf_recheck_required": True,
        },
    )


def _rejected_finding(name: str, selected: Sequence[MetadataEdit], error: Exception) -> Finding:
    # Rejection messages are fixed text; file-system errors may name absolute paths.
    reason = (
        str(error)
        if isinstance(error, PreparationError)
        else "the file is unreadable or is not UTF-8 text"
    )
    return Finding(
        "privacy.metadata_sanitization",
        f"Metadata edit cannot be proposed: {reason}.",
        severity="error",
        status="inconclusive",
        path=name,
        suggestion="Compare the edit's path, field and expected_before with the "
        "source file, correct the edit or the source, and run again.",
        details={"fields": [edit.field for edit in selected]},
    )


def plan_metadata_sanitization(
    root: Path, options: MetadataPrivacyOptions
) -> MetadataSanitizationPlan:
    """Return all-or-nothing source proposals; never modify the source directory."""
    plan = MetadataSanitizationPlan()
    if not options.edits:
        return plan
    files, issues = inventory_files(root)
    if issues:
        plan.findings.append(
            Finding(
                "privacy.metadata_sanitization",
                "Source inventory is incomplete; no metadata changes are proposed.",
                severity="error",
                status="inconclusive",
            )
        )
        return plan
    total = 0
    for name in sorted({edit.path for edit in options.edits}):
        cancellation_point()
        selected = [edit for edit in options.edits if edit.path == name]
        try:
            if name not in files:
                raise PreparationError("Explicit metadata source file is absent")
            original = read_regular_file(files[name], MAX_SOURCE_BYTES)
            total += len(original)
            if total > MAX_TOTAL_SOURCE_BYTES:
                raise PreparationError("Metadata sources exceed the total source-byte limit")
            planned = _plan_file(name, original, selected)
            if planned is not None:
                plan.planned[name] = planned
            plan.findings.append(_matched_finding(name, selected))
        except (OSError, UnicodeError, PreparationError) as error:
            plan.findings.append(_rejected_finding(name, selected, error))
    if has_blockers(plan.findings):
        plan.planned.clear()
    return plan


class _FinalProperties(NamedTuple):
    """PDF properties by case-folded name, and the spelling each name had in the PDF."""

    values: dict[str, str]
    spellings: dict[str, str]


def _final_properties(pdf_findings: Sequence[Finding]) -> _FinalProperties:
    """The single inspected property inventory; raises when it is missing or ambiguous."""
    inventories = [
        finding.details.get("properties")
        for finding in pdf_findings
        if finding.rule == "pdf.metadata" and finding.status == "passed"
    ]
    inventory = inventories[0] if len(inventories) == 1 else None
    if not isinstance(inventory, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in inventory.items()
    ):
        raise PreparationError(
            "Fresh final PDF property evidence is missing or ambiguous after the selected "
            "source edits."
        )
    values = {key.casefold(): value for key, value in inventory.items()}
    if len(values) != len(inventory) or not _PAGE_COUNT.fullmatch(values.get("pages", "")):
        raise PreparationError(
            "Final PDF properties have ambiguous names or lack document page-count evidence."
        )
    return _FinalProperties(values, {key.casefold(): key for key in inventory})


def _edit_violations(
    index: int, edit: MetadataEdit, properties: _FinalProperties
) -> list[Violation]:
    """The ways the final PDF properties disagree with one edit."""
    violations: list[Violation] = []

    def violation(name: str, problem: str) -> None:
        # The reason names the edit and property, never a value, and is what a terminal
        # report shows for each violation.
        violations.append(
            {
                "edit_index": index,
                "property": name,
                "reason": f"Edit {index}: PDF property {name} {problem}",
            }
        )

    if edit.pdf_property is not None:
        shown = " ".join(properties.values.get(edit.pdf_property.casefold(), "").split())
        if shown != " ".join(edit.replacement.split()):
            violation(edit.pdf_property, "does not show the selected replacement")
    old = normalize_for_matching(edit.expected_before)
    if old and old not in normalize_for_matching(edit.replacement):
        for key, value in properties.values.items():
            if old in normalize_for_matching(value):
                violation(
                    properties.spellings[key], "still contains the value that the edit removes"
                )
    return violations


def _recheck_message(violations: Sequence[Mapping[str, object]]) -> str:
    """Outcome of the final-PDF recheck, naming the first few problems."""
    if not violations:
        return (
            "Checked selected metadata replacements and removed "
            "literal values in final reported PDF properties."
        )
    shown = "; ".join(str(item["reason"]) for item in violations[:_MAX_LISTED_PROBLEMS])
    more = len(violations) - _MAX_LISTED_PROBLEMS
    return (
        f"Final PDF metadata does not match the selected edits: {shown}"
        + (f"; and {more} more" if more > 0 else "")
        + ". Correct the metadata in the source, rebuild the PDF and run the check again."
    )


def check_sanitized_pdf_metadata(
    pdf_findings: Sequence[Finding], options: MetadataPrivacyOptions
) -> list[Finding]:
    """Recheck freshly inspected PDF properties; never treat a source proposal as proof."""
    if not options.edits:
        return []
    try:
        properties = _final_properties(pdf_findings)
    except PreparationError as error:
        return [
            Finding(
                "privacy.sanitized_pdf_metadata",
                str(error),
                severity="error",
                status="inconclusive",
            )
        ]
    violations = [
        violation
        for index, edit in enumerate(options.edits, 1)
        for violation in _edit_violations(index, edit, properties)
    ]
    return [
        Finding(
            "privacy.sanitized_pdf_metadata",
            _recheck_message(violations),
            severity="error" if violations else "info",
            status="failed" if violations else "passed",
            details={
                "violations": violations,
                "scope": "Reported top-level PDF properties only; hidden objects, image pixels and "
                "semantic anonymity remain unverified.",
            },
        )
    ]
