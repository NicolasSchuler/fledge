"""Explicit, atomic bibliography proposals for a supported literal source graph.

No input is written. Values, string definitions and comments are preserved unless
the caller explicitly selects their field/entry for removal or supplies a guarded
replacement. A merge means discard the named donor and retain the named target;
it never guesses work identity or combines metadata. DOI-prefix normalization is
a separate, narrowly scoped proposal over the integrity-checked files.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .bibliography_checks import inspect_bibliography_integrity
from .bibtex_syntax import (
    ENTRY_TYPE,
    IDENTIFIER,
    LOWERCASE_NAME,
    MAX_BIB_ENTRIES,
    MAX_RESOURCE_FILES,
    MAX_RESOURCES_TOTAL_BYTES,
    BibDocument,
    BibFileReader,
    Declaration,
    Entry,
    Field,
    describe_read_failure,
    parse_bibtex,
    read_doi,
    skip_trivia,
)
from .cancellation import cancellation_point
from .citation_graph import (
    CITATION_COMMANDS,
    KEY_LIST_FIELDS,
    MULTICITE_COMMANDS,
    Citation,
    CitationGraph,
    LocatedEntry,
    build_citation_graph,
    citation_keys,
    graph_failures,
    index_entries,
    located_entries,
    require_directory,
    uncertainty_messages,
)
from .errors import PreparationError
from .expanded_document import commands_before_end, reachable_sources
from .findings import Finding
from .option_config import require_booleans
from .report import Change, ChangeKind
from .source_graph import SourceGraph
from .tex_lexer import split_argument_items
from .text_edits import (
    OverlappingEdits,
    TextEdit,
    apply_text_edits,
    unified_diff_text,
    with_bom,
)

_KEY = re.compile(r"(?!\*$)[^\s\\{}#$^~&%\x00-\x1f\x7f\"'(),=]+")
_ENTRY_HEADER = re.compile("@" + ENTRY_TYPE.pattern)
# Reasons quoted in a refusal; the rest are counted so one rerun can fix more than one.
_MAX_LISTED_REASONS = 3
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


@dataclass(frozen=True)
class BibliographyTransformOptions:
    """All transformations are opt-in; tuples make reviewed requests immutable.

    ``field_edits`` rows contain (key, field, expected, replacement). Expected
    and replacement are literal contents, including any TeX brace protection.
    None means an absent field; a None replacement removes the selected field.
    Relationship fields must use key mappings instead of metadata edits.
    """

    format_entries: bool = False
    remove_fields: tuple[str, ...] = ()
    cited_only: bool = False
    key_renames: tuple[tuple[str, str], ...] = ()
    merge_keys: tuple[tuple[str, str], ...] = ()
    field_edits: tuple[tuple[str, str, str | None, str | None], ...] = ()
    order_entries: bool = False

    def __post_init__(self) -> None:
        require_booleans(self, "format_entries", "cited_only", "order_entries")
        if (
            not isinstance(self.remove_fields, tuple)
            or any(
                not isinstance(name, str) or not LOWERCASE_NAME.fullmatch(name)
                for name in self.remove_fields
            )
            or len(set(self.remove_fields)) != len(self.remove_fields)
            or set(self.remove_fields) & KEY_LIST_FIELDS
        ):
            raise PreparationError(
                "remove_fields must contain unique lowercase, non-relationship field names"
            )
        for name in ("key_renames", "merge_keys"):
            rows = getattr(self, name)
            if not isinstance(rows, tuple) or any(
                not isinstance(row, tuple)
                or len(row) != 2
                or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in row)
                for row in rows
            ):
                raise PreparationError(f"{name} must contain literal (source, target) key tuples")
            if len({row[0] for row in rows}) != len(rows):
                raise PreparationError(f"{name} source keys must be unique")
        if not isinstance(self.field_edits, tuple):
            raise PreparationError("field_edits must be a tuple of guarded field edits")
        seen: set[tuple[str, str]] = set()
        for row in self.field_edits:
            if not isinstance(row, tuple) or len(row) != 4:
                raise PreparationError("Each field edit needs key, field, expected and replacement")
            key, name, expected, replacement = row
            if (
                not isinstance(key, str)
                or not _KEY.fullmatch(key)
                or not isinstance(name, str)
                or not LOWERCASE_NAME.fullmatch(name)
                or name in KEY_LIST_FIELDS
                or name in self.remove_fields
                or (key, name) in seen
                or any(
                    value is not None and (not isinstance(value, str) or "\x00" in value)
                    for value in (expected, replacement)
                )
            ):
                raise PreparationError(
                    "Field edits need unique literal keys, non-relationship fields and text "
                    "values; they cannot overlap remove_fields"
                )
            seen.add((key, name))

    @property
    def key_mapping(self) -> bool:
        return bool(self.key_renames or self.merge_keys)


_DEFAULT_OPTIONS = BibliographyTransformOptions()


@dataclass(frozen=True)
class _FieldSpan:
    field: Field
    name_end: int
    value_start: int
    value_end: int
    comma: int | None


@dataclass(frozen=True)
class _EntrySpan:
    entry: Entry
    key_start: int
    key_end: int
    comma: int | None
    fields: tuple[_FieldSpan, ...]
    closing: int

    @property
    def end(self) -> int:
        return self.closing + 1


class _Blocked(Exception):
    def __init__(self, message: str, *, uncertain: bool = True, path: str | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.path = path


def _layout(document: BibDocument, entry: Entry) -> _EntrySpan:
    text = document.text
    match = _ENTRY_HEADER.match(text, entry.start)
    if match is None:
        raise _Blocked("An entry has no supported literal key.", path=document.path)
    opening = skip_trivia(text, match.end())
    key_start = skip_trivia(text, opening + 1)
    key_end = key_start + len(entry.key)
    after_key = skip_trivia(text, key_end)
    comma = after_key if text[after_key] == "," else None
    spans: list[_FieldSpan] = []
    for item in entry.fields:
        start, end = item.atoms[0].outer_start, item.atoms[-1].outer_end
        delimiter = skip_trivia(text, end)
        field_comma = delimiter if text[delimiter] == "," else None
        name = IDENTIFIER.match(text, item.start)
        if name is None:
            raise _Blocked("A field name has unsupported syntax.", path=document.path)
        spans.append(_FieldSpan(item, name.end(), start, end, field_comma))
    if spans:
        final = spans[-1]
        closing = skip_trivia(text, final.comma + 1 if final.comma is not None else final.value_end)
    else:
        closing = skip_trivia(text, comma + 1 if comma is not None else key_end)
    return _EntrySpan(entry, key_start, key_end, comma, tuple(spans), closing)


def _apply(text: str, edits: list[TextEdit]) -> str:
    try:
        return apply_text_edits(text, edits)
    except OverlappingEdits as error:
        raise _Blocked(
            "Requested edits overlap; separate the conflicting transformations."
        ) from error


def _joined_reasons(reasons: list[str]) -> str:
    """The first few ``reasons`` as one sentence, with a count of those left out."""
    distinct = list(dict.fromkeys(reason.rstrip(".") for reason in reasons))
    text = "; ".join(distinct[:_MAX_LISTED_REASONS])
    if len(distinct) > _MAX_LISTED_REASONS:
        text += f"; and {len(distinct) - _MAX_LISTED_REASONS} more"
    return text + "."


def _require_valid(document: BibDocument) -> BibDocument:
    if not document.valid:
        raise _Blocked(
            f"{document.path}: repair the incomplete bibliography syntax first.",
            path=document.path,
        )
    return document


def _read(root: Path, names: set[str]) -> dict[str, BibDocument]:
    if len(names) > MAX_RESOURCE_FILES:
        raise _Blocked("The declared bibliography resource count exceeds the supported limit.")
    documents: dict[str, BibDocument] = {}
    reader = BibFileReader(max_total_bytes=MAX_RESOURCES_TOTAL_BYTES)
    entries = 0
    for name in sorted(names):
        cancellation_point()
        try:
            parts = Path(name).parts
            if any(
                root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)
            ):
                raise ValueError("is or lies behind a symbolic link, which is not supported")
            document = reader.read(root / name, name)
            if document is None:
                raise ValueError("exceeds the per-file or total bibliography byte limit")
            if "\r" in document.text.replace("\r\n", ""):
                raise ValueError("uses bare carriage-return line endings, which are not supported")
        except (OSError, UnicodeError, ValueError) as error:
            raise _Blocked(f"{name} {describe_read_failure(error)}.", path=name) from error
        entries += len(_require_valid(document).records)
        if entries > MAX_BIB_ENTRIES:
            raise _Blocked("The bibliography entry count exceeds the supported limit.")
        documents[name] = document
    return documents


def _unique_entries(documents: dict[str, BibDocument]) -> dict[str, LocatedEntry]:
    result: dict[str, LocatedEntry] = {}
    for reference in located_entries(documents.values()):
        key = reference.entry.key
        if key in result:
            raise _Blocked(f"Citation key {key!r} is duplicated; identify entries unambiguously.")
        result[key] = reference
    return result


def _comments(text: str, start: int, end: int, fields: tuple[Field, ...]) -> str:
    """Retain percent comments outside removed value atoms verbatim."""
    protected = {atom.outer_start: atom.outer_end for item in fields for atom in item.atoms}
    result: list[str] = []
    position = start
    while position < end:
        if position in protected:
            position = protected[position]
        elif text[position] == "%":
            newline = text.find("\n", position, end)
            stop = end if newline < 0 else newline + 1
            result.append(text[position:stop])
            position = stop
        else:
            position += 1
    return "".join(result)


def _remove_field(document: BibDocument, span: _FieldSpan) -> TextEdit:
    start = span.field.start
    end = span.comma + 1 if span.comma is not None else span.value_end
    return TextEdit(start, end, _comments(document.text, start, end, (span.field,)))


def _remove_entry(document: BibDocument, span: _EntrySpan) -> TextEdit:
    start, end = span.entry.start, span.end
    return TextEdit(start, end, _comments(document.text, start, end, span.entry.fields))


def _replace_documents(documents: dict[str, BibDocument], edits: dict[str, list[TextEdit]]) -> int:
    changed = 0
    for path, changes in edits.items():
        document = documents[path]
        text = _apply(document.text, changes)
        if text != document.text:
            documents[path] = _require_valid(parse_bibtex(path, text, bom=document.bom))
            changed += 1
    return changed


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _format(documents: dict[str, BibDocument]) -> int:
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for document in documents.values():
        cancellation_point()
        newline = _newline(document.text)
        for entry in document.entries:
            span = _layout(document, entry)
            if not span.fields:
                continue
            assert span.comma is not None
            previous = span.comma + 1
            for item in span.fields:
                gap = document.text[previous : item.field.start]
                if not gap or gap.isspace():
                    edits[document.path].append(
                        TextEdit(previous, item.field.start, newline + "  ")
                    )
                assignment = document.text[item.name_end : item.value_start]
                if re.fullmatch(r"\s*=\s*", assignment):
                    edits[document.path].append(TextEdit(item.name_end, item.value_start, " = "))
                if item.comma is not None:
                    gap = document.text[item.value_end : item.comma]
                    if not gap or gap.isspace():
                        edits[document.path].append(TextEdit(item.value_end, item.comma, ""))
                    previous = item.comma + 1
                else:
                    edits[document.path].append(TextEdit(item.value_end, item.value_end, ","))
                    previous = item.value_end
            gap = document.text[previous : span.closing]
            if not gap or gap.isspace():
                edits[document.path].append(TextEdit(previous, span.closing, newline))
    return _replace_documents(documents, edits)


def _remove_selected(documents: dict[str, BibDocument], names: tuple[str, ...]) -> int:
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    count = 0
    for document in documents.values():
        for entry in document.entries:
            for item in _layout(document, entry).fields:
                if item.field.name in names:
                    edits[document.path].append(_remove_field(document, item))
                    count += 1
    _replace_documents(documents, edits)
    return count


def _expression(value: str, wrapper: str = "{") -> str:
    closing = "}" if wrapper == "{" else '"'
    expression = wrapper + value + closing
    test = parse_bibtex("replacement", "@misc{key, value=" + expression + "}")
    if (
        not test.valid
        or len(test.records) != 1
        or len(test.records[0].fields) != 1
        or len(test.records[0].fields[0].atoms) != 1
        or test.records[0].fields[0].atoms[0].value != value
    ):
        raise _Blocked(
            "A replacement has unbalanced braces or unsupported value syntax.", uncertain=False
        )
    return expression


def _edit_fields(documents: dict[str, BibDocument], options: BibliographyTransformOptions) -> int:
    before = {path: document.text for path, document in documents.items()}
    entries = _unique_entries(documents)
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    additions: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    donors = dict(options.merge_keys)
    for key, name, expected, replacement in options.field_edits:
        if key not in entries:
            raise _Blocked(
                f"Field edit key {key!r} does not exist in the selected resources.", uncertain=False
            )
        if key in donors:
            raise _Blocked(
                f"Field edit key {key!r} is also selected for merge removal.", uncertain=False
            )
        reference = entries[key]
        document = reference.document
        span = _layout(document, reference.entry)
        matches = [item for item in span.fields if item.field.name == name]
        if len(matches) > 1:
            raise _Blocked(f"{key!r} has repeated {name!r} fields; choose one value first.")
        if not matches:
            if expected is not None:
                raise _Blocked(
                    f"The expected {name!r} field in {key!r} is absent.", uncertain=False
                )
            if replacement is not None:
                additions[document.path][key].append((name, _expression(replacement)))
            continue
        item = matches[0]
        atoms = item.field.atoms
        if len(atoms) != 1 or atoms[0].kind not in {"literal", "number"}:
            raise _Blocked(f"{key!r}/{name}: macro or concatenated values cannot be edited.")
        atom = atoms[0]
        if atom.value != expected:
            raise _Blocked(
                f"The expected value for {key!r}/{name} does not match; review the edit.",
                uncertain=False,
            )
        if replacement is None:
            edits[document.path].append(_remove_field(document, item))
        elif replacement != expected:
            if atom.kind == "number" and replacement.isascii() and replacement.isdigit():
                expression = replacement
            else:
                wrapper = document.text[item.value_start] if atom.kind == "literal" else "{"
                expression = _expression(replacement, wrapper)
            edits[document.path].append(TextEdit(item.value_start, item.value_end, expression))
    _replace_documents(documents, edits)
    edits = defaultdict(list)
    entries = _unique_entries(documents)
    for path, groups in additions.items():
        document = documents[path]
        newline = _newline(document.text)
        for key, rows in groups.items():
            span = _layout(document, entries[key].entry)
            if span.fields and span.fields[-1].comma is None:
                end = span.fields[-1].value_end
                edits[path].append(TextEdit(end, end, ","))
            elif not span.fields and span.comma is None:
                edits[path].append(TextEdit(span.key_end, span.key_end, ","))
            value = newline + newline.join(f"  {name} = {expression}," for name, expression in rows)
            edits[path].append(TextEdit(span.closing, span.closing, value + newline))
    _replace_documents(documents, edits)
    return sum(document.text != before[path] for path, document in documents.items())


def _require_literal_graph(
    inspection: SourceGraph, graph: CitationGraph, documents: dict[str, BibDocument]
) -> None:
    """Refuse key changes and pruning unless every key use is literal and resolved."""
    reasons = [*graph.reasons, *graph.unused_reasons]
    if len(inspection.roots) != 1:
        reasons.append("Key changes and pruning require a package with exactly one document root")
    index = index_entries(located_entries(documents.values()))
    reasons.extend(uncertainty_messages(index.uncertainties))
    for path in reachable_sources(inspection):
        for command in commands_before_end(inspection.sources[path]):
            if command.name in {"bibentry", "entrydata", "entryset", "defbibentryset"}:
                reasons.append(f"{path}:{command.line}: \\{command.name} key use is unsupported")
    for document in documents.values():
        for record in document.records:
            if (
                isinstance(record, Declaration)
                and record.kind == "preamble"
                and any("\\" in atom.value for item in record.fields for atom in item.atoms)
            ):
                reasons.append(f"{document.path}: executable bibliography preamble is unsupported")
    for citation in graph.citations:
        if citation.key not in index.keys and citation.key not in graph.inline_keys:
            reasons.append(f"Citation key {citation.key!r} is unresolved")
    if reasons:
        raise _Blocked(_joined_reasons(reasons))


def _key_list_edits(key: str, item: _FieldSpan, mapping: dict[str, str]) -> list[TextEdit]:
    """Rewrite mapped keys inside one literal relationship or alias field."""
    if len(item.field.atoms) != 1 or item.field.atoms[0].kind != "literal":
        raise _Blocked(f"{key!r}/{item.field.name}: only literal key lists can be updated.")
    atom = item.field.atoms[0]
    if citation_keys(atom.value) is None:
        raise _Blocked(f"{key!r}/{item.field.name}: the key list is not literal.")
    return [
        TextEdit(atom.start + match.start(), atom.start + match.end(), mapping[match[0]])
        for match in re.finditer(r"[^,\s]+", atom.value)
        if match[0] in mapping and mapping[match[0]] != match[0]
    ]


def _citation_edits(inspection: SourceGraph, mapping: dict[str, str]) -> dict[str, list[TextEdit]]:
    """Rewrite mapped keys in the literal citation commands of the selected sources."""
    edits: dict[str, list[TextEdit]] = {}
    for path in sorted(reachable_sources(inspection)):
        changes: list[TextEdit] = []
        for command in commands_before_end(inspection.sources[path]):
            name = command.name.lower()
            if name not in CITATION_COMMANDS | MULTICITE_COMMANDS:
                continue
            arguments = command.arguments if name in MULTICITE_COMMANDS else command.arguments[:1]
            for argument in arguments:
                for item in split_argument_items(argument):
                    if item.value in mapping and mapping[item.value] != item.value:
                        changes.append(TextEdit(item.start, item.end, mapping[item.value]))
        if changes:
            edits[path] = changes
    return edits


def _mapping(
    inspection: SourceGraph,
    graph: CitationGraph,
    documents: dict[str, BibDocument],
    options: BibliographyTransformOptions,
) -> tuple[dict[str, str], dict[str, str], int]:
    """Apply reviewed key renames and merges; return the mapping and rewritten sources."""
    _require_literal_graph(inspection, graph, documents)
    entries = _unique_entries(documents)
    renames, merges = dict(options.key_renames), dict(options.merge_keys)
    if set(renames) & set(merges) or set(merges) & set(merges.values()):
        raise _Blocked(
            "A merge donor cannot also be renamed or serve as another merge target.",
            uncertain=False,
        )
    for key in renames.keys() | merges.keys() | set(merges.values()):
        if key not in entries:
            raise _Blocked(
                f"Reviewed key {key!r} does not exist in the selected resources.", uncertain=False
            )
    final = [renames.get(key, key) for key in entries if key not in merges]
    if len(final) != len(set(final)):
        raise _Blocked(
            "The reviewed key mapping would create duplicate citation keys.", uncertain=False
        )
    mapping = dict(renames)
    for donor, target in merges.items():
        mapping[donor] = renames.get(target, target)
    for alias, references in index_entries(located_entries(documents.values())).keys.items():
        key = references[0].entry.key
        if key in merges:
            mapping[alias] = mapping[key]
    if (set(mapping) | set(mapping.values())) & graph.inline_keys:
        raise _Blocked("The reviewed mapping overlaps an inline bibliography key.", uncertain=False)
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for key, reference in entries.items():
        document = reference.document
        span = _layout(document, reference.entry)
        if key in merges:
            edits[document.path].append(_remove_entry(document, span))
            continue
        if key in renames:
            edits[document.path].append(TextEdit(span.key_start, span.key_end, renames[key]))
        for item in span.fields:
            if item.field.name in KEY_LIST_FIELDS:
                edits[document.path].extend(_key_list_edits(key, item, mapping))
    _replace_documents(documents, edits)
    reasons = uncertainty_messages(index_entries(located_entries(documents.values())).uncertainties)
    if reasons:
        raise _Blocked(
            "The reviewed mapping is ambiguous: " + _joined_reasons(reasons), uncertain=False
        )
    sources: dict[str, str] = {}
    count = 0
    for path, changes in _citation_edits(inspection, mapping).items():
        source = inspection.sources[path]
        sources[path] = with_bom(_apply(source.text, changes), source.bom)
        count += len(changes)
    return mapping, sources, count


def _prune(
    inspection: SourceGraph,
    graph: CitationGraph,
    documents: dict[str, BibDocument],
    mapping: dict[str, str],
) -> int:
    # Mapped citations are resolved against the already mapped bibliography.
    remapped = replace(
        graph,
        citations=[
            Citation(mapping.get(citation.key, citation.key), citation.path, citation.line)
            for citation in graph.citations
        ],
    )
    _require_literal_graph(inspection, remapped, documents)
    index = index_entries(located_entries(documents.values()))
    keep = index.reachable(remapped)
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    count = 0
    for reference in index.entries:
        if reference.identity not in keep:
            document = reference.document
            edits[document.path].append(_remove_entry(document, _layout(document, reference.entry)))
            count += 1
    _replace_documents(documents, edits)
    return count


def _order(documents: dict[str, BibDocument]) -> int:
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    _unique_entries(documents)
    for document in documents.values():
        spans = [_layout(document, entry) for entry in document.entries]
        ordered = sorted(spans, key=lambda span: span.entry.key)
        if spans == ordered:
            continue
        for first, second in zip(spans, spans[1:], strict=False):
            if document.text[first.end : second.entry.start].strip():
                raise _Blocked(
                    "Ordering would move entries across comments, free text or macro declarations; "
                    "separate those boundaries first.",
                    path=document.path,
                )
        positions = {span.entry.key: index for index, span in enumerate(ordered)}
        for span in spans:
            for item in span.entry.fields:
                if item.name in {"crossref", "xref"}:
                    if len(item.atoms) != 1 or item.atoms[0].kind != "literal":
                        raise _Blocked("Ordering cannot resolve a macro-valued cross-reference.")
                    target = item.atoms[0].value.strip()
                    if target not in positions or positions[span.entry.key] >= positions[target]:
                        raise _Blocked(
                            "Key ordering would place a cross-reference parent before its child, "
                            "or its ordering is outside this resource."
                        )
        for current, replacement in zip(spans, ordered, strict=True):
            edits[document.path].append(
                TextEdit(
                    current.entry.start,
                    current.end,
                    document.text[replacement.entry.start : replacement.end],
                )
            )
    return _replace_documents(documents, edits)


@dataclass(slots=True)
class _Proposal:
    """The state that the selected operations transform in turn."""

    inspection: SourceGraph
    graph: CitationGraph
    documents: dict[str, BibDocument]
    options: BibliographyTransformOptions
    mapping: dict[str, str] = field(default_factory=dict)
    # Rewritten TeX sources, as stored in their files.
    sources: dict[str, str] = field(default_factory=dict)


def _apply_field_edits(proposal: _Proposal) -> str:
    count = _edit_fields(proposal.documents, proposal.options)
    return f"Reviewed metadata edits change {count} resource(s)."


def _apply_field_removal(proposal: _Proposal) -> str:
    count = _remove_selected(proposal.documents, proposal.options.remove_fields)
    return f"Proposed removal of {count} explicitly selected field(s)."


def _apply_key_mapping(proposal: _Proposal) -> str:
    proposal.mapping, proposal.sources, count = _mapping(
        proposal.inspection, proposal.graph, proposal.documents, proposal.options
    )
    return (
        f"Reviewed key mapping updates {count} literal citation(s); "
        f"{len(proposal.options.merge_keys)} donor entry/entries are removed."
    )


def _apply_pruning(proposal: _Proposal) -> str:
    count = _prune(proposal.inspection, proposal.graph, proposal.documents, proposal.mapping)
    return (
        f"Proposed removal of {count} unreachable entry/entries; "
        "nocite, relationships and all string declarations are retained."
    )


def _apply_ordering(proposal: _Proposal) -> str:
    return f"Literal key ordering changes {_order(proposal.documents)} resource(s)."


def _apply_formatting(proposal: _Proposal) -> str:
    return f"Lossless field layout changes {_format(proposal.documents)} resource(s)."


# Execution order of the selected operations. Field edits and removals address the
# original keys before key mapping renames them; pruning resolves mapped citations;
# ordering and layout come last.
_STEPS: tuple[tuple[str, Callable[[_Proposal], str]], ...] = (
    ("field_edits", _apply_field_edits),
    ("remove_fields", _apply_field_removal),
    ("key_mapping", _apply_key_mapping),
    ("cited_only", _apply_pruning),
    ("order_entries", _apply_ordering),
    ("format_entries", _apply_formatting),
)


def _passed(operation: str, message: str) -> Finding:
    return Finding(f"bibliography.{operation}", message, severity="info", status="passed")


def _refused(operation: str, error: _Blocked) -> Finding:
    return Finding(
        f"bibliography.{operation}",
        str(error),
        severity="error",
        status="inconclusive" if error.uncertain else "failed",
        path=error.path,
        suggestion=(
            "Resolve this condition and repeat the complete bibliography proposal; "
            "no requested transformation was returned."
        ),
    )


def plan_bibliography(
    root: Path,
    main: str | None = None,
    options: BibliographyTransformOptions = _DEFAULT_OPTIONS,
) -> tuple[dict[str, bytes], list[Change], list[Finding]]:
    """Return an all-or-nothing proposal for declared resources and literal uses.

    Only selected resources are considered. All-root transformations, dynamic
    citation execution, filtered bibliographies and inline-key rewrites are not
    supported. Callers apply returned bytes only to their isolated working copy.
    """
    requested = [operation for operation in _CATALOGUE_ORDER if getattr(options, operation)]
    if not requested:
        return {}, [], []
    require_directory(root, "Bibliography input")
    findings: list[Finding] = []
    active = requested[0]
    try:
        inspection = SourceGraph.build(root, main)
        graph = build_citation_graph(inspection)
        scope_errors = graph_failures(inspection, reachable_sources(inspection))
        if inspection.main is None or not graph.resources or scope_errors:
            raise _Blocked(
                scope_errors[0].message
                if scope_errors
                else "Select a readable main source with supported literal bibliography resources."
            )
        documents = _read(root, graph.resources)
        before = {path: document.file_text for path, document in documents.items()}
        proposal = _Proposal(inspection, graph, documents, options)
        for operation, step in _STEPS:
            if getattr(options, operation):
                active = operation
                findings.append(_passed(operation, step(proposal)))
        after = {
            path: document.file_text
            for path, document in documents.items()
            if document.file_text != before[path]
        }
        after.update(proposal.sources)
        for path in proposal.sources:
            source = inspection.sources[path]
            before[path] = with_bom(source.text, source.bom)
        changes = [
            Change(
                path,
                kind=ChangeKind.BIBLIOGRAPHY_TRANSFORM,
                reason="Apply the explicitly selected bibliography edits.",
                diff=unified_diff_text(path, before[path], after[path]),
            )
            for path in sorted(after)
        ]
        return {path: text.encode("utf-8") for path, text in after.items()}, changes, findings
    except _Blocked as error:
        # Never expose a partial plan, including independent earlier operations.
        return {}, [], [_refused(active, error)]


def normalize_dois(
    root: Path, main: str | None = None
) -> tuple[dict[str, bytes], list[Change], list[Finding]]:
    """Propose literal DOI prefix removal; callers explicitly select and apply it.

    ``main`` narrows the inspected and proposed files exactly as it does for
    :func:`~latexprep.bibliography_checks.check_bibliography_integrity`, whose
    findings are returned. Invalid files and repeated DOI fields are left
    unchanged. Only changed files are returned, keyed by their relative POSIX
    path, with visible unified diffs.
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
            doi_fields = [bib_field for bib_field in entry.fields if bib_field.name == "doi"]
            if len(doi_fields) != 1:
                continue
            span = read_doi(doi_fields[0]).prefix_span
            if span is not None:
                edits.append(TextEdit(*span, ""))
        if not edits:
            continue
        updated = with_bom(apply_text_edits(document.text, edits), document.bom)
        proposed[document.path] = updated.encode("utf-8")
        changes.append(
            Change(
                document.path,
                kind=ChangeKind.NORMALIZE_DOI,
                reason=f"Remove supported prefixes from {len(edits)} literal DOI field(s).",
                diff=unified_diff_text(document.path, document.file_text, updated),
            )
        )
    return proposed, changes, findings
