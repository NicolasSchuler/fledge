"""Literal citations of one selected manuscript and the bibliography entries they reach.

Shared by bibliography checks, bibliography transformations, generated-bibliography
(.bbl) checks and online lookups. Only the bounded literal source graph is read:
TeX is not executed, macros are not expanded, and a bibliography backend's
inheritance, filters and reference sections are not applied. Constructs outside
that subset become uncertainties instead of evidence.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .bibtex_syntax import BibDocument, Entry, field_literal
from .cancellation import cancellation_point
from .errors import PreparationError
from .expanded_document import commands_before_end, reachable_sources
from .findings import Finding
from .source_graph import ArgumentLocation, ParsedSource, SourceGraph
from .tex_lexer import Command, conditional_spans, in_conditional, split_argument_items

CITATION_COMMANDS = frozenset(
    "cite citep citet citealp citealt citeauthor citeyear citeyearpar "
    "parencite textcite autocite footcite footcitetext smartcite supercite "
    "fullcite footfullcite notecite pnotecite fnotecite citeurl citetitle nocite".split()
)
MULTICITE_COMMANDS = frozenset(
    "cites parencites textcites autocites footcites footcitetexts smartcites supercites".split()
)
_RESOURCE_COMMANDS = frozenset({"bibliography", "addbibresource"})
# Entry fields that name other entries' keys.
RELATIONSHIP_FIELDS = frozenset({"crossref", "xref", "xdata", "related", "entryset"})
# Fields holding literal key lists: relationships and biblatex's alternate keys.
KEY_LIST_FIELDS = RELATIONSHIP_FIELDS | {"ids"}
# Source findings that leave the selected literal graph incomplete.
_GRAPH_FAILURE_RULES = frozenset(
    {
        "source.encoding",
        "source.size_limit",
        "source.argument_limit",
        "source.command_limit",
        "source.literal_unclosed",
        "source.input_cycle",
        "source.input_depth",
        "source.expansion_limit",
        "source.dynamic_path",
        "source.nested_file_command",
        "source.conditional_dependency",
        "source.path_literal_context",
        "source.external_path",
        "source.ambiguous_dependency",
        "source.case_mismatch",
        "source.custom_path",
        "source.unsupported_command",
        "source.missing_dependency",
        "source.link",
        "source.context_ambiguity",
        "source.subfile_structure",
        "source.font_options",
    }
)
_MAX_LISTED_UNCERTAINTIES = 32
_DEFINITIONS = frozenset(
    {
        "newcommand",
        "renewcommand",
        "providecommand",
        "DeclareRobustCommand",
        "NewDocumentCommand",
        "RenewDocumentCommand",
        "ProvideDocumentCommand",
        "DeclareDocumentCommand",
        "def",
        "gdef",
        "edef",
        "xdef",
        "let",
        "futurelet",
    }
)
# Expansion, category codes and bibliography-backend declarations can reach any
# citation; no lexical argument inspection bounds what they may later execute.
_EXECUTION = frozenset(
    {
        "csname",
        "catcode",
        "directlua",
        "DeclareCiteCommand",
        "DeclareMultiCiteCommand",
        "DeclareSourcemap",
        "DeclareStyleSourcemap",
        "DeclareDatamodelEntryfields",
    }
)
_DYNAMIC = _DEFINITIONS | _EXECUTION
# Substrings that make a command name or a definition body citation-relevant. A
# plain \newcommand{\R}{\mathbb{R}} cannot influence bibliography evidence.
_CITATION_MARKERS = ("cite", "nocite", "bibitem", "addbibresource", "bibliography")
_FILTERS = frozenset(
    {
        "defbibfilter",
        "defbibcheck",
        "DeclareBibliographyCategory",
        "addtocategory",
        "DeclareEntrySet",
        "includeonly",
        "refsection",
        "refsegment",
    }
)
# Source findings that leave no selected document; each is a blocking error.
_ROOT_FAILURES = frozenset({"source.root_ambiguous", "source.root_missing", "source.main_invalid"})


@dataclass(frozen=True, slots=True)
class Uncertainty:
    """Why evidence is incomplete; ``already_reported`` marks a restated source error.

    A blocking source error is reported by the source checks themselves, so a
    bibliography summary whose only uncertainty restates it can be collapsed.
    """

    message: str
    already_reported: bool = False


def bounded_uncertainties(items: Iterable[Uncertainty]) -> list[Uncertainty]:
    """Distinct messages in sorted order; beyond the listing limit, only a count remains.

    A message is already reported when any of its occurrences is.
    """
    reported: dict[str, bool] = {}
    for item in items:
        reported[item.message] = reported.get(item.message, False) or item.already_reported
    unique = [Uncertainty(message, reported[message]) for message in sorted(reported)]
    if len(unique) > _MAX_LISTED_UNCERTAINTIES:
        omitted = len(unique) - _MAX_LISTED_UNCERTAINTIES
        return [
            *unique[:_MAX_LISTED_UNCERTAINTIES],
            Uncertainty(f"{omitted} further uncertainties omitted"),
        ]
    return unique


def uncertainty_messages(items: Iterable[Uncertainty]) -> list[str]:
    return [item.message for item in items]


class Citation(NamedTuple):
    """One literal citation key and the source line that cites it."""

    key: str
    path: str
    line: int


@dataclass(slots=True)
class CitationGraph:
    """Literal citation evidence of one selected main source."""

    main: str | None
    resources: set[str] = field(default_factory=set)
    citations: list[Citation] = field(default_factory=list)
    # Keys defined by \bibitem in the sources rather than by a bibliography resource.
    inline_keys: set[str] = field(default_factory=set)
    nocite_all: bool = False
    # Gaps in the citation evidence itself.
    uncertainties: list[Uncertainty] = field(default_factory=list)
    # Further gaps that only weaken claims that an entry is unused.
    unused_uncertainties: list[Uncertainty] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return uncertainty_messages(self.uncertainties)

    @property
    def unused_reasons(self) -> list[str]:
        return uncertainty_messages(self.unused_uncertainties)


def resource_declarations(
    inspection: SourceGraph, selected: set[str]
) -> tuple[set[str], list[str]]:
    """Resolve literal bibliography resource declarations in the selected sources."""
    resources: set[str] = set()
    reasons: list[str] = []
    for name in sorted(selected):
        cancellation_point()
        for command in commands_before_end(inspection.sources[name]):
            if command.name not in _RESOURCE_COMMANDS or not command.arguments:
                continue
            for argument in split_argument_items(
                command.arguments[0], comma_separated=command.name == "bibliography"
            ):
                target = inspection.references.get(
                    ArgumentLocation(name, argument.start, argument.end)
                )
                if target and target[1] in _RESOURCE_COMMANDS:
                    resources.add(target[0])
                else:
                    reasons.append(
                        f"{name}:{command.line}: bibliography resource could not be resolved"
                    )
    return resources, reasons


class StyleReference(NamedTuple):
    """A ``\\bibliographystyle`` name and the project .bst file it resolved to, if any."""

    name: str
    local_file: str | None


class BibliographySelection(NamedTuple):
    """What one selected main source declares about its bibliography.

    ``resources`` is None when the whole tree is in scope. ``styles`` is None when no
    selected document could be read, so the BibTeX style in use is unknown.
    """

    resources: set[str] | None
    styles: list[StyleReference] | None


def bibliography_styles(inspection: SourceGraph, selected: set[str]) -> list[StyleReference]:
    """The BibTeX styles named by the selected sources, in source order."""
    styles: list[StyleReference] = []
    for name in sorted(selected):
        cancellation_point()
        for command in commands_before_end(inspection.sources[name]):
            if command.name != "bibliographystyle" or not command.arguments:
                continue
            argument = command.arguments[0]
            target = inspection.references.get(ArgumentLocation(name, argument.start, argument.end))
            local = target[0] if target and target[1] == "bibliographystyle" else None
            styles.append(StyleReference(argument.value.strip(), local))
    return styles


def selected_bibliography(root: Path, main: str | None) -> BibliographySelection:
    """Resources and styles one selected literal graph declares; None where unavailable.

    An unselectable main source or a declaration that cannot be resolved keeps the
    documented whole-tree scope instead of silently narrowing the inspected files.
    """
    if main is None:
        return BibliographySelection(None, None)
    inspection = SourceGraph.build(root, main)
    if inspection.main is None:
        return BibliographySelection(None, None)
    selected = reachable_sources(inspection)
    resources, _ = resource_declarations(inspection, selected)
    return BibliographySelection(resources or None, bibliography_styles(inspection, selected))


def require_directory(root: Path, purpose: str) -> None:
    """Refuse a project root that is not a directory, naming ``purpose`` and the fix."""
    if not root.is_dir():
        raise PreparationError(
            f"{purpose} must be a directory, but {root} is not. "
            "Pass the project folder that holds the manuscript and its .bib files."
        )


def graph_failures(inspection: SourceGraph, selected: set[str]) -> list[Finding]:
    """Unresolved source findings that leave the selected sources incomplete."""
    scope = selected | {inspection.main}
    # A resolved lookup-order or conditional dependency is evidence, not a gap.
    return [
        item
        for item in inspection.findings
        if item.path in scope and item.rule in _GRAPH_FAILURE_RULES and item.status != "passed"
    ]


def citation_keys(value: str) -> list[str] | None:
    """The keys of a literal comma-separated key list, or None if any key is not literal."""
    keys = [part.strip() for part in value.split(",")]
    if not keys or any(
        not key or any(char.isspace() or char in "\\{}#$^~&%\x00" for char in key) for key in keys
    ):
        return None
    return keys


def _citation_marked(text: str) -> bool:
    """Whether a command name or definition body can reach citation machinery."""
    lowered = text.lower()
    return any(marker in lowered for marker in _CITATION_MARKERS)


def _execution_gaps(command: Command, spans: list[tuple[int, int]]) -> Iterator[str]:
    """Constructs around one command that lexical inspection cannot follow."""
    if command.name == "subfile":
        yield "subfile bibliography content context is unsupported"
    if _citation_marked(command.name) and in_conditional(spans, command.start):
        yield "conditional citation or bibliography execution is not expanded"
    if command.name in _DEFINITIONS and _citation_marked(
        " ".join(item.value for item in (*command.arguments, *command.options))
    ):
        yield "a macro definition of citation markup is not expanded"
    if command.name in _EXECUTION:
        yield "macro or category-code execution is not expanded"
    if command.name == "endinput":
        # Tokens on its physical line may still run, even though lexical
        # traversal deliberately stops before them.
        yield "endinput token buffering is unverified"
    if command.name in _FILTERS or (
        command.name == "begin"
        and command.arguments
        and command.arguments[0].value.strip() in {"refsection", "refsegment"}
    ):
        yield "bibliography scope/filter is unsupported"


def _read_citation(
    graph: CitationGraph, name: str, source: ParsedSource, command: Command
) -> Iterator[str]:
    """Record the literal keys of a citation or \\bibitem; yield what stays unverified."""
    lowered = command.name.lower()
    multicite = lowered in MULTICITE_COMMANDS
    inline = command.name == "bibitem"
    if not inline and lowered not in CITATION_COMMANDS and not multicite:
        if "cite" in lowered and command.name not in _DYNAMIC:
            yield f"unsupported citation-like command \\{command.name}"
        return
    arguments = command.arguments if multicite else command.arguments[:1]
    if not arguments:
        yield "citation has no supported key argument"
    if command.depth:
        yield "grouped citation execution is unverified"
    if multicite and len(command.arguments) + len(command.options) >= 6:
        yield "multicite exceeds lexical argument coverage"
    for argument in arguments:
        keys = citation_keys(argument.value)
        if keys is None or source.text[argument.start : argument.end] != argument.value:
            yield "citation key contains dynamic markup"
            continue
        for key in keys:
            if key == "*" and lowered == "nocite":
                graph.nocite_all = True
            elif inline:
                graph.inline_keys.add(key)
            else:
                graph.citations.append(Citation(key, name, command.line))


def build_citation_graph(inspection: SourceGraph) -> CitationGraph:
    """Collect the literal citations, resources and evidence gaps of the selected main."""
    graph = CitationGraph(inspection.main)
    selected = reachable_sources(inspection)
    if inspection.main not in selected:
        graph.uncertainties.append(
            Uncertainty(
                "A unique readable main source could not be selected",
                already_reported=not inspection.main
                and any(
                    item.rule in _ROOT_FAILURES and item.is_blocking for item in inspection.findings
                ),
            )
        )
    if not inspection.main:
        return graph
    uncertainties = [
        *graph.uncertainties,
        *(
            Uncertainty(f"{item.path}: {item.message}", already_reported=item.is_blocking)
            for item in graph_failures(inspection, selected)
        ),
    ]
    resources, resource_reasons = resource_declarations(inspection, selected)
    graph.resources.update(resources)
    uncertainties.extend(map(Uncertainty, resource_reasons))
    unused: list[Uncertainty] = []
    for name in sorted(selected):
        cancellation_point()
        source = inspection.sources[name]
        spans = conditional_spans(source.commands)
        for command in commands_before_end(source):
            gaps = [*_execution_gaps(command, spans), *_read_citation(graph, name, source, command)]
            uncertainties.extend(Uncertainty(f"{name}:{command.line}: {gap}") for gap in gaps)
            if command.name == "printbibliography" and command.options:
                unused.append(
                    Uncertainty(
                        f"{name}:{command.line}: bibliography print filters/options "
                        "are not interpreted"
                    )
                )
    graph.uncertainties = bounded_uncertainties(uncertainties)
    graph.unused_uncertainties = bounded_uncertainties(unused)
    return graph


@dataclass(frozen=True, slots=True)
class LocatedEntry:
    """A keyed entry and the document that holds it."""

    document: BibDocument
    entry: Entry

    @property
    def identity(self) -> tuple[str, int]:
        return self.document.path, self.entry.start


def located_entries(documents: Iterable[BibDocument]) -> list[LocatedEntry]:
    return [LocatedEntry(document, entry) for document in documents for entry in document.entries]


@dataclass(frozen=True, slots=True)
class EntryIndex:
    """Keys, aliases and literal relationships of a set of entries."""

    entries: list[LocatedEntry]
    # Every key and ids alias, mapped to the entries it identifies.
    keys: dict[str, list[LocatedEntry]]
    # Keys each entry's relationship fields name; entry-set members point back to their set.
    relationships: dict[tuple[str, int], set[str]]
    uncertainties: list[Uncertainty]

    def reachable(self, graph: CitationGraph) -> set[tuple[str, int]]:
        """Identities reached from the graph's citations, \\nocite{*} and relationships."""
        queue: deque[LocatedEntry] = deque(self.entries if graph.nocite_all else [])
        for citation in graph.citations:
            queue.extend(self.keys.get(citation.key, []))
        reached: set[tuple[str, int]] = set()
        while queue:
            cancellation_point()
            reference = queue.popleft()
            if reference.identity in reached:
                continue
            reached.add(reference.identity)
            for target in self.relationships.get(reference.identity, set()):
                queue.extend(self.keys.get(target, []))
        return reached


def index_entries(references: list[LocatedEntry]) -> EntryIndex:
    keys: dict[str, list[LocatedEntry]] = defaultdict(list)
    relationships: dict[tuple[str, int], set[str]] = defaultdict(set)
    reasons: list[str] = []
    sets: list[tuple[str, list[str]]] = []
    for reference in references:
        cancellation_point()
        entry = reference.entry
        keys[entry.key].append(reference)
        for item in entry.fields:
            if item.name not in KEY_LIST_FIELDS:
                continue
            value = field_literal(item)
            targets = citation_keys(value) if value is not None else None
            if targets is None:
                reasons.append(f"{reference.document.path}: {entry.key} has dynamic {item.name}")
            elif item.name == "ids":
                for target in targets:
                    keys[target].append(reference)
            else:
                relationships[reference.identity].update(targets)
                if item.name == "entryset":
                    sets.append((entry.key, targets))
    for key, targets in keys.items():
        if len({target.identity for target in targets}) > 1:
            reasons.append(f"Citation key or alias {key!r} identifies multiple entries")
    for targets in relationships.values():
        for key in targets:
            if key not in keys:
                reasons.append(f"Bibliography relationship target {key!r} is unresolved")
    # A backend may render a set when one member is cited. Retaining both
    # directions avoids calling its set or sibling entries definitely unused.
    for key, members in sets:
        for member in members:
            for reference in keys.get(member, []):
                relationships[reference.identity].add(key)
    return EntryIndex(
        references,
        dict(keys),
        dict(relationships),
        bounded_uncertainties(map(Uncertainty, reasons)),
    )
