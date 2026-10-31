"""Literal citations, bibliography declarations and styles of one selected manuscript.

This module owns what the TeX side says: which keys the selected sources cite, which
bibliography resources and BibTeX styles they declare, and which constructs make that
evidence incomplete (``Uncertainty``). It is shared by bibliography checks,
transformations, generated-bibliography (.bbl) checks and online lookups. Only the
bounded literal source graph is read: TeX is not executed, macros are not expanded,
and a bibliography backend's inheritance, filters and reference sections are not
applied. Constructs outside that subset become uncertainties instead of evidence.
What the .bib files say about themselves lives in ``bibliography_index``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import cancellation_point
from .errors import PreparationError
from .expanded_document import commands_before_end, reachable_sources
from .findings import Finding
from .source_graph import (
    INCOMPLETE_GRAPH_RULES,
    ROOT_AMBIGUOUS_RULE,
    ROOT_MISSING_RULE,
    ArgumentLocation,
    SourceGraph,
)
from .source_loading import ParsedSource
from .tex_commands import (
    CITATION_COMMANDS,
    COMMAND_DEFINITION_COMMANDS,
    MULTICITE_COMMANDS,
    PRIMITIVE_DEFINITION_COMMANDS,
)
from .tex_lexer import Argument, Command, conditional_spans, in_conditional, split_argument_items

_RESOURCE_COMMANDS = frozenset({"bibliography", "addbibresource"})
# Entry fields that name other entries' keys.
RELATIONSHIP_FIELDS = frozenset({"crossref", "xref", "xdata", "related", "entryset"})
# Fields holding literal key lists: relationships and biblatex's alternate keys.
KEY_LIST_FIELDS = RELATIONSHIP_FIELDS | {"ids"}
_MAX_LISTED_UNCERTAINTIES = 32
# A multicite command with this many arguments and options together goes beyond what the
# lexical scan reads reliably.
_MULTICITE_LEXICAL_LIMIT = 6
_DEFINITIONS = COMMAND_DEFINITION_COMMANDS | PRIMITIVE_DEFINITION_COMMANDS
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
_ROOT_FAILURES = frozenset({ROOT_AMBIGUOUS_RULE, ROOT_MISSING_RULE, "source.main_invalid"})


@dataclass(frozen=True, slots=True)
class Uncertainty:
    """Why evidence is incomplete; ``already_reported`` marks a restated source error.

    A blocking source error is reported by the source checks themselves, so a
    bibliography summary whose only uncertainty restates it can be collapsed.

    Attributes:
        message: What makes the evidence incomplete.
        already_reported: Whether the message restates a blocking source error.
    """

    message: str
    already_reported: bool = False


def bounded_uncertainties(items: Iterable[Uncertainty]) -> list[Uncertainty]:
    """Merge uncertainties into distinct messages in sorted order, up to a listing limit.

    Beyond the listing limit, only a count remains. A message is already reported when
    any of its occurrences is.

    Args:
        items: The uncertainties to merge.

    Returns:
        The distinct uncertainties sorted by message, with those beyond the listing limit
        replaced by one that counts them.
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
    """List the messages of the uncertainties."""
    return [item.message for item in items]


class Citation(NamedTuple):
    """One literal citation key and the source line that cites it.

    Attributes:
        key: The cited key.
        path: The source that cites it.
        line: The line of the citation.
    """

    key: str
    path: str
    line: int


@dataclass(slots=True)
class CitationGraph:
    """Literal citation evidence of one selected main source.

    Attributes:
        main: The selected main source, if any.
        resources: The bibliography resources the selected sources declare.
        citations: The literal citations.
        inline_keys: The keys that ``bibitem`` commands in the sources define rather than
            a bibliography resource.
        nocite_all: Whether a ``nocite`` of ``*`` cites every entry.
        uncertainties: The gaps in the citation evidence itself.
        unused_uncertainties: Further gaps that only weaken claims that an entry is
            unused.
    """

    main: str | None
    resources: set[str] = field(default_factory=set)
    citations: list[Citation] = field(default_factory=list)
    inline_keys: set[str] = field(default_factory=set)
    nocite_all: bool = False
    uncertainties: list[Uncertainty] = field(default_factory=list)
    unused_uncertainties: list[Uncertainty] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """The messages of ``uncertainties``."""
        return uncertainty_messages(self.uncertainties)

    @property
    def unused_reasons(self) -> list[str]:
        """The messages of ``unused_uncertainties``."""
        return uncertainty_messages(self.unused_uncertainties)


def resource_declarations(
    inspection: SourceGraph, selected: set[str]
) -> tuple[set[str], list[str]]:
    """Resolve literal bibliography resource declarations in the selected sources.

    Args:
        inspection: The source graph of the manuscript.
        selected: The selected sources, by relative path.

    Returns:
        The resolved resources, and a reason for each declaration that could not be
        resolved.
    """
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
    r"""A ``\bibliographystyle`` name and the project .bst file it resolved to, if any.

    Attributes:
        name: The style name, without surrounding white space.
        local_file: The project .bst file it resolved to, or ``None``.
    """

    name: str
    local_file: str | None


class BibliographySelection(NamedTuple):
    """What one selected main source declares about its bibliography.

    Attributes:
        resources: The declared bibliography resources; ``None`` when the whole tree is in
            scope.
        styles: The named BibTeX styles; ``None`` when no selected document could be read, so
            the BibTeX style in use is unknown.
    """

    resources: set[str] | None
    styles: list[StyleReference] | None


def bibliography_styles(inspection: SourceGraph, selected: set[str]) -> list[StyleReference]:
    """List the BibTeX styles named by the selected sources, in source order.

    Args:
        inspection: The source graph of the manuscript.
        selected: The selected sources, by relative path.

    Returns:
        A reference for each style command, with the project .bst file it resolved to.
    """
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


def selected_bibliography(
    root: Path, main: str | None, *, analysed: AnalysedSource | None = None
) -> BibliographySelection:
    """Find the resources and styles that one selected literal graph declares.

    An unselectable main source or a declaration that cannot be resolved keeps the
    documented whole-tree scope instead of silently narrowing the inspected files.

    Args:
        root: The project directory.
        main: The main source, relative to ``root``, or ``None`` when none is selected.
        analysed: A source analysis of the same root and main file shared with other
            checks; without one the graph is built here.

    Returns:
        The declared resources and styles; ``None`` where unavailable.
    """
    if main is None:
        return BibliographySelection(None, None)
    inspection = reuse_or_analyse(root, main, analysed).graph
    if inspection.main is None:
        return BibliographySelection(None, None)
    selected = reachable_sources(inspection)
    resources, _ = resource_declarations(inspection, selected)
    return BibliographySelection(resources or None, bibliography_styles(inspection, selected))


def require_directory(root: Path, purpose: str) -> None:
    """Refuse a project root that is not a directory, naming ``purpose`` and the fix.

    Args:
        root: The project root.
        purpose: What the directory is for, named in the error.

    Raises:
        PreparationError: ``root`` is not a directory.
    """
    if not root.is_dir():
        raise PreparationError(
            f"{purpose} must be a directory, but {root} is not. "
            "Pass the project folder that holds the manuscript and its .bib files."
        )


def graph_failures(inspection: SourceGraph, selected: set[str]) -> list[Finding]:
    """List the unresolved source findings that leave the selected sources incomplete.

    Args:
        inspection: The source graph of the manuscript.
        selected: The selected sources, by relative path.

    Returns:
        The findings about the selected sources or the main file whose rules mark an
        incomplete graph and that did not pass.
    """
    scope = selected | {inspection.main}
    # A resolved lookup-order or conditional dependency is evidence, not a gap.
    return [
        item
        for item in inspection.findings
        if item.path in scope and item.rule in INCOMPLETE_GRAPH_RULES and item.status != "passed"
    ]


def citation_keys(value: str) -> list[str] | None:
    """Split a literal comma-separated key list; return ``None`` if any key is not literal."""
    keys = [part.strip() for part in value.split(",")]
    if not keys or any(
        not key or any(char.isspace() or char in "\\{}#$^~&%\x00" for char in key) for key in keys
    ):
        return None
    return keys


def _citation_marked(text: str) -> bool:
    """Return whether a command name or definition body can reach citation machinery."""
    lowered = text.lower()
    return any(marker in lowered for marker in _CITATION_MARKERS)


def _execution_gaps(command: Command, spans: list[tuple[int, int]]) -> Iterator[str]:
    """Describe the constructs around one command that lexical inspection cannot follow.

    Args:
        command: The command to inspect.
        spans: The conditional spans of its source.

    Yields:
        A description of each construct that is not followed.
    """
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


def _citation_shape_gaps(
    command: Command, arguments: Sequence[Argument], *, multicite: bool
) -> Iterator[str]:
    """Describe what is unverified about the arguments and placement of a citation command.

    Args:
        command: The citation command.
        arguments: The arguments that hold its keys.
        multicite: Whether the command is a multicite command.

    Yields:
        A description of each unverified aspect.
    """
    if not arguments:
        yield "citation has no supported key argument"
    if command.depth:
        yield "grouped citation execution is unverified"
    if multicite and len(command.arguments) + len(command.options) >= _MULTICITE_LEXICAL_LIMIT:
        yield "multicite exceeds lexical argument coverage"


def _read_citation(
    graph: CitationGraph, name: str, source: ParsedSource, command: Command
) -> Iterator[str]:
    r"""Record the literal keys of a citation or \bibitem; yield what stays unverified.

    A ``nocite`` of ``*`` sets ``nocite_all``, and the keys of a ``bibitem`` become
    inline keys. A command that is neither is read only to report an unsupported
    citation-like command.

    Args:
        graph: The graph that receives the keys.
        name: The source that holds the command, by relative path.
        source: The parsed source.
        command: The command to read.

    Yields:
        A description of each unverified aspect of the command.
    """
    lowered = command.name.lower()
    multicite = lowered in MULTICITE_COMMANDS
    inline = command.name == "bibitem"
    if not inline and lowered not in CITATION_COMMANDS and not multicite:
        if "cite" in lowered and command.name not in _DYNAMIC:
            yield f"unsupported citation-like command \\{command.name}"
        return
    arguments = command.arguments if multicite else command.arguments[:1]
    yield from _citation_shape_gaps(command, arguments, multicite=multicite)
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
    """Collect the literal citations, resources and evidence gaps of the selected main.

    Args:
        inspection: The source graph of the manuscript.

    Returns:
        The citation graph; without a selected main source, it holds only the
        uncertainty that says so.
    """
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
