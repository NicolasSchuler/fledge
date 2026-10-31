"""Operations of a bibliography proposal that depend on the manuscript's citations.

This module owns renaming and merging citation keys, and pruning the entries that no
citation reaches. Both rewrite the .bib files and, for key changes, the literal
citation commands of the selected sources, so both first demand that every key use in
the document is literal and resolved (``require_literal_graph``). Operations that need
only the .bib files are in ``bibliography_edits``; the order of the steps is
``bibliography_transform``'s.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import NamedTuple

from .bibliography_index import LocatedEntry, index_entries, located_entries
from .bibliography_proposal import (
    ProposalBlocked,
    apply_edits,
    entry_layout,
    joined_reasons,
    replace_documents,
    unique_entries,
)
from .bibtex_layout import FieldSpan, entry_removal
from .bibtex_syntax import BibDocument, Declaration
from .citation_graph import (
    KEY_LIST_FIELDS,
    Citation,
    CitationGraph,
    citation_keys,
    uncertainty_messages,
)
from .expanded_document import commands_before_end, reachable_sources
from .source_graph import SourceGraph
from .tex_commands import CITATION_COMMANDS, MULTICITE_COMMANDS
from .tex_lexer import split_argument_items
from .text_edits import TextEdit, with_bom

_KEY_ENTRY_COMMANDS = frozenset({"bibentry", "entrydata", "entryset", "defbibentryset"})


class KeyMapping(NamedTuple):
    """The outcome of renaming and merging keys.

    Attributes:
        mapping: Every old key or alias that changes, mapped to the key that now
            replaces it.
        sources: The rewritten TeX sources by relative path, as they are stored in their
            files.
        citations_updated: The number of citation keys rewritten in the sources.
    """

    mapping: dict[str, str]
    sources: dict[str, str]
    citations_updated: int


def _unsupported_key_uses(inspection: SourceGraph) -> list[str]:
    r"""List the uses of ``\bibentry``, ``\entrydata``, ``\entryset`` and ``\defbibentryset``.

    Key changes and pruning do not support these key uses in the reachable sources.

    Returns:
        One reason per use, with its path and line.
    """
    return [
        f"{path}:{command.line}: \\{command.name} key use is unsupported"
        for path in reachable_sources(inspection)
        for command in commands_before_end(inspection.sources[path])
        if command.name in _KEY_ENTRY_COMMANDS
    ]


def _executable_preambles(documents: dict[str, BibDocument]) -> list[str]:
    """List the ``@preamble`` declarations whose values contain a backslash.

    Returns:
        One reason per such declaration, naming its document.
    """
    return [
        f"{document.path}: executable bibliography preamble is unsupported"
        for document in documents.values()
        for record in document.records
        if isinstance(record, Declaration)
        and record.kind == "preamble"
        and any("\\" in atom.value for item in record.fields for atom in item.atoms)
    ]


def require_literal_graph(
    inspection: SourceGraph, graph: CitationGraph, documents: dict[str, BibDocument]
) -> None:
    """Refuse key changes and pruning unless every key use is literal and resolved.

    Args:
        inspection: The source graph of the manuscript.
        graph: The citations of the selected sources.
        documents: The parsed .bib documents by relative path.

    Raises:
        ProposalBlocked: The citations are uncertain, the package does not have exactly
            one document root, a key or alias is ambiguous or a relationship target
            unresolved, a key use or a preamble is unsupported, or a citation key is
            unresolved. The message lists the reasons.
    """
    reasons = [*graph.reasons, *graph.unused_reasons]
    if len(inspection.roots) != 1:
        reasons.append("Key changes and pruning require a package with exactly one document root")
    index = index_entries(located_entries(documents.values()))
    reasons.extend(uncertainty_messages(index.uncertainties))
    reasons.extend(_unsupported_key_uses(inspection))
    reasons.extend(_executable_preambles(documents))
    reasons.extend(
        f"Citation key {citation.key!r} is unresolved"
        for citation in graph.citations
        if citation.key not in index.keys and citation.key not in graph.inline_keys
    )
    if reasons:
        raise ProposalBlocked(joined_reasons(reasons))


def _key_list_edits(
    document: BibDocument, key: str, item: FieldSpan, mapping: dict[str, str]
) -> list[TextEdit]:
    """Rewrite mapped keys inside one literal relationship or alias field.

    Args:
        document: The document that holds the field.
        key: The key of the entry that holds the field, which a refusal names.
        item: The field and its location.
        mapping: The replacement of each changed key or alias.

    Returns:
        One edit per listed key that ``mapping`` changes.

    Raises:
        ProposalBlocked: The field is not one literal, or its key list is not literal.
    """
    line = document.line(item.field.start)
    if len(item.field.atoms) != 1 or item.field.atoms[0].kind != "literal":
        raise ProposalBlocked(
            f"{key!r}/{item.field.name}: only literal key lists can be updated.",
            path=document.path,
            line=line,
        )
    atom = item.field.atoms[0]
    if citation_keys(atom.value) is None:
        raise ProposalBlocked(
            f"{key!r}/{item.field.name}: the key list is not literal.",
            path=document.path,
            line=line,
        )
    return [
        TextEdit(atom.start + match.start(), atom.start + match.end(), mapping[match[0]])
        for match in re.finditer(r"[^,\s]+", atom.value)
        if match[0] in mapping and mapping[match[0]] != match[0]
    ]


def _citation_edits(inspection: SourceGraph, mapping: dict[str, str]) -> dict[str, list[TextEdit]]:
    """Rewrite mapped keys in the literal citation commands of the selected sources.

    A citation command's first argument is rewritten, a multicite command's every
    argument.

    Args:
        inspection: The source graph whose reachable sources are rewritten.
        mapping: The replacement of each changed key or alias.

    Returns:
        The edits of each source that changes, by relative path.
    """
    edits: dict[str, list[TextEdit]] = {}
    for path in sorted(reachable_sources(inspection)):
        changes: list[TextEdit] = []
        for command in commands_before_end(inspection.sources[path]):
            name = command.name.lower()
            if name not in CITATION_COMMANDS | MULTICITE_COMMANDS:
                continue
            arguments = command.arguments if name in MULTICITE_COMMANDS else command.arguments[:1]
            for argument in arguments:
                changes.extend(
                    TextEdit(item.start, item.end, mapping[item.value])
                    for item in split_argument_items(argument)
                    if item.value in mapping and mapping[item.value] != item.value
                )
        if changes:
            edits[path] = changes
    return edits


def _require_consistent_request(
    entries: dict[str, LocatedEntry], renames: dict[str, str], merges: dict[str, str]
) -> None:
    """Block a rename or merge that names missing entries or would collide.

    Args:
        entries: Every entry by key.
        renames: The new key of each renamed key.
        merges: The target key of each merge donor.

    Raises:
        ProposalBlocked: With status "failed": a merge donor is also renamed or is a
            merge target, a named key does not exist, or the result would hold duplicate
            keys.
    """
    if set(renames) & set(merges) or set(merges) & set(merges.values()):
        raise ProposalBlocked(
            "A merge donor cannot also be renamed or serve as another merge target.",
            status="failed",
        )
    for key in renames.keys() | merges.keys() | set(merges.values()):
        if key not in entries:
            raise ProposalBlocked(
                f"Reviewed key {key!r} does not exist in the selected resources.", status="failed"
            )
    final = [renames.get(key, key) for key in entries if key not in merges]
    if len(final) != len(set(final)):
        raise ProposalBlocked(
            "The reviewed key mapping would create duplicate citation keys.", status="failed"
        )


def _build_mapping(
    documents: dict[str, BibDocument],
    renames: dict[str, str],
    merges: dict[str, str],
    inline_keys: set[str],
) -> dict[str, str]:
    """Build the old-to-new key map, including the aliases of merged-away entries.

    A merge donor maps to its target's new key.

    Args:
        documents: The parsed .bib documents by relative path.
        renames: The new key of each renamed key.
        merges: The target key of each merge donor.
        inline_keys: The keys of inline bibliography entries.

    Returns:
        The replacement of each changed key or alias.

    Raises:
        ProposalBlocked: With status "failed" when the map's old or new keys include an
            inline bibliography key.
    """
    mapping = dict(renames)
    for donor, target in merges.items():
        mapping[donor] = renames.get(target, target)
    for alias, references in index_entries(located_entries(documents.values())).keys.items():
        key = references[0].entry.key
        if key in merges:
            mapping[alias] = mapping[key]
    if (set(mapping) | set(mapping.values())) & inline_keys:
        raise ProposalBlocked(
            "The reviewed mapping overlaps an inline bibliography key.", status="failed"
        )
    return mapping


def _key_edits(
    entries: dict[str, LocatedEntry],
    renames: dict[str, str],
    merges: dict[str, str],
    mapping: dict[str, str],
) -> dict[str, list[TextEdit]]:
    """Rename the renamed keys, drop the merged-away entries and update key-list fields.

    Args:
        entries: Every entry by key.
        renames: The new key of each renamed key.
        merges: The target key of each merge donor.
        mapping: The replacement of each changed key or alias.

    Returns:
        The edits of each document, by relative path.

    Raises:
        ProposalBlocked: An entry cannot be laid out, or a key-list field is not literal.
    """
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for key, reference in entries.items():
        document = reference.document
        span = entry_layout(document, reference.entry)
        if key in merges:
            edits[document.path].append(entry_removal(document, span))
            continue
        if key in renames:
            edits[document.path].append(TextEdit(span.key_start, span.key_end, renames[key]))
        for item in span.fields:
            if item.field.name in KEY_LIST_FIELDS:
                edits[document.path].extend(_key_list_edits(document, key, item, mapping))
    return edits


def map_keys(
    inspection: SourceGraph,
    graph: CitationGraph,
    documents: dict[str, BibDocument],
    key_renames: Sequence[tuple[str, str]],
    merge_keys: Sequence[tuple[str, str]],
) -> KeyMapping:
    """Apply reviewed key renames and merges to the documents and the citing sources.

    A merge discards the donor entry and keeps the target; it never combines metadata.

    Args:
        inspection: The source graph of the manuscript.
        graph: The citations of the selected sources.
        documents: The parsed .bib documents by relative path; the changed ones are
            replaced.
        key_renames: Pairs of an old key and its new key.
        merge_keys: Pairs of a donor key and the target key that replaces it.

    Returns:
        The key map, the rewritten sources and the number of citations updated.

    Raises:
        ProposalBlocked: A key use is not literal and resolved, the request is
            inconsistent, an edit cannot be made, or the resulting keys are ambiguous.
    """
    require_literal_graph(inspection, graph, documents)
    entries = unique_entries(documents)
    renames, merges = dict(key_renames), dict(merge_keys)
    _require_consistent_request(entries, renames, merges)
    mapping = _build_mapping(documents, renames, merges, graph.inline_keys)
    replace_documents(documents, _key_edits(entries, renames, merges, mapping))
    reasons = uncertainty_messages(index_entries(located_entries(documents.values())).uncertainties)
    if reasons:
        raise ProposalBlocked(
            "The reviewed mapping is ambiguous: " + joined_reasons(reasons), status="failed"
        )
    sources: dict[str, str] = {}
    count = 0
    for path, changes in _citation_edits(inspection, mapping).items():
        source = inspection.sources[path]
        sources[path] = with_bom(apply_edits(source.text, changes, path=path), bom=source.bom)
        count += len(changes)
    return KeyMapping(mapping, sources, count)


def prune_unreachable(
    inspection: SourceGraph,
    graph: CitationGraph,
    documents: dict[str, BibDocument],
    mapping: dict[str, str],
) -> int:
    r"""Remove the entries that no citation, ``\nocite{*}`` or relationship reaches.

    Args:
        inspection: The source graph of the manuscript.
        graph: The citations of the selected sources.
        documents: The parsed .bib documents by relative path; the changed ones are
            replaced.
        mapping: The key changes already applied; citations are resolved after it.

    Returns:
        The number of entries removed.

    Raises:
        ProposalBlocked: A key use is not literal and resolved, or an entry cannot be
            removed safely.
    """
    remapped = replace(
        graph,
        citations=[
            Citation(mapping.get(citation.key, citation.key), citation.path, citation.line)
            for citation in graph.citations
        ],
    )
    require_literal_graph(inspection, remapped, documents)
    index = index_entries(located_entries(documents.values()))
    keep = index.reachable(remapped)
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    count = 0
    for reference in index.entries:
        if reference.identity not in keep:
            document = reference.document
            edits[document.path].append(
                entry_removal(document, entry_layout(document, reference.entry))
            )
            count += 1
    replace_documents(documents, edits)
    return count
