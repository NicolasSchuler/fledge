"""Bounded tagged-PDF structure observations and the structure option schema.

The tagged-structure checks read the structure tree through qpdf's object data: tables,
links, figure alternative text and the parent links that tie marked content to its
elements. Expected-heading checks read the extracted text. None of them infer heading
semantics or establish PDF/UA conformance, meaningful reading order, or description
quality. The options also configure the literal author-record contract that
``author_records`` checks, which is not a PDF check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, NamedTuple

from .command_runner import CommandRunner
from .errors import PreparationError, SettingsError
from .findings import Finding
from .measurement_findings import MAX_SAMPLES, measurement_finding, unavailable_finding
from .option_config import (
    Bounds,
    TextFormat,
    require_records,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .pdf_qpdf_graph import QpdfObjectGraph, is_object_reference
from .pdf_rule_names import PdfRule
from .pdf_structure_tree import MAX_STRUCTURE_ITEMS, StructureElement, StructureTree
from .pdf_tool_output import MAX_INSPECTION_PAGES, split_text_pages
from .pdf_tools import ToolMeasurement, layout_text_argv, preflight_pdf, qpdf_objects_argv
from .resource_budget import ResourceBudget

# Lines of a page that end with an expected title, shown as candidates when none matches.
_MAX_HEADING_CANDIDATES = 10
# Configuration bound on explicit heading expectations (not on the page they may name).
_MAX_HEADING_EXPECTATIONS = 300
_MAX_HEADING_TITLE_CHARACTERS = 256
_MAX_HEADING_NUMBER_CHARACTERS = 64
# Associated author-block commands, such as affiliation or email, that must follow each author.
_MAX_AUTHOR_FIELDS = 32
_COMMAND = re.compile(r"[A-Za-z][A-Za-z@]{0,63}")


def _require_single_line(name: str, value: object, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise SettingsError(
            f"{name} must be one line of at most {max_length} characters "
            "without surrounding spaces",
            (name,),
        )


@dataclass(frozen=True)
class HeadingExpectation:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {"page": Bounds(1, MAX_INSPECTION_PAGES)}

    page: int
    title: str
    number: str

    def __post_init__(self) -> None:
        validate_scalars(self)
        _require_single_line("title", self.title, _MAX_HEADING_TITLE_CHARACTERS)
        _require_single_line("number", self.number, _MAX_HEADING_NUMBER_CHARACTERS)
        if not self.title:
            raise SettingsError("title must not be empty", ("title",))


@dataclass(frozen=True)
class StructureOptions:
    author_command: str = "author"
    required_author_fields: tuple[str, ...] = ()
    heading_expectations: tuple[HeadingExpectation, ...] = ()
    require_structure_tree: bool = False
    check_table_structure: bool = False
    check_link_structure: bool = False
    require_figure_alt: bool = False
    check_structure_references: bool = False

    def __post_init__(self) -> None:
        validate_scalars(self)
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
        require_records(
            "heading_expectations",
            self.heading_expectations,
            HeadingExpectation,
            max_items=_MAX_HEADING_EXPECTATIONS,
            distinct_by=lambda heading: (heading.page, heading.title),
            distinct_description="page and title",
        )


def _heading_findings(text: str, pages: int, options: StructureOptions) -> list[Finding]:
    extracted = split_text_pages(text, pages)
    results = []
    for expectation in options.heading_expectations:
        expected = " ".join((expectation.number + " " + expectation.title).split())
        title = " ".join(expectation.title.split())
        outside = expectation.page > pages
        lines = (
            []
            if outside
            else [" ".join(line.split()) for line in extracted[expectation.page - 1].splitlines()]
        )
        count = lines.count(expected)
        uncertain = outside or not any(lines) or count > 1
        candidates = [line for line in lines if line == title or line.endswith(" " + title)]
        results.append(
            measurement_finding(
                PdfRule.EXPECTED_HEADINGS,
                f"Checked configured heading text on page {expectation.page}.",
                ["Expected numbered heading line was not extracted"]
                if not count and not uncertain
                else [],
                incomplete=uncertain,
                page=expectation.page,
                expected_title=expectation.title,
                expected_number=expectation.number,
                matching_line_count=count,
                candidates=candidates[:_MAX_HEADING_CANDIDATES],
                scope="Exact line after whitespace normalization; heading meaning and "
                "numbering are supplied, not inferred.",
            )
        )
    return results


def _marked_content_problems(tree: StructureTree) -> tuple[list[object], list[object]]:
    """Problems with each marked-content item's page and parent-tree entry, and the items."""
    problems: list[object] = []
    sequence: list[object] = []
    seen: set[tuple[str | None, str | None, int]] = set()
    for owner, page, stream, mcid in tree.content:
        item = {
            **tree.location(owner),
            "page": tree.pages.get(page or ""),
            "mcid": mcid,
            "stream": stream,
        }
        sequence.append(item)
        identity = page, stream, mcid
        if identity in seen:
            problems.append({**item, "reason": "duplicate marked-content reference"})
        seen.add(identity)
        if page not in tree.pages:
            problems.append({**item, "reason": "marked content lacks a valid page reference"})
            continue
        container = tree.objects.dictionary(stream or page)
        parents = tree.parent_entry(container, "/StructParents")
        if not isinstance(parents, list) or mcid >= len(parents) or parents[mcid] is None:
            problems.append({**item, "reason": "missing marked-content parent-tree entry"})
        elif tree.objects.resolve(parents[mcid]) is not tree.elements[owner].data:
            problems.append({**item, "reason": "marked-content parent-tree owner does not match"})
    return problems, sequence


def _object_reference_problems(tree: StructureTree) -> list[object]:
    problems: list[object] = []
    for owner, page, target in tree.object_references:
        item = {**tree.location(owner), "target": target}
        if not is_object_reference(target):
            problems.append({**item, "reason": "missing indirect object reference"})
            continue
        target_object = tree.objects.dictionary(target)
        if page not in tree.pages:
            problems.append({**item, "reason": "object reference lacks a valid page"})
        parent = tree.parent_entry(target_object, "/StructParent")
        if parent is None:
            problems.append({**item, "reason": "missing object parent-tree entry"})
        elif parent is not tree.elements[owner].data:
            problems.append({**item, "reason": "object parent-tree owner does not match"})
    return problems


def _structure_references(tree: StructureTree, required: bool) -> Finding:
    problems = list(tree.problems)
    if required and not tree.elements:
        problems.append({"reason": "Required structure tree has no structure elements"})
    content_problems, sequence = _marked_content_problems(tree)
    problems.extend(content_problems)
    problems.extend(_object_reference_problems(tree))
    return measurement_finding(
        PdfRule.STRUCTURE_REFERENCES,
        "Checked structure-tree parent links and marked-content references.",
        problems,
        incomplete=bool(tree.uncertainty),
        uncertainty=sorted(tree.uncertainty),
        structural_sequence=sequence[:MAX_SAMPLES],
        element_count=len(tree.elements),
        marked_content_count=len(tree.content),
        scope="Declared references only; stream completeness and intended reading order "
        "require human review. No PDF/UA claim.",
    )


_TABLE_SECTIONS = frozenset({"/THead", "/TBody", "/TFoot"})
_TABLE_CELLS = frozenset({"/TH", "/TD"})


def _table_rows(
    tree: StructureTree, table: StructureElement, violations: list[object]
) -> list[int]:
    """The rows of a table, looking through its header, body and footer groups."""
    rows: list[int] = []
    pending = list(reversed(table.children))
    while pending:
        child = pending.pop()
        role = tree.elements[child].role
        if role in _TABLE_SECTIONS:
            pending.extend(reversed(tree.elements[child].children))
        elif role == "/TR":
            rows.append(child)
        elif role != "/Caption":
            violations.append({**tree.location(child), "reason": "unsupported child inside Table"})
    return rows


def _table_summary(tree: StructureTree, index: int, violations: list[object]) -> dict[str, object]:
    """Check one table's rows and cells; its location with its row and cell counts."""
    rows = _table_rows(tree, tree.elements[index], violations)
    headers = cells = 0
    if not rows:
        violations.append({**tree.location(index), "reason": "table has no tagged rows"})
    for row in rows:
        children = tree.elements[row].children
        if not children:
            violations.append({**tree.location(row), "reason": "table row has no tagged cells"})
        for child in children:
            role = tree.elements[child].role
            if role not in _TABLE_CELLS:
                violations.append(
                    {**tree.location(child), "reason": "table row child is not TH or TD"}
                )
            else:
                cells += 1
                headers += role == "/TH"
    return {**tree.location(index), "rows": len(rows), "cells": cells, "header_cells": headers}


def _table_structure(tree: StructureTree) -> Finding:
    violations: list[object] = []
    tables = [
        _table_summary(tree, index, violations)
        for index, element in enumerate(tree.elements)
        if element.role == "/Table"
    ]
    result = measurement_finding(
        PdfRule.TABLE_STRUCTURE,
        "Checked tagged table, row and cell relationships.",
        violations,
        incomplete=bool(tree.uncertainty),
        tables=tables,
        uncertainty=sorted(tree.uncertainty),
        scope="Tagged Tables only; untagged visual tables, header meaning, spans and "
        "associations are not inferred.",
    )
    if not tables and not tree.uncertainty:
        result = replace(result, status="not_applicable")
    return result


def _unicode_string(value: object) -> str | None:
    return value[2:] if isinstance(value, str) and value.startswith("u:") else None


def _figure_alt(tree: StructureTree) -> Finding:
    violations: list[object] = []
    uncertain = bool(tree.uncertainty)
    figures = 0
    for index, element in enumerate(tree.elements):
        if element.role != "/Figure":
            continue
        figures += 1
        raw = tree.objects.resolve(element.data.get("/Alt"))
        text = _unicode_string(raw)
        if raw is None or text is not None and not text.strip():
            violations.append({**tree.location(index), "reason": "missing or empty Figure Alt"})
        elif text is None:
            uncertain = True
    result = measurement_finding(
        PdfRule.FIGURE_ALTERNATIVE_TEXT,
        "Checked nonempty Alt strings on tagged Figures.",
        violations,
        incomplete=uncertain,
        figure_count=figures,
        scope="Tagged Figure Alt presence only; image coverage and description quality "
        "are not established.",
    )
    if not figures and not uncertain:
        result = replace(result, status="not_applicable")
    return result


class _LinkOwner(NamedTuple):
    """A Link element that owns an object reference, and the page that reference is on."""

    index: int
    page: str | None


class _Problem(NamedTuple):
    """What is wrong with a link target: a violation, or something the check cannot resolve."""

    reason: str
    unresolved: bool = False


@dataclass(slots=True)
class _LinkAudit:
    """What the link check has found so far, across every page's annotations."""

    tree: StructureTree
    associated: dict[str, list[_LinkOwner]]
    uncertainty: set[str]
    violations: list[object] = field(default_factory=list)
    annotations: list[object] = field(default_factory=list)
    encountered: set[str] = field(default_factory=set)

    def violate(self, item: dict[str, object], reason: str) -> None:
        self.violations.append({**item, "reason": reason})

    def record(self, item: dict[str, object], problem: _Problem | None) -> None:
        if problem is None:
            return
        if problem.unresolved:
            self.uncertainty.add(problem.reason)
        else:
            self.violate(item, problem.reason)


def _link_tag_associations(tree: StructureTree) -> dict[str, list[_LinkOwner]]:
    """For each annotation an element references, the Link elements that own the reference."""
    associated: dict[str, list[_LinkOwner]] = {}
    for reference in tree.object_references:
        target = reference.target
        if not is_object_reference(target):
            continue
        index: int | None = reference.owner
        while index is not None and tree.elements[index].role != "/Link":
            index = tree.elements[index].parent
        if index is not None:
            associated.setdefault(target, []).append(_LinkOwner(index, reference.page))
    return associated


def _destination_problem(tree: StructureTree, raw: object) -> _Problem | None:
    destination = tree.objects.resolve(raw)
    if destination is None or destination == [] or destination in ("u:", "b:", "/"):
        return _Problem("link destination is empty")
    if not isinstance(destination, list):
        return _Problem("named or unsupported link destination is not resolved", unresolved=True)
    first = destination[0] if destination else None
    if len(destination) < 2 or not isinstance(first, str) or first not in tree.pages:
        return _Problem("local link destination lacks a valid page")
    return None


def _check_link_action(audit: _LinkAudit, raw: object, item: dict[str, object]) -> None:
    tree = audit.tree
    action = tree.objects.dictionary(raw)
    kind = action.get("/S")
    if kind == "/URI":
        target = _unicode_string(tree.objects.resolve(action.get("/URI")))
        if target is None:
            audit.uncertainty.add("link URI is not a supported Unicode string")
        elif not target.strip():
            audit.violate(item, "link URI is empty")
    elif kind != "/GoTo":
        audit.uncertainty.add("link action is outside supported URI/GoTo inspection")
    elif "/D" not in action:
        audit.violate(item, "GoTo link action has no destination")
    else:
        audit.record(item, _destination_problem(tree, action["/D"]))


def _check_link_annotation(
    audit: _LinkAudit, page: str, number: int, reference: str, annotation: dict[str, object]
) -> None:
    item: dict[str, object] = {"page": number, "annotation": reference}
    audit.annotations.append(item)
    if reference in audit.encountered:
        audit.violate(item, "Link annotation is repeated in page arrays")
    audit.encountered.add(reference)
    owners = audit.associated.get(reference, [])
    if len(owners) != 1:
        audit.violate(item, "link annotation needs exactly one Link tag association")
    elif owners[0].page != page:
        audit.violate(item, "Link object reference has the wrong page")
    if "/Dest" not in annotation and "/A" not in annotation:
        audit.violate(item, "link annotation has no destination or action")
    if "/Dest" in annotation:
        audit.record(item, _destination_problem(audit.tree, annotation["/Dest"]))
    if "/A" in annotation:
        _check_link_action(audit, annotation["/A"], item)


def _audit_page_link_annotations(audit: _LinkAudit) -> None:
    tree = audit.tree
    for page, number in tree.pages.items():
        page_object = tree.objects.dictionary(page)
        raw = tree.objects.resolve(page_object.get("/Annots", []))
        if not isinstance(raw, list) or len(raw) > MAX_STRUCTURE_ITEMS:
            raise PreparationError("PDF page annotation array is malformed or exceeds bounds")
        for reference in raw:
            annotation = tree.objects.dictionary(reference)
            if annotation.get("/Subtype") != "/Link":
                continue
            if not is_object_reference(reference):
                audit.uncertainty.add("direct link annotation lacks an indirect identity")
                continue
            _check_link_annotation(audit, page, number, reference, annotation)


def _orphan_link_findings(audit: _LinkAudit) -> set[int]:
    """Report Link tags without an annotation and references without a page annotation.

    Returns the Link elements that own an annotation reference.
    """
    associated = audit.associated
    for reference in associated:
        if reference not in audit.encountered:
            audit.violations.append(
                {"target": reference, "reason": "Link tag references no page Link annotation"}
            )
    linked_owners = {owner.index for owners in associated.values() for owner in owners}
    for index, element in enumerate(audit.tree.elements):
        if element.role == "/Link" and index not in linked_owners:
            audit.violations.append(
                {**audit.tree.location(index), "reason": "Link tag has no associated annotation"}
            )
    return linked_owners


def _link_structure(tree: StructureTree) -> Finding:
    audit = _LinkAudit(
        tree, associated=_link_tag_associations(tree), uncertainty=set(tree.uncertainty)
    )
    _audit_page_link_annotations(audit)
    linked_owners = _orphan_link_findings(audit)
    result = measurement_finding(
        PdfRule.LINK_STRUCTURE,
        "Checked Link tags against page Link annotations.",
        audit.violations,
        incomplete=bool(audit.uncertainty),
        annotations=audit.annotations[:MAX_SAMPLES],
        uncertainty=sorted(audit.uncertainty),
        scope="Object associations and supported target presence only; name adequacy, "
        "navigation and target availability are unverified.",
    )
    if not (audit.annotations or linked_owners or audit.violations or audit.uncertainty):
        result = replace(result, status="not_applicable")
    return result


def _object_rules(options: StructureOptions) -> list[str]:
    return [
        rule
        for rule, selected in (
            (PdfRule.TABLE_STRUCTURE, options.check_table_structure),
            (PdfRule.LINK_STRUCTURE, options.check_link_structure),
            (PdfRule.FIGURE_ALTERNATIVE_TEXT, options.require_figure_alt),
            (
                PdfRule.STRUCTURE_REFERENCES,
                options.require_structure_tree or options.check_structure_references,
            ),
        )
        if selected
    ]


def required_structure_tools(options: StructureOptions) -> set[str]:
    """The external tools the configured checks run, for setup diagnostics."""
    object_rules = _object_rules(options)
    tools = {"pdfinfo"} if object_rules or options.heading_expectations else set()
    if options.heading_expectations:
        tools.add("pdftotext")
    if object_rules:
        tools.add("qpdf")
    return tools


def _object_findings(output: str, pages: int, options: StructureOptions) -> list[Finding]:
    rules = _object_rules(options)
    objects = QpdfObjectGraph(output, pages)
    if objects.root.get("/StructTreeRoot") is None:
        required = options.require_structure_tree
        return [
            measurement_finding(
                rule,
                "The PDF catalog has no structure tree.",
                ["A structure tree is explicitly required"]
                if rule == PdfRule.STRUCTURE_REFERENCES and required
                else [],
                incomplete=not (rule == PdfRule.STRUCTURE_REFERENCES and required),
            )
            for rule in rules
        ]
    tree = StructureTree.read(objects)
    checks = {
        PdfRule.TABLE_STRUCTURE: lambda: _table_structure(tree),
        PdfRule.LINK_STRUCTURE: lambda: _link_structure(tree),
        PdfRule.FIGURE_ALTERNATIVE_TEXT: lambda: _figure_alt(tree),
        PdfRule.STRUCTURE_REFERENCES: lambda: _structure_references(
            tree, options.require_structure_tree
        ),
    }
    findings = []
    for rule in rules:
        try:
            findings.append(checks[PdfRule(rule)]())
        except PreparationError as error:
            findings.append(unavailable_finding(rule, error))
    return findings


async def inspect_pdf_structure(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    options: StructureOptions,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect only selected line expectations and tagged PDF object structures."""
    object_rules = _object_rules(options)
    configured = (
        [PdfRule.EXPECTED_HEADINGS] if options.heading_expectations else []
    ) + object_rules
    if not configured:
        return []
    preflight = await preflight_pdf(pdf, work, runner, budget, configured)
    if isinstance(preflight, list):
        return preflight
    session, pages = preflight.session, preflight.pages
    measurements: list[ToolMeasurement] = []
    if options.heading_expectations:
        measurements.append(
            ToolMeasurement(
                "headings",
                layout_text_argv(),
                (PdfRule.EXPECTED_HEADINGS,),
                lambda result: _heading_findings(result.stdout, pages, options),
            )
        )
    if object_rules:
        measurements.append(
            ToolMeasurement(
                "structure",
                qpdf_objects_argv(),
                tuple(object_rules),
                lambda result: _object_findings(result.stdout, pages, options),
            )
        )
    findings: list[Finding] = []
    for measurement in measurements:
        findings.extend(await measurement.run(session))
    return findings
