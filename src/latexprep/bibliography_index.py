"""The entries of a set of .bib documents: their keys, aliases, relationships and reach.

This module owns what a bibliography says about itself: which entries exist, which keys
and ``ids`` aliases name them, which entries point to others through relationship
fields, and which entries a manuscript's citations reach. It does not read TeX (see
``citation_graph`` for the citations themselves) and builds no findings.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from .bibtex_syntax import BibDocument, Entry, field_literal
from .cancellation import cancellation_point
from .citation_graph import (
    KEY_LIST_FIELDS,
    CitationGraph,
    Uncertainty,
    bounded_uncertainties,
    citation_keys,
)


class EntryId(NamedTuple):
    """Where an entry starts: its document's project-relative path and character offset.

    Attributes:
        path: The project-relative path of the document that holds the entry.
        offset: The character offset at which the entry starts.
    """

    path: str
    offset: int


@dataclass(frozen=True, slots=True)
class LocatedEntry:
    """A keyed entry and the document that holds it.

    Attributes:
        document: The document that holds the entry.
        entry: The keyed entry.
    """

    document: BibDocument
    entry: Entry

    @property
    def identity(self) -> EntryId:
        """The document path and start offset that identify the entry."""
        return EntryId(self.document.path, self.entry.start)


def located_entries(documents: Iterable[BibDocument]) -> list[LocatedEntry]:
    """List every keyed entry of ``documents`` with the document that holds it."""
    return [LocatedEntry(document, entry) for document in documents for entry in document.entries]


@dataclass(frozen=True, slots=True)
class EntryIndex:
    """Keys, aliases and literal relationships of a set of entries.

    Attributes:
        entries: The indexed entries.
        keys: Every key and ``ids`` alias, mapped to the entries it identifies.
        relationships: The keys that each entry's relationship fields name, by the
            entry's identity; entry-set members point back to their set.
        uncertainties: Ambiguous keys, unresolved targets and dynamic key fields.
    """

    entries: list[LocatedEntry]
    keys: dict[str, list[LocatedEntry]]
    relationships: dict[EntryId, set[str]]
    uncertainties: list[Uncertainty]

    def reachable(self, graph: CitationGraph) -> set[EntryId]:
        r"""Collect the identities reached from the graph's citations, \nocite{*} and relationships.

        Args:
            graph: The citations whose keys start the search.

        Returns:
            The identities of every entry a citation, ``\nocite{*}`` or a chain of
            relationships from a reached entry reaches.
        """
        queue: deque[LocatedEntry] = deque(self.entries if graph.nocite_all else [])
        for citation in graph.citations:
            queue.extend(self.keys.get(citation.key, []))
        reached: set[EntryId] = set()
        while queue:
            cancellation_point()
            reference = queue.popleft()
            if reference.identity in reached:
                continue
            reached.add(reference.identity)
            for target in self.relationships.get(reference.identity, set()):
                queue.extend(self.keys.get(target, []))
        return reached


def _read_key_fields(
    references: list[LocatedEntry],
) -> tuple[
    dict[str, list[LocatedEntry]],
    dict[EntryId, set[str]],
    list[tuple[str, list[str]]],
    list[str],
]:
    """Read keys and aliases, relationship targets and entry sets, with what cannot be read.

    Args:
        references: The entries to read.

    Returns:
        The entries each key or ``ids`` alias names, the relationship targets of each
        entry, the ``entryset`` keys with their members, and the reasons why a dynamic
        key field could not be read.
    """
    keys: dict[str, list[LocatedEntry]] = defaultdict(list)
    relationships: dict[EntryId, set[str]] = defaultdict(set)
    sets: list[tuple[str, list[str]]] = []
    reasons: list[str] = []
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
    return keys, relationships, sets, reasons


def _ambiguities(
    keys: dict[str, list[LocatedEntry]], relationships: dict[EntryId, set[str]]
) -> list[str]:
    """Describe the keys that name several entries and the relationship targets no key names.

    Args:
        keys: The entries that each key or alias names.
        relationships: The relationship targets of each entry.

    Returns:
        One reason for every ambiguous key or alias and every unresolved target.
    """
    reasons = [
        f"Citation key or alias {key!r} identifies multiple entries"
        for key, targets in keys.items()
        if len({target.identity for target in targets}) > 1
    ]
    for targets in relationships.values():
        reasons.extend(
            f"Bibliography relationship target {key!r} is unresolved"
            for key in targets
            if key not in keys
        )
    return reasons


def index_entries(references: list[LocatedEntry]) -> EntryIndex:
    """Index ``references`` by key and alias; ambiguity and dynamic fields become uncertainties.

    Entry-set members also point back to their set, so a cited member keeps its set and
    siblings from being called unused.

    Args:
        references: The entries to index.

    Returns:
        The index of the entries' keys, aliases and relationships, with its
        uncertainties bounded.
    """
    keys, relationships, sets, reasons = _read_key_fields(references)
    reasons.extend(_ambiguities(keys, relationships))
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
