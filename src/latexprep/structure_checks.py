"""Explicit author-record contracts and bounded PDF structural observations.

These checks do not interpret arbitrary author templates, infer heading semantics,
or establish PDF/UA conformance, meaningful reading order, or description quality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .manuscript_checks import _placeholder, _read, _stop, _value, _where
from .models import Finding, PreparationError
from .pdf import _page_count
from .pdf_checks import _Objects, _properties, _result, _session, _unavailable
from .runtime import ToolRunner
from .scheduler import ResourceBudget, cancellation_point
from .source import in_conditional

_LIMIT = 10000
_REFERENCE = re.compile(r"[0-9]+ [0-9]+ R")
_COMMAND = re.compile(r"[A-Za-z][A-Za-z@]{0,63}")
_STANDARD_ROLES = frozenset(
    "/" + role
    for role in (
        "Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private "
        "P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span "
        "Quote Note Reference BibEntry Code Link Annot Ruby RB RT RP Warichu WT WP "
        "Figure Formula Form"
    ).split()
)


@dataclass(frozen=True)
class HeadingExpectation:
    page: int
    title: str
    number: str

    def __post_init__(self) -> None:
        if type(self.page) is not int or not 1 <= self.page <= 300:
            raise PreparationError("Heading page must be an integer from 1 to 300")
        for name, value, limit in (("title", self.title, 256), ("number", self.number, 64)):
            if (
                not isinstance(value, str)
                or len(value) > limit
                or value != value.strip()
                or any(ord(character) < 32 for character in value)
            ):
                raise PreparationError(f"Heading {name} must be a bounded single-line string")
        if not self.title:
            raise PreparationError("Heading title cannot be empty")


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
        if not isinstance(self.author_command, str) or not _COMMAND.fullmatch(self.author_command):
            raise PreparationError(
                "Author command must be a literal command name without backslash"
            )
        if (
            not isinstance(self.required_author_fields, tuple)
            or len(self.required_author_fields) > 32
            or any(
                not isinstance(value, str) or not _COMMAND.fullmatch(value)
                for value in self.required_author_fields
            )
            or len(set(self.required_author_fields)) != len(self.required_author_fields)
            or self.author_command in self.required_author_fields
        ):
            raise PreparationError("Required author fields must be unique associated command names")
        if (
            not isinstance(self.heading_expectations, tuple)
            or len(self.heading_expectations) > 300
            or any(not isinstance(value, HeadingExpectation) for value in self.heading_expectations)
            or len({(value.page, value.title) for value in self.heading_expectations})
            != len(self.heading_expectations)
        ):
            raise PreparationError("Heading expectations must be unique HeadingExpectation records")
        for name in (
            "require_structure_tree",
            "check_table_structure",
            "check_link_structure",
            "require_figure_alt",
            "check_structure_references",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")


def check_author_records(root: Path, main: str | None, options: StructureOptions) -> list[Finding]:
    """Check sequential top-level literal records before begin{document}/maketitle.

    The user supplies the record-opening command and required associated commands.
    Each opener starts a new author; fields apply only until the next opener. This
    deliberately does not interpret affiliation indices or shared author lists.
    """
    if not options.required_author_fields:
        return []
    document = _read(root, main)
    uncertainty = list(document.graph_uncertainty)
    records: list[dict[str, object]] = []
    fields: dict[str, int] = {}
    violations: list[object] = []
    ignored_until = -1
    configured = {options.author_command, *options.required_author_fields}

    def finish() -> None:
        if not records:
            return
        record = records[-1]
        missing = [name for name in options.required_author_fields if not fields.get(name)]
        record["present_fields"] = sorted(name for name, count in fields.items() if count)
        record["missing_fields"] = missing
        if missing:
            violations.append({**record, "reason": "missing or empty associated author fields"})

    for command in document.commands:
        cancellation_point()
        if command.start < ignored_until:
            continue
        if command.name in {"newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"}:
            # The declaration's literal command token is not an author record.
            ignored_until = _stop(command)
            continue
        if not command.depth and (
            command.name == "maketitle"
            or command.name == "begin"
            and command.arguments
            and command.arguments[0].value.strip() == "document"
        ):
            break
        if command.name in {"def", "gdef", "xdef", "edef", "csname", "catcode", "let"}:
            uncertainty.append("dynamically redefined preamble commands")
        if command.name not in configured:
            continue
        location = document.location(command.start)
        # A \if… token only matters where it encloses a record: a \newif-defined
        # name or a branch-taking package conditional executes nothing by itself.
        if in_conditional(document.conditionals, command.start):
            uncertainty.append(f"{location}: author record inside conditional control flow")
            continue
        if command.depth or command.options or len(command.arguments) != 1:
            uncertainty.append(f"{location}: grouped, optional or ambiguous author-record syntax")
            continue
        value, reasons = _value(command)
        uncertainty.extend(reasons)
        nonempty = bool(value and not _placeholder(value))
        if command.name == options.author_command:
            finish()
            fields = {}
            records.append({"author_index": len(records) + 1, **location})
            if not nonempty and not reasons:
                violations.append({**records[-1], "reason": "empty or placeholder author value"})
        elif not records:
            violations.append({**location, "field": command.name, "reason": "field before author"})
        elif nonempty and not reasons:
            fields[command.name] = fields.get(command.name, 0) + 1
    finish()
    if not records:
        violations.append({"reason": "no supported literal author records"})
    reasons = sorted(set(uncertainty))
    confirmed: list[object] = violations if not reasons else []
    places = _where([item for item in confirmed if isinstance(item, dict)])
    message = "Checked each configured sequential literal author record."
    if confirmed:
        message = (
            f"{len(confirmed)} of {max(len(records), 1)} literal author records "
            "lack a configured field" + (f": {places}" if places else "") + "."
        )
    elif reasons:
        message += " Inconclusive: " + reasons[0].rstrip(".") + "."
    result = _result(
        "manuscript.author_records",
        message,
        confirmed,
        uncertain=bool(uncertainty),
        records=records[:100],
        observed_violations=violations[:100],
        uncertainty=reasons,
        author_command=options.author_command,
        required_fields=list(options.required_author_fields),
        scope="User-selected preamble record grammar; no template, identity or "
        "shared-field inference.",
    )
    return [replace(result, path=main)]


def _heading_findings(text: str, pages: int, options: StructureOptions) -> list[Finding]:
    if text.count("\f") != pages or text.split("\f")[-1].strip():
        raise PreparationError("Text extraction did not report every PDF page boundary")
    extracted = text.split("\f")[:-1]
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
            _result(
                "pdf.expected_headings",
                f"Checked configured heading text on page {expectation.page}.",
                ["Expected numbered heading line was not extracted"]
                if not count and not uncertain
                else [],
                uncertain=uncertain,
                page=expectation.page,
                expected_title=expectation.title,
                expected_number=expectation.number,
                matching_line_count=count,
                candidates=candidates[:10],
                scope="Exact line after whitespace normalization; heading meaning and "
                "numbering are supplied, not inferred.",
            )
        )
    return results


@dataclass
class _Element:
    reference: str | None
    role: str
    data: dict[str, object]
    parent: int | None
    page: str | None
    children: list[int] = field(default_factory=list)


class _Structure:
    def __init__(self, objects: _Objects):
        self.objects = objects
        root_value = objects.root.get("/StructTreeRoot")
        self.root_reference = root_value if isinstance(root_value, str) else None
        self.root = objects.dictionary(root_value)
        if self.root.get("/Type") != "/StructTreeRoot":
            raise PreparationError("The PDF structure root has an unsupported object type")
        self.pages: dict[str, int] = {}
        for number, page in enumerate(objects.pages, 1):
            if not isinstance(page, dict) or not isinstance(page.get("object"), str):
                raise PreparationError("qpdf page inventory has no valid object reference")
            reference = page["object"]
            if reference in self.pages or objects.dictionary(reference).get("/Type") != "/Page":
                raise PreparationError("qpdf page inventory is duplicated or malformed")
            self.pages[reference] = number
        self.roles = objects.dictionary(self.root.get("/RoleMap", {}))
        self.elements: list[_Element] = []
        self.content: list[tuple[int, str | None, str | None, int]] = []
        self.object_references: list[tuple[int, str | None, object]] = []
        self.problems: list[object] = []
        self.uncertainty: list[str] = []
        self.visited: set[str] = set()
        self.steps = 0
        self._visit(self.root.get("/K", []), None, None, 0)
        self.parents = self._parent_tree(self.root.get("/ParentTree"))

    def _role(self, value: object) -> str:
        role = self.objects.resolve(value)
        seen = set()
        while isinstance(role, str) and role in self.roles:
            if role in seen or len(seen) > 100:
                raise PreparationError("PDF structure role mapping is cyclic or excessively deep")
            seen.add(role)
            role = self.objects.resolve(self.roles[role])
        if not isinstance(role, str) or not role.startswith("/"):
            raise PreparationError("PDF structure element has no supported role name")
        if role not in _STANDARD_ROLES:
            self.uncertainty.append("unmapped or unsupported PDF structure role")
        return role

    def _visit(self, raw: object, parent: int | None, page: str | None, depth: int) -> None:
        cancellation_point()
        self.steps += 1
        if self.steps > _LIMIT or depth > 100:
            raise PreparationError("PDF structure tree exceeds bounded traversal limits")
        reference = raw if isinstance(raw, str) and _REFERENCE.fullmatch(raw) else None
        if reference:
            if reference in self.visited:
                raise PreparationError("PDF structure tree has a cycle or repeated object child")
            self.visited.add(reference)
        value = self.objects.resolve(raw)
        if isinstance(value, list):
            for child in value:
                self._visit(child, parent, page, depth + 1)
            return
        if type(value) is int:
            if parent is None or value < 0:
                self.problems.append({"reason": "MCID has no structural owner or is negative"})
            else:
                self.content.append((parent, page, None, value))
            return
        if not isinstance(value, dict):
            raise PreparationError("PDF structure child is not a supported element or content item")
        declared_page = value.get("/Pg", page)
        page = declared_page if isinstance(declared_page, str) else None
        kind = value.get("/Type")
        if kind == "/MCR":
            mcid = self.objects.resolve(value.get("/MCID"))
            stream = value.get("/Stm")
            if parent is None or type(mcid) is not int or mcid < 0:
                self.problems.append({"reason": "MCR is missing a valid nonnegative MCID or owner"})
            elif stream is not None and (
                not isinstance(stream, str) or not _REFERENCE.fullmatch(stream)
            ):
                self.uncertainty.append("non-reference marked-content stream")
            else:
                self.content.append((parent, page, stream, mcid))
            return
        if kind == "/OBJR":
            if parent is None:
                self.problems.append({"reason": "Object reference has no structural owner"})
            else:
                self.object_references.append((parent, page, value.get("/Obj")))
            return
        role = self._role(value.get("/S"))
        if "/NS" in value:
            self.uncertainty.append("PDF namespace-specific structure roles are not interpreted")
        expected_parent = self.root_reference if parent is None else self.elements[parent].reference
        if expected_parent is None:
            self.uncertainty.append(
                "direct structure dictionaries lack verifiable parent identities"
            )
        elif value.get("/P") != expected_parent:
            self.problems.append(
                {"object": reference, "reason": "missing or incorrect structure parent"}
            )
        index = len(self.elements)
        self.elements.append(_Element(reference, role, value, parent, page))
        if parent is not None:
            self.elements[parent].children.append(index)
        self._visit(value.get("/K", []), index, page, depth + 1)

    def _parent_tree(self, raw: object) -> dict[int, object]:
        if raw is None:
            return {}
        result: dict[int, object] = {}
        pending = [(raw, 0)]
        seen: set[str] = set()
        steps = 0
        while pending:
            value, depth = pending.pop()
            steps += 1
            if steps > _LIMIT or depth > 100:
                raise PreparationError("Structure parent number tree exceeds traversal limits")
            if isinstance(value, str):
                if value in seen:
                    raise PreparationError("Structure parent number tree is cyclic")
                seen.add(value)
            node = self.objects.dictionary(value)
            numbers = self.objects.resolve(node.get("/Nums", []))
            children = self.objects.resolve(node.get("/Kids", []))
            if not isinstance(numbers, list) or len(numbers) % 2 or not isinstance(children, list):
                raise PreparationError("Structure parent number tree is malformed")
            if len(result) + len(numbers) // 2 > _LIMIT or len(pending) + len(children) > _LIMIT:
                raise PreparationError("Structure parent number tree exceeds entry limits")
            for index in range(0, len(numbers), 2):
                key = numbers[index]
                if type(key) is not int or key < 0 or key in result:
                    raise PreparationError(
                        "Structure parent number tree has invalid or duplicate keys"
                    )
                result[key] = numbers[index + 1]
            pending.extend((child, depth + 1) for child in children)
        return result

    def parent_entry(self, container: dict[str, object], name: str) -> object:
        key = self.objects.resolve(container.get(name))
        if type(key) is not int or key < 0 or key not in self.parents:
            return None
        return self.objects.resolve(self.parents[key])

    def location(self, index: int) -> dict[str, object]:
        element = self.elements[index]
        return {
            "object": element.reference,
            "role": element.role,
            "page": self.pages.get(element.page or ""),
        }


def _structure_references(tree: _Structure, required: bool) -> Finding:
    problems = list(tree.problems)
    if required and not tree.elements:
        problems.append({"reason": "Required structure tree has no structure elements"})
    seen: set[tuple[str | None, str | None, int]] = set()
    sequence = []
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
    for owner, page, target in tree.object_references:
        item = {**tree.location(owner), "target": target}
        if not isinstance(target, str) or not _REFERENCE.fullmatch(target):
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
    return _result(
        "pdf.structure_references",
        "Checked structure-tree parent links and marked-content references.",
        problems,
        uncertain=bool(tree.uncertainty),
        uncertainty=tree.uncertainty,
        structural_sequence=sequence[:100],
        element_count=len(tree.elements),
        marked_content_count=len(tree.content),
        scope="Declared references only; stream completeness and intended reading order "
        "require human review. No PDF/UA claim.",
    )


def _table_structure(tree: _Structure) -> Finding:
    violations: list[object] = []
    tables = []
    for index, element in enumerate(tree.elements):
        if element.role != "/Table":
            continue
        rows: list[int] = []
        pending = list(reversed(element.children))
        while pending:
            child = pending.pop()
            role = tree.elements[child].role
            if role in {"/THead", "/TBody", "/TFoot"}:
                pending.extend(reversed(tree.elements[child].children))
            elif role == "/TR":
                rows.append(child)
            elif role != "/Caption":
                violations.append(
                    {**tree.location(child), "reason": "unsupported child inside Table"}
                )
        headers = cells = 0
        if not rows:
            violations.append({**tree.location(index), "reason": "table has no tagged rows"})
        for row in rows:
            children = tree.elements[row].children
            if not children:
                violations.append({**tree.location(row), "reason": "table row has no tagged cells"})
            for child in children:
                role = tree.elements[child].role
                if role not in {"/TH", "/TD"}:
                    violations.append(
                        {**tree.location(child), "reason": "table row child is not TH or TD"}
                    )
                else:
                    cells += 1
                    headers += role == "/TH"
        tables.append(
            {**tree.location(index), "rows": len(rows), "cells": cells, "header_cells": headers}
        )
    result = _result(
        "pdf.table_structure",
        "Checked tagged table, row and cell relationships.",
        violations,
        uncertain=bool(tree.uncertainty),
        tables=tables,
        uncertainty=tree.uncertainty,
        scope="Tagged Tables only; untagged visual tables, header meaning, spans and "
        "associations are not inferred.",
    )
    if not tables and not tree.uncertainty:
        result = replace(result, status="not_applicable")
    return result


def _unicode_string(value: object) -> str | None:
    return value[2:] if isinstance(value, str) and value.startswith("u:") else None


def _figure_alt(tree: _Structure) -> Finding:
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
    result = _result(
        "pdf.figure_alternative_text",
        "Checked nonempty Alt strings on tagged Figures.",
        violations,
        uncertain=uncertain,
        figure_count=figures,
        scope="Tagged Figure Alt presence only; image coverage and description quality "
        "are not established.",
    )
    if not figures and not uncertain:
        result = replace(result, status="not_applicable")
    return result


def _link_structure(tree: _Structure) -> Finding:
    violations: list[object] = []
    uncertainty = list(tree.uncertainty)
    associated: dict[str, list[tuple[int, str | None]]] = {}
    for owner, page, target in tree.object_references:
        if not isinstance(target, str) or not _REFERENCE.fullmatch(target):
            continue
        index: int | None = owner
        while index is not None and tree.elements[index].role != "/Link":
            index = tree.elements[index].parent
        if index is not None:
            associated.setdefault(target, []).append((index, page))

    def check_destination(raw: object, item: dict[str, object]) -> None:
        destination = tree.objects.resolve(raw)
        if destination is None or destination == [] or destination in ("u:", "b:", "/"):
            violations.append({**item, "reason": "link destination is empty"})
        elif isinstance(destination, list):
            if (
                len(destination) < 2
                or not isinstance(destination[0], str)
                or destination[0] not in tree.pages
            ):
                violations.append({**item, "reason": "local link destination lacks a valid page"})
        else:
            uncertainty.append("named or unsupported link destination is not resolved")

    annotations: list[object] = []
    encountered: set[str] = set()
    for page, number in tree.pages.items():
        page_object = tree.objects.dictionary(page)
        raw = tree.objects.resolve(page_object.get("/Annots", []))
        if not isinstance(raw, list) or len(raw) > _LIMIT:
            raise PreparationError("PDF page annotation array is malformed or exceeds bounds")
        for reference in raw:
            annotation = tree.objects.dictionary(reference)
            if annotation.get("/Subtype") != "/Link":
                continue
            if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
                uncertainty.append("direct link annotation lacks an indirect identity")
                continue
            item: dict[str, object] = {"page": number, "annotation": reference}
            annotations.append(item)
            if reference in encountered:
                violations.append({**item, "reason": "Link annotation is repeated in page arrays"})
            encountered.add(reference)
            owners = associated.get(reference, [])
            if len(owners) != 1:
                violations.append(
                    {**item, "reason": "link annotation needs exactly one Link tag association"}
                )
            elif owners[0][1] != page:
                violations.append({**item, "reason": "Link object reference has the wrong page"})
            if "/Dest" not in annotation and "/A" not in annotation:
                violations.append(
                    {**item, "reason": "link annotation has no destination or action"}
                )
            if "/Dest" in annotation:
                check_destination(annotation["/Dest"], item)
            if "/A" in annotation:
                action = tree.objects.dictionary(annotation["/A"])
                if action.get("/S") == "/URI":
                    target = _unicode_string(tree.objects.resolve(action.get("/URI")))
                    if target is None:
                        uncertainty.append("link URI is not a supported Unicode string")
                    elif not target.strip():
                        violations.append({**item, "reason": "link URI is empty"})
                elif action.get("/S") != "/GoTo":
                    uncertainty.append("link action is outside supported URI/GoTo inspection")
                elif "/D" not in action:
                    violations.append({**item, "reason": "GoTo link action has no destination"})
                else:
                    check_destination(action["/D"], item)
    for reference in associated:
        if reference not in encountered:
            violations.append(
                {"target": reference, "reason": "Link tag references no page Link annotation"}
            )
    linked_owners = {owner for owners in associated.values() for owner, _ in owners}
    for index, element in enumerate(tree.elements):
        if element.role == "/Link" and index not in linked_owners:
            violations.append(
                {**tree.location(index), "reason": "Link tag has no associated annotation"}
            )
    result = _result(
        "pdf.link_structure",
        "Checked Link tags against page Link annotations.",
        violations,
        uncertain=bool(uncertainty),
        annotations=annotations[:100],
        uncertainty=sorted(set(uncertainty)),
        scope="Object associations and supported target presence only; name adequacy, "
        "navigation and target availability are unverified.",
    )
    if not annotations and not linked_owners and not violations and not uncertainty:
        result = replace(result, status="not_applicable")
    return result


def _object_rules(options: StructureOptions) -> list[str]:
    return [
        rule
        for rule, selected in (
            ("pdf.table_structure", options.check_table_structure),
            ("pdf.link_structure", options.check_link_structure),
            ("pdf.figure_alternative_text", options.require_figure_alt),
            (
                "pdf.structure_references",
                options.require_structure_tree or options.check_structure_references,
            ),
        )
        if selected
    ]


def _object_findings(output: str, pages: int, options: StructureOptions) -> list[Finding]:
    rules = _object_rules(options)
    objects = _Objects(output, pages)
    if objects.root.get("/StructTreeRoot") is None:
        return [
            _result(
                rule,
                "The PDF catalog has no structure tree.",
                ["A structure tree is explicitly required"]
                if rule == "pdf.structure_references" and options.require_structure_tree
                else [],
                uncertain=not (
                    rule == "pdf.structure_references" and options.require_structure_tree
                ),
            )
            for rule in rules
        ]
    tree = _Structure(objects)
    checks = {
        "pdf.table_structure": lambda: _table_structure(tree),
        "pdf.link_structure": lambda: _link_structure(tree),
        "pdf.figure_alternative_text": lambda: _figure_alt(tree),
        "pdf.structure_references": lambda: _structure_references(
            tree, options.require_structure_tree
        ),
    }
    findings = []
    for rule in rules:
        try:
            findings.append(checks[rule]())
        except PreparationError as error:
            findings.append(_unavailable(rule, error))
    return findings


async def inspect_pdf_structure(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    options: StructureOptions,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect only selected line expectations and tagged PDF object structures."""
    object_rules = _object_rules(options)
    configured = (["pdf.expected_headings"] if options.heading_expectations else []) + object_rules
    if not configured:
        return []
    try:
        session = await _session(pdf, work, runner, budget)
        properties = await session.measure(
            "properties",
            ["pdfinfo", "-enc", "UTF-8", "@PDF@"],
            lambda result, _: _properties(result.stdout),
        )
        pages = _page_count(properties)
    except (PreparationError, OSError) as error:
        return [_unavailable(rule, error) for rule in configured]
    findings = []
    if options.heading_expectations:
        try:
            findings.extend(
                await session.measure(
                    "headings",
                    ["pdftotext", "-layout", "-enc", "UTF-8", "@PDF@", "-"],
                    lambda result, _: _heading_findings(result.stdout, pages, options),
                )
            )
        except (PreparationError, OSError) as error:
            findings.append(_unavailable("pdf.expected_headings", error))
    if object_rules:
        try:
            findings.extend(
                await session.measure(
                    "structure",
                    [
                        "qpdf",
                        "--json=2",
                        "--json-key=qpdf",
                        "--json-key=pages",
                        "--json-stream-data=none",
                        "@PDF@",
                    ],
                    lambda result, _: _object_findings(result.stdout, pages, options),
                )
            )
        except (PreparationError, OSError) as error:
            findings.extend(_unavailable(rule, error) for rule in object_rules)
    return findings
