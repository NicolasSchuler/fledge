"""Offline citation coverage and field checks over a manuscript's declared bibliography.

This module owns the checks that relate a selected manuscript to its bibliography: which
literal citations resolve to an entry, which entries nothing cites, and the configured
field checks and duplicate search over the entries of the declared resources. It decides
which documents and resources are in scope (the selected root, every root, or a lone
.bib directory) and what is uncertain about that scope. Only declared local resources
contribute to a selected manuscript's evidence.

It is a bounded lexical subset: it does not execute TeX, expand bibliography string
macros, apply a bibliography backend's inheritance rules, contact URLs or propose
edits. The checks of the .bib files alone are in ``bibliography_integrity``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .analysed_source import AnalysedSource, reuse_or_analyse
from .bibliography_duplicates import find_fuzzy_duplicates
from .bibliography_field_checks import check_entry_fields
from .bibliography_files import BibliographyBudget, read_declared_bibliographies
from .bibliography_index import EntryId, EntryIndex, LocatedEntry, index_entries, located_entries
from .bibliography_options import BibliographyOptions
from .bibliography_results import (
    SELECTION_UNAVAILABLE,
    ReportScope,
    ResultCollector,
    SummarySpec,
)
from .bibtex_syntax import (
    MAX_BIB_ENTRIES,
    MAX_RESOURCE_FILES,
    MAX_RESOURCES_TOTAL_BYTES,
    BibDocument,
)
from .cancellation import cancellation_point
from .citation_graph import (
    CitationGraph,
    Uncertainty,
    bounded_uncertainties,
    build_citation_graph,
    require_directory,
    uncertainty_messages,
)
from .errors import PreparationError
from .finding_text import more_suffix
from .findings import EvidenceKind, Finding
from .source_graph import UNPARSED_SOURCE_RULES, SourceGraph
from .tex_commands import ROOT_SUFFIXES

_MAX_ROOTS = 32
_MAX_LISTED_ENTRIES = 3
_DEFAULT_OPTIONS = BibliographyOptions()


def _resource_budget() -> BibliographyBudget:
    """Return the budget the declared resources of one manuscript share."""
    return BibliographyBudget(
        max_total_bytes=MAX_RESOURCES_TOTAL_BYTES,
        max_files=MAX_RESOURCE_FILES,
        max_records=MAX_BIB_ENTRIES,
    )


def _load_resources(root: Path, names: set[str]) -> tuple[list[BibDocument], list[Uncertainty]]:
    """Parse the declared resources within the shared resource-set budget.

    Args:
        root: The project root.
        names: The declared resource paths, relative to ``root``.

    Returns:
        The parsed documents, and the gaps: each loading problem and each document whose
        syntax is incomplete.
    """
    load = read_declared_bibliographies(root, names, _resource_budget())
    uncertainties = [Uncertainty(problem.message) for problem in load.problems]
    uncertainties.extend(
        Uncertainty(f"{document.path}: bibliography syntax is incomplete")
        for document in load.documents
        if not document.valid
    )
    return load.documents, uncertainties


def _missing_citations(
    graph: CitationGraph, index: EntryIndex, uncertain: list[Uncertainty]
) -> list[Finding]:
    """Report each literal citation whose key has no entry and no inline definition.

    Args:
        graph: The citation graph of one root.
        index: The entries of the resources that root declares.
        uncertain: What is uncertain about the root's coverage; any uncertainty makes a
            missing key inconclusive instead of failed.

    Returns:
        One finding per unresolved citation.
    """
    details = uncertainty_messages(bounded_uncertainties(uncertain))
    missing: list[Finding] = []
    for key, path, line in graph.citations:
        cancellation_point()
        if key in index.keys or key in graph.inline_keys:
            continue
        missing.append(
            Finding(
                "bibliography.citation_coverage",
                f"Citation key {key!r} has no entry in this root's inspected resources.",
                severity="warning" if uncertain else "error",
                status="inconclusive" if uncertain else "failed",
                path=path,
                line=line,
                evidence_kind=EvidenceKind.DERIVED,
                details={
                    "key": key,
                    "document": graph.main,
                    "resources": sorted(graph.resources),
                    "uncertainty": details,
                },
            )
        )
    return missing


def _uncited_entries(
    references: list[LocatedEntry],
    used: set[EntryId],
    uncertainties: list[Uncertainty],
    results: ResultCollector,
) -> None:
    """Add one aggregate finding for the entries nothing reaches, and its summary.

    Args:
        references: Every entry of the inspected resources.
        used: The entries a citation, ``nocite`` or relationship reaches.
        uncertainties: What is uncertain about reachability; any uncertainty makes the
            finding inconclusive instead of failed.
        results: The collector that receives the finding and the summary.
    """
    candidates = [item for item in references if item.identity not in used]
    unused: list[Finding] = []
    if candidates:
        cancellation_point()
        # One aggregate keeps a shared master bibliography readable; every
        # candidate stays individually addressable through the details.
        rows = [
            {
                "entry": item.entry.key,
                "entry_type": item.entry.entry_type,
                "path": item.document.path,
                "line": item.document.line(item.entry.start),
            }
            for item in candidates
        ]
        listed = ", ".join(item.entry.key for item in candidates[:_MAX_LISTED_ENTRIES])
        unused.append(
            Finding(
                "bibliography.uncited_entries",
                f"{len(candidates)} of {len(references)} entries have no scanned use: "
                + listed
                + more_suffix(len(candidates) - _MAX_LISTED_ENTRIES),
                severity="warning",
                status="inconclusive" if uncertainties else "failed",
                path=candidates[0].document.path,
                line=candidates[0].document.line(candidates[0].entry.start),
                evidence_kind=EvidenceKind.HEURISTIC,
                incomplete=bool(uncertainties),
                details=results.scope.details(
                    entries=rows,
                    uncertainty=uncertainty_messages(bounded_uncertainties(uncertainties)),
                ),
            )
        )
    spec = SummarySpec("uncited_entries", "Citation and relationship reachability")
    results.summarize(spec, unused, len(references), uncertainties)


def _coverage(
    graphs: list[CitationGraph],
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: ResultCollector,
) -> None:
    """Run the citation coverage and uncited-entry checks that ``options`` enable.

    Keys resolve per root, against only the resources that root declares; the entries
    reached are united over every root.

    Args:
        graphs: The citation graph of each document in scope.
        references: Every entry of the inspected resources.
        uncertainties: The gaps that weaken coverage.
        options: Which checks run.
        results: The collector that receives the findings and summaries.
    """
    used: set[EntryId] = set()
    unused_uncertainties = list(uncertainties)
    for graph in graphs:
        # Keys resolve per root, against only the resources that root declares.
        index = index_entries(
            [item for item in references if item.document.path in graph.resources]
        )
        uncertain = [*uncertainties, *graph.uncertainties, *index.uncertainties]
        unused_uncertainties.extend([*uncertain, *graph.unused_uncertainties])
        if options.check_citation_coverage:
            spec = SummarySpec(
                "citation_coverage",
                "Supported literal citation coverage",
                details=results.scope.details(
                    document=graph.main, resources=sorted(graph.resources)
                ),
            )
            results.summarize(
                spec, _missing_citations(graph, index, uncertain), len(graph.citations), uncertain
            )
        used.update(index.reachable(graph))
    if options.check_uncited_entries:
        _uncited_entries(references, used, unused_uncertainties, results)


def _all_root_graphs(
    root: Path, inspection: SourceGraph, selected: CitationGraph, uncertainties: list[Uncertainty]
) -> list[CitationGraph]:
    """Build one citation graph per discovered root, up to ``_MAX_ROOTS``.

    Args:
        root: The project root.
        inspection: The literal source graph whose roots are discovered.
        selected: The citation graph of ``inspection``, reused for its own main file.
        uncertainties: Extended in place with the roots that could not be parsed and an
            excess of roots.

    Returns:
        The graphs, or ``[selected]`` when no root was discovered.
    """
    uncertainties.extend(
        Uncertainty(f"All-root discovery is incomplete: {item.path}: {item.message}")
        for item in inspection.findings
        if item.path
        and Path(item.path).suffix.lower() in ROOT_SUFFIXES
        and item.rule in UNPARSED_SOURCE_RULES
    )
    if len(inspection.roots) > _MAX_ROOTS:
        uncertainties.append(Uncertainty(f"Document root count exceeds {_MAX_ROOTS}"))
    graphs: list[CitationGraph] = []
    for main in inspection.roots[:_MAX_ROOTS]:
        cancellation_point()
        graphs.append(
            selected
            if inspection.main == main
            else build_citation_graph(SourceGraph.build(root, main))
        )
    return graphs or [selected]


@dataclass(slots=True)
class _EvidenceScope:
    """The documents and resources in scope, and what is uncertain about them.

    Coverage and the field checks differ in what limits them: a bibliography-only
    directory has no citation scope, so graph gaps weaken coverage but not the field
    checks, which have their own list of gaps.

    Attributes:
        graphs: The citation graph of each document in scope.
        names: The bibliography resource paths in scope.
        description: The scope as the report describes it.
        coverage_gaps: What weakens citation coverage and reachability.
        field_gaps: What weakens the field checks and the duplicate search.
    """

    graphs: list[CitationGraph]
    names: set[str]
    description: str
    coverage_gaps: list[Uncertainty]
    field_gaps: list[Uncertainty]

    def add_input_gaps(self, gaps: list[Uncertainty]) -> None:
        """Record gaps in the bibliography files themselves, which weaken every check."""
        self.coverage_gaps.extend(gaps)
        self.field_gaps.extend(gaps)


def _select_scope(
    root: Path, main: str | None, options: BibliographyOptions, inspection: SourceGraph
) -> _EvidenceScope:
    """Choose the graphs and resource names the checks describe, and why they may be partial.

    A standalone bibliography directory (no root and no main file) that declares no
    resource covers every local .bib file.

    Args:
        root: The project root.
        main: The selected main file, or ``None``.
        options: Whether every root is in scope.
        inspection: The literal source graph of ``root`` and ``main``.

    Returns:
        The evidence scope.
    """
    graphs = [build_citation_graph(inspection)]
    discovery_gaps: list[Uncertainty] = []
    if options.all_roots:
        graphs = _all_root_graphs(root, inspection, graphs[0], discovery_gaps)
    names: set[str] = set().union(*(graph.resources for graph in graphs))
    field_gaps = list(discovery_gaps)
    graph_gaps = [item for graph in graphs for item in graph.uncertainties]
    standalone = not inspection.roots and main is None
    if not names and standalone:
        # A standalone .bib command has no manuscript resource selection. Its
        # explicitly stated metadata scope is every local bibliography file.
        names = {name for name in inspection.files if Path(name).suffix.lower() == ".bib"}
    elif not names and graph_gaps:
        # Derived only from the graph uncertainties, so it restates nothing new.
        field_gaps.append(
            Uncertainty(
                SELECTION_UNAVAILABLE,
                already_reported=all(item.already_reported for item in graph_gaps),
            )
        )
    if not standalone:
        field_gaps.extend(graph_gaps)
    if standalone:
        description = "bibliography-only directory; citation scope unavailable"
    elif options.all_roots:
        description = "all literal document roots"
    else:
        description = "selected literal document graph"
    return _EvidenceScope(graphs, names, description, discovery_gaps, field_gaps)


def check_bibliography_coverage_and_fields(
    root: Path,
    main: str | None = None,
    options: BibliographyOptions = _DEFAULT_OPTIONS,
    *,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Inspect offline evidence without writing files or resolving remote metadata.

    Metadata in a bibliography-only directory can be inspected without a TeX root;
    citation coverage then remains inconclusive. With TeX roots, only selected
    resources are checked. ``all_roots`` unions usage but resolves keys separately
    for each root. Passed summaries describe only the supported literal subset.

    Args:
        root: The project root.
        main: The selected main file, or ``None``.
        options: Which checks run and their settings.
        analysed: The source analysis of the same root and main file shared with other
            checks; without one the checks build their own.

    Returns:
        The findings in report order, or none when no check is enabled.

    Raises:
        PreparationError: ``options`` is not a ``BibliographyOptions``, or ``root`` is
            not a directory.
    """
    if not isinstance(options, BibliographyOptions):
        raise PreparationError("Bibliography checks require BibliographyOptions")
    if not options.any_enabled:
        return []
    require_directory(root, "Bibliography input")
    scope = _select_scope(root, main, options, reuse_or_analyse(root, main, analysed).graph)
    documents, input_gaps = _load_resources(root, scope.names)
    scope.add_input_gaps(input_gaps)
    references = located_entries(document for document in documents if document.valid)
    results = ResultCollector(
        ReportScope(
            scope.description,
            tuple(graph.main for graph in scope.graphs if graph.main),
            tuple(sorted(scope.names)),
        )
    )
    _coverage(scope.graphs, references, scope.coverage_gaps, options, results)
    check_entry_fields(references, scope.field_gaps, options, results)
    if options.check_fuzzy_duplicates:
        find_fuzzy_duplicates(references, scope.field_gaps, options, results)
    return results.report()
