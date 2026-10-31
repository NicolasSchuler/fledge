"""Explicit, atomic bibliography proposals for a supported literal source graph.

This module owns the settings that select the operations (``BibliographyTransformOptions``),
the fixed order in which the selected operations run, and the assembly of their result
into one ``BibliographyProposal``. The operations themselves are in ``bibliography_edits``
(.bib files only) and ``bibliography_keys`` (keys and pruning, which read the
manuscript's citations). DOI-prefix normalization is a separate, narrowly scoped
proposal over the integrity-checked files.

No input is written. Values, string definitions and comments are preserved unless
the caller explicitly selects their field/entry for removal or supplies a guarded
replacement. A merge means discard the named donor and retain the named target;
it never guesses work identity or combines metadata.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .bibliography_edits import (
    edit_fields,
    format_entries,
    order_entries,
    remove_selected_fields,
)
from .bibliography_files import BibliographyBudget, read_declared_bibliographies
from .bibliography_integrity import inspect_bibliography_integrity
from .bibliography_keys import map_keys, prune_unreachable
from .bibliography_proposal import BibliographyProposal, ProposalBlocked, require_valid
from .bibtex_syntax import (
    LOWERCASE_NAME,
    MAX_BIB_ENTRIES,
    MAX_RESOURCE_FILES,
    MAX_RESOURCES_TOTAL_BYTES,
    BibDocument,
    read_doi,
)
from .cancellation import cancellation_point
from .citation_graph import (
    KEY_LIST_FIELDS,
    CitationGraph,
    build_citation_graph,
    graph_failures,
    require_directory,
)
from .errors import SettingsError
from .expanded_document import reachable_sources
from .findings import Finding
from .option_config import (
    TextFormat,
    require_pairs,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .report import Change, ChangeKind
from .source_graph import SourceGraph
from .text_edits import TextEdit, apply_text_edits, unified_diff_text, with_bom

_KEY = re.compile(r"(?!\*$)[^\s\\{}#$^~&%\x00-\x1f\x7f\"'(),=]+")
_FIELD_NAME_FORMAT = TextFormat(LOWERCASE_NAME, "a lowercase field name")
# Operations in rule-catalogue order (BIB201-BIB206). A failure before any step
# runs, such as graph selection or reading, is reported under the first requested one.
_CATALOGUE_ORDER = (
    "format_entries",
    "remove_fields",
    "cited_only",
    "key_mapping",
    "field_edits",
    "order_entries",
)


def _require_key(name: str, value: object) -> None:
    """Reject a value that is not a literal BibTeX key.

    Args:
        name: The setting path that the error message names.
        value: The value to check.

    Raises:
        SettingsError: ``value`` is not a literal BibTeX key.
    """
    require_text(name, value, expected="a literal BibTeX key", pattern=_KEY)


@dataclass(frozen=True)
class BibliographyTransformOptions:
    """All transformations are opt-in; tuples make reviewed requests immutable.

    Relationship fields must use key mappings instead of metadata edits. Construction
    raises ``SettingsError`` when a value breaks its rule, such as a relationship field
    in ``remove_fields`` or a malformed key pair or field edit.

    Attributes:
        format_entries: Whether to give the entries a lossless field layout.
        remove_fields: The names of the fields to remove; relationship fields are not
            accepted.
        cited_only: Whether to remove the entries that no citation reaches.
        key_renames: Pairs of an old key and its new key.
        merge_keys: Pairs of a donor key and the target key that replaces it.
        field_edits: Rows of (key, field, expected, replacement). Expected and
            replacement are literal contents, including any TeX brace protection. ``None``
            means an absent field; a ``None`` replacement removes the selected field.
        order_entries: Whether to sort each document's entries by key.
    """

    format_entries: bool = False
    remove_fields: tuple[str, ...] = ()
    cited_only: bool = False
    key_renames: tuple[tuple[str, str], ...] = ()
    merge_keys: tuple[tuple[str, str], ...] = ()
    field_edits: tuple[tuple[str, str, str | None, str | None], ...] = ()
    order_entries: bool = False

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings("remove_fields", self.remove_fields, item_format=_FIELD_NAME_FORMAT)
        related = sorted(set(self.remove_fields) & KEY_LIST_FIELDS)
        if related:
            raise SettingsError(
                f"remove_fields cannot remove relationship fields ({', '.join(related)}); "
                "use key_renames or merge_keys",
                ("remove_fields",),
            )
        for name in ("key_renames", "merge_keys"):
            require_pairs(name, getattr(self, name), first=_require_key, second=_require_key)
        self._validate_field_edits()

    def _validate_field_edits(self) -> None:
        """Check every guarded field edit row.

        Raises:
            SettingsError: ``field_edits`` is not a tuple, or a row does not hold four
                items, has a malformed key, field or value, edits a relationship field
                or a field that ``remove_fields`` removes, or repeats a key and field.
        """
        if not isinstance(self.field_edits, tuple):
            raise SettingsError(
                "field_edits must be a list of guarded field edits", ("field_edits",)
            )
        seen: set[tuple[str, str]] = set()
        for index, row in enumerate(self.field_edits):
            label = f"field_edits[{index}]"
            if not isinstance(row, tuple) or len(row) != 4:
                raise SettingsError(
                    f"{label} needs key, field, expected and replacement", ("field_edits",)
                )
            key, name, expected, replacement = row
            _require_key(f"{label} key", key)
            require_text(
                f"{label} field",
                name,
                expected=_FIELD_NAME_FORMAT.description,
                pattern=_FIELD_NAME_FORMAT.pattern,
            )
            for part, value in (("expected", expected), ("replacement", replacement)):
                require_text(f"{label} {part}", value, allow_blank=True, allow_none=True)
            if name in KEY_LIST_FIELDS:
                raise SettingsError(
                    f"{label} edits the relationship field {name}; use key_renames or merge_keys",
                    ("field_edits",),
                )
            if name in self.remove_fields:
                raise SettingsError(
                    f"{label} edits {name}, which remove_fields also removes", ("field_edits",)
                )
            if (key, name) in seen:
                raise SettingsError(
                    f"field_edits lists {name} of {key} more than once", ("field_edits",)
                )
            seen.add((key, name))

    @property
    def key_mapping(self) -> bool:
        """Whether any key is renamed or merged."""
        return bool(self.key_renames or self.merge_keys)


_DEFAULT_OPTIONS = BibliographyTransformOptions()


def _read_documents(root: Path, names: set[str]) -> dict[str, BibDocument]:
    """Read the declared resources; any file that is unusable, or unparsable, blocks.

    Args:
        root: The project directory the names are relative to.
        names: The project-relative names of the declared resources.

    Returns:
        The parsed documents by relative path.

    Raises:
        ProposalBlocked: A file cannot be read, a byte, file or entry limit is reached,
            a document uses bare carriage-return line endings, or a document does not
            parse completely.
    """
    budget = BibliographyBudget(
        max_total_bytes=MAX_RESOURCES_TOTAL_BYTES,
        max_files=MAX_RESOURCE_FILES,
        max_records=MAX_BIB_ENTRIES,
    )
    load = read_declared_bibliographies(root, names, budget)
    if load.problems:
        problem = load.problems[0]
        raise ProposalBlocked(f"{problem.message}.", path=problem.name)
    documents: dict[str, BibDocument] = {}
    for document in load.documents:
        if "\r" in document.text.replace("\r\n", ""):
            raise ProposalBlocked(
                f"{document.path} uses bare carriage-return line endings, which are not supported.",
                path=document.path,
            )
        documents[document.path] = require_valid(document)
    return documents


@dataclass(slots=True)
class _ProposalState:
    """The state that the selected operations transform in turn.

    Attributes:
        inspection: The source graph of the manuscript.
        graph: The citations of the selected sources.
        documents: The parsed .bib documents by relative path.
        options: The selected operations and their requests.
        originals: The text of each document as read, by relative path, to tell
            afterwards which ones changed.
        mapping: The key changes the key mapping applied.
        sources: The TeX sources the key mapping rewrote, by relative path, as stored in
            their files.
    """

    inspection: SourceGraph
    graph: CitationGraph
    documents: dict[str, BibDocument]
    options: BibliographyTransformOptions
    originals: dict[str, str]
    mapping: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


def _open_proposal(
    root: Path, main: str | None, options: BibliographyTransformOptions
) -> _ProposalState:
    """Select the manuscript and read its resources, or block with the reason.

    Args:
        root: The project directory.
        main: The main source to select, if given.
        options: The selected operations and their requests.

    Returns:
        The state before any operation runs.

    Raises:
        ProposalBlocked: No readable main source with supported literal bibliography
            resources is selected, the selected sources are incomplete, or a resource
            cannot be read.
    """
    inspection = SourceGraph.build(root, main)
    graph = build_citation_graph(inspection)
    scope_errors = graph_failures(inspection, reachable_sources(inspection))
    if inspection.main is None or not graph.resources or scope_errors:
        raise ProposalBlocked(
            scope_errors[0].message
            if scope_errors
            else "Select a readable main source with supported literal bibliography resources.",
            path=scope_errors[0].path if scope_errors else None,
        )
    documents = _read_documents(root, graph.resources)
    originals = {path: document.file_text for path, document in documents.items()}
    return _ProposalState(inspection, graph, documents, options, originals)


def _apply_field_edits(state: _ProposalState) -> str:
    """Apply the guarded field edits and describe the result."""
    count = edit_fields(state.documents, state.options.field_edits, state.options.merge_keys)
    return f"Reviewed metadata edits change {count} resource(s)."


def _apply_field_removal(state: _ProposalState) -> str:
    """Remove the selected fields and describe the result."""
    count = remove_selected_fields(state.documents, state.options.remove_fields)
    return f"Proposed removal of {count} explicitly selected field(s)."


def _apply_key_mapping(state: _ProposalState) -> str:
    """Apply the key renames and merges, keep the map and sources, and describe the result."""
    options = state.options
    result = map_keys(
        state.inspection, state.graph, state.documents, options.key_renames, options.merge_keys
    )
    state.mapping, state.sources = result.mapping, result.sources
    return (
        f"Reviewed key mapping updates {result.citations_updated} literal citation(s); "
        f"{len(options.merge_keys)} donor entry/entries are removed."
    )


def _apply_pruning(state: _ProposalState) -> str:
    """Remove the entries that no mapped citation reaches and describe the result."""
    count = prune_unreachable(state.inspection, state.graph, state.documents, state.mapping)
    return (
        f"Proposed removal of {count} unreachable entry/entries; "
        "nocite, relationships and all string declarations are retained."
    )


def _apply_ordering(state: _ProposalState) -> str:
    """Sort each document's entries by key and describe the result."""
    return f"Literal key ordering changes {order_entries(state.documents)} resource(s)."


def _apply_formatting(state: _ProposalState) -> str:
    """Give the entries a lossless field layout and describe the result."""
    return f"Lossless field layout changes {format_entries(state.documents)} resource(s)."


# Execution order of the selected operations, each named after the option that selects
# it. Field edits and removals address the original keys before key mapping renames
# them; pruning resolves mapped citations; ordering and layout come last.
_STEPS: tuple[tuple[str, Callable[[_ProposalState], str]], ...] = (
    ("field_edits", _apply_field_edits),
    ("remove_fields", _apply_field_removal),
    ("key_mapping", _apply_key_mapping),
    ("cited_only", _apply_pruning),
    ("order_entries", _apply_ordering),
    ("format_entries", _apply_formatting),
)


def _passed(operation: str, message: str) -> Finding:
    """Build the passed ``bibliography.<operation>`` finding that reports ``message``."""
    return Finding(f"bibliography.{operation}", message, severity="info", status="passed")


def _refusal(operation: str, error: ProposalBlocked) -> BibliographyProposal:
    """Build a proposal with no file, no change, and one finding: a partial plan is never exposed.

    Args:
        operation: The operation the finding is reported under.
        error: The block whose message, status and location the finding takes.

    Returns:
        The refused proposal.
    """
    finding = Finding(
        f"bibliography.{operation}",
        str(error),
        severity="error",
        status=error.status,
        path=error.path,
        line=error.line,
        suggestion=(
            "Resolve this condition and repeat the complete bibliography proposal; "
            "no requested transformation was returned."
        ),
    )
    return BibliographyProposal({}, [], [finding])


def _assemble(state: _ProposalState, findings: list[Finding]) -> BibliographyProposal:
    """Collect the changed files with a unified diff each, from the state after every step ran.

    Args:
        state: The state after every selected step ran.
        findings: The finding of each step.

    Returns:
        The changed .bib files and rewritten sources, a diff for each, and ``findings``.
    """
    after = {
        path: document.file_text
        for path, document in state.documents.items()
        if document.file_text != state.originals[path]
    }
    after.update(state.sources)
    before = dict(state.originals)
    for path in state.sources:
        source = state.inspection.sources[path]
        before[path] = with_bom(source.text, bom=source.bom)
    changes = [
        Change(
            path,
            kind=ChangeKind.BIBLIOGRAPHY_TRANSFORM,
            reason="Apply the explicitly selected bibliography edits.",
            diff=unified_diff_text(path, before[path], after[path]),
        )
        for path in sorted(after)
    ]
    return BibliographyProposal(
        {path: text.encode("utf-8") for path, text in after.items()}, changes, findings
    )


def plan_bibliography(
    root: Path,
    main: str | None = None,
    options: BibliographyTransformOptions = _DEFAULT_OPTIONS,
) -> BibliographyProposal:
    """Return an all-or-nothing proposal for declared resources and literal uses.

    Only selected resources are considered. All-root transformations, dynamic
    citation execution, filtered bibliographies and inline-key rewrites are not
    supported. Callers apply returned bytes only to their isolated working copy.
    A failure before any step runs, such as graph selection or reading, is reported
    under the first requested operation.

    Args:
        root: The project directory.
        main: The main source to select, if given.
        options: The selected operations and their requests.

    Returns:
        The changed files, a diff for each and one passed finding per selected
        operation; or, when an operation is blocked, no file, no change and one finding
        that says why. With no operation selected the proposal is empty.

    Raises:
        PreparationError: An operation is selected and ``root`` is not a directory.
    """
    requested = [operation for operation in _CATALOGUE_ORDER if getattr(options, operation)]
    if not requested:
        return BibliographyProposal({}, [], [])
    require_directory(root, "Bibliography input")
    try:
        state = _open_proposal(root, main, options)
    except ProposalBlocked as error:
        return _refusal(requested[0], error)
    findings: list[Finding] = []
    for operation, step in _STEPS:
        if not getattr(options, operation):
            continue
        try:
            findings.append(_passed(operation, step(state)))
        except ProposalBlocked as error:
            return _refusal(operation, error)
    return _assemble(state, findings)


def normalize_dois(root: Path, main: str | None = None) -> BibliographyProposal:
    """Propose literal DOI prefix removal; callers explicitly select and apply it.

    Invalid files and repeated DOI fields are left unchanged.

    Args:
        root: The project directory.
        main: The main source that narrows the inspected and proposed files exactly as
            it does for
            :func:`~latexprep.bibliography_integrity.check_bibliography_integrity`.

    Returns:
        Only the changed files, keyed by their relative POSIX path, with visible
        unified diffs, and the findings of
        :func:`~latexprep.bibliography_integrity.check_bibliography_integrity`.

    Raises:
        PreparationError: ``root`` is not a directory.
    """
    documents, findings = inspect_bibliography_integrity(root, main)
    proposed: dict[str, bytes] = {}
    changes: list[Change] = []
    for document in documents:
        cancellation_point()
        if not document.valid:
            continue
        edits: list[TextEdit] = []
        for entry in document.entries:
            doi_fields = entry.fields_named("doi")
            if len(doi_fields) != 1:
                continue
            span = read_doi(doi_fields[0]).prefix_span
            if span is not None:
                edits.append(TextEdit(*span, ""))
        if not edits:
            continue
        updated = with_bom(apply_text_edits(document.text, edits), bom=document.bom)
        proposed[document.path] = updated.encode("utf-8")
        changes.append(
            Change(
                document.path,
                kind=ChangeKind.NORMALIZE_DOI,
                reason=f"Remove supported prefixes from {len(edits)} literal DOI field(s).",
                diff=unified_diff_text(document.path, document.file_text, updated),
            )
        )
    return BibliographyProposal(proposed, changes, findings)
