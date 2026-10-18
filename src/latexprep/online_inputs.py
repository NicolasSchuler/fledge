"""What an online check may send: bibliography entries and source links, bounded.

This module owns the choice of inputs for the online checks. Entries come from the
bibliography resources that the selected literal source graph declares (or, without a
document root, every local .bib file), and links from configured addresses and the
literal ``\\url``, ``\\nolinkurl`` and ``\\href`` commands of the same graph. Only the
bounded literal values a lookup needs are kept, never a whole file. Whatever could not
be inspected is returned as a gap, so that a check can say its coverage is incomplete
instead of looking complete. It makes no request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .analysed_source import AnalysedSource, reuse_or_analyse
from .bibliography_files import (
    BibliographyBudget,
    discover_bibliographies,
    read_declared_bibliographies,
)
from .bibtex_syntax import DOI_PATTERN, BibDocument, Entry, field_literal, read_doi
from .cancellation import cancellation_point
from .citation_graph import require_directory, resource_declarations
from .expanded_document import reachable_sources
from .limits import MIB
from .online_options import OnlineOptions
from .source_graph import SourceGraph
from .tex_lexer import (
    Command,
    ParseLimitExceeded,
    blank_regions,
    comment_starts,
    scan_commands,
    source_regions,
)

MAX_BIB_BYTES = 2 * MIB
MAX_INPUT_BYTES = 16 * MIB
MAX_REFERENCES = 2000
# Longest literal value of one field that is read for a lookup.
_MAX_VALUE_CHARACTERS = 8192
_LINK_COMMANDS = frozenset({"url", "nolinkurl", "href"})
# \url and \nolinkurl arguments are literal text to the lexer; here they are what is sought.
_URL_LITERAL = re.compile(r"\\(?:url|nolinkurl)\b")


@dataclass(frozen=True)
class Reference:
    """One bibliography entry or link as far as an online check may use it.

    ``values`` holds the literal field values without braces (None where a field is
    repeated, a macro or markup); ``author_text`` keeps the author list as written, braces
    included, because they mark names that stay whole.
    """

    key: str
    path: str | None
    line: int | None
    kind: str
    values: dict[str, str | None]
    doi: str | None = None
    author_text: str | None = None


class OnlineInputs(NamedTuple):
    """The entries and links to check, and the reasons why some inputs were not read."""

    references: list[Reference]
    links: list[Reference]
    gaps: list[str]


def _bibliography_names(root: Path, inspection: SourceGraph, gaps: list[str]) -> list[str]:
    """The selected graph's declared resources, or every .bib file without a document root."""
    resources, resource_reasons = (
        resource_declarations(inspection, reachable_sources(inspection))
        if inspection.main
        else (set(), [])
    )
    if resources:
        gaps.extend(resource_reasons)
        return sorted(resources)
    if not inspection.roots:
        # A bibliography-only directory states its own scope: every local file.
        return discover_bibliographies(root)
    gaps.append(
        "The selected source graph declares no resolvable bibliography resource; "
        "no entry was requested"
    )
    return []


def entry_reference(document: BibDocument, entry: Entry) -> Reference:
    """The bounded literal values a lookup may share; a repeated field becomes unknown."""
    values: dict[str, str | None] = {}
    doi = None
    author_text = None
    for item in entry.fields:
        if item.name in values:
            values[item.name] = None
            if item.name == "doi":
                doi = None
            if item.name == "author":
                author_text = None
            continue
        value = field_literal(item, max_chars=_MAX_VALUE_CHARACTERS)
        values[item.name] = None if value is None else value.replace("{", "").replace("}", "")
        if item.name == "author":
            author_text = value
        if item.name == "doi":
            doi = read_doi(item).identifier
            if doi is not None and DOI_PATTERN.fullmatch(doi) is None:
                doi = None
    return Reference(
        entry.key,
        document.path,
        document.line(entry.start),
        entry.entry_type,
        values,
        doi,
        author_text,
    )


def _bibliography_references(root: Path, names: list[str], gaps: list[str]) -> list[Reference]:
    budget = BibliographyBudget(max_file_bytes=MAX_BIB_BYTES, max_total_bytes=MAX_INPUT_BYTES)
    load = read_declared_bibliographies(root, names, budget)
    gaps.extend(problem.message for problem in load.problems)
    references: list[Reference] = []
    for document in load.documents:
        if not document.valid:
            gaps.append(f"{document.path}: bibliography syntax is incomplete")
            continue
        for entry in document.entries:
            cancellation_point()
            if len(references) >= MAX_REFERENCES:
                gaps.append(f"Online bibliography scan exceeded its {MAX_REFERENCES}-entry bound")
                break
            references.append(entry_reference(document, entry))
    return references


def _link_commands(text: str) -> list[Command]:
    """The commands of ``text`` with its comments blanked and its \\url arguments readable."""
    regions = source_regions(text).regions
    kept = [
        region
        for region in regions
        if region.kind == "comment" or _URL_LITERAL.match(text, region.start) is None
    ]
    return scan_commands(blank_regions(text, kept), comment_starts(regions))


def _source_links(
    inspection: SourceGraph, options: OnlineOptions, gaps: list[str]
) -> list[Reference]:
    """Configured and literal source URLs, up to the link bound."""
    links = [
        Reference(f"configured link {index}", None, None, "url", {"url": url})
        for index, url in enumerate(options.online_replication_urls, 1)
    ]
    if inspection.main is None or inspection.uncertainties:
        gaps.append("Literal source-link coverage is incomplete in the selected source graph")
    for name in sorted(inspection.visited):
        cancellation_point()
        source = inspection.sources.get(name)
        if source is None:
            continue
        try:
            commands = _link_commands(source.text)
        except ParseLimitExceeded:
            gaps.append(f"{name}: URL command arguments are incomplete")
            continue
        for command in commands:
            if command.name in _LINK_COMMANDS and command.arguments:
                value = command.arguments[0].value
                if any(char in value for char in "{}\\") or command.depth:
                    gaps.append(f"{name}: an indirect or grouped URL command was not requested")
                    continue
                links.append(Reference("source link", name, command.line, "url", {"url": value}))
                if len(links) >= MAX_REFERENCES:
                    gaps.append(f"Source-link scan exceeded its {MAX_REFERENCES}-link bound")
                    return links
    return links


def collect_inputs(
    root: Path,
    main: str | None,
    options: OnlineOptions,
    *,
    analysed: AnalysedSource | None = None,
) -> OnlineInputs:
    """Read the entries and links that the selected online checks would send.

    Raises ``PreparationError`` when ``root`` is not a directory. Everything else that
    cannot be read becomes a gap. ``analysed`` shares the source analysis of the same
    root and main file with other checks.
    """
    require_directory(root, "Online reference input")
    gaps: list[str] = []
    # Both bibliography entries and source links are scoped by the selected graph.
    inspection = reuse_or_analyse(root, main, analysed).graph
    references = (
        _bibliography_references(root, _bibliography_names(root, inspection, gaps), gaps)
        if options.reads_bibliography
        else []
    )
    links = _source_links(inspection, options, gaps) if options.online_replication_links else []
    return OnlineInputs(references, links, sorted(set(gaps)))
