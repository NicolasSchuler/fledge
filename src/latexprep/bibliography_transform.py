"""Explicit, atomic bibliography proposals for a supported literal source graph.

No input is written. Values, string definitions and comments are preserved unless
the caller explicitly selects their field/entry for removal or supplies a guarded
replacement. A merge means discard the named donor and retain the named target;
it never guesses work identity or combines metadata.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from .bibliography import _MAX_FILE_BYTES, _Document, _Entry, _Field, _Parser
from .bibliography_checks import (
    _CITATIONS,
    _GRAPH_FAILURES,
    _MAX_DOCUMENTS,
    _MAX_ENTRIES,
    _MAX_TOTAL_BYTES,
    _MULTICITES,
    _RELATIONS,
    _entry_index,
    _Graph,
    _graph,
    _keys,
    _Reference,
)
from .manuscript import _commands_before_end, _reachable_sources
from .models import Change, Finding, PreparationError
from .scheduler import cancellation_point
from .source import _Inspection, _items

_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_KEY = re.compile(r"(?!\*$)[^\s\\{}#$^~&%\x00-\x1f\x7f\"'(),=]+")
_ENTRY_HEADER = re.compile(r"@[A-Za-z][A-Za-z0-9_-]*")
_FIELD_NAME = re.compile(r"[^\s\"#%'(),={}]+")
_STRUCTURAL_FIELDS = _RELATIONS | {"ids"}
_PREFIX = "bibliography."
_OPERATIONS = (
    ("format_entries", "format_entries"),
    ("remove_fields", "remove_fields"),
    ("cited_only", "cited_only"),
    ("key_mapping", "key_mapping"),
    ("field_edits", "field_edits"),
    ("order_entries", "order_entries"),
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
        for name in ("format_entries", "cited_only", "order_entries"):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
        if (
            not isinstance(self.remove_fields, tuple)
            or any(
                not isinstance(name, str) or not _NAME.fullmatch(name)
                for name in self.remove_fields
            )
            or len(set(self.remove_fields)) != len(self.remove_fields)
            or set(self.remove_fields) & _STRUCTURAL_FIELDS
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
                or not _NAME.fullmatch(name)
                or name in _STRUCTURAL_FIELDS
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
    field: _Field
    name_end: int
    value_start: int
    value_end: int
    comma: int | None


@dataclass(frozen=True)
class _EntrySpan:
    entry: _Entry
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


def _trivia(text: str, position: int) -> int:
    while position < len(text):
        if text[position].isspace():
            position += 1
        elif text[position] == "%":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline + 1
        else:
            break
    return position


def _layout(document: _Document, entry: _Entry) -> _EntrySpan:
    text = document.text
    match = _ENTRY_HEADER.match(text, entry.start)
    if match is None or entry.key is None:
        raise _Blocked("An entry has no supported literal key.", path=document.path)
    opening = _trivia(text, match.end())
    key_start = _trivia(text, opening + 1)
    key_end = key_start + len(entry.key)
    after_key = _trivia(text, key_end)
    comma = after_key if text[after_key] == "," else None
    spans: list[_FieldSpan] = []
    for item in entry.fields:
        first, last = item.atoms[0], item.atoms[-1]
        start = first.start - (first.kind == "literal")
        end = last.end + (last.kind == "literal")
        delimiter = _trivia(text, end)
        field_comma = delimiter if text[delimiter] == "," else None
        name = _FIELD_NAME.match(text, item.start)
        if name is None:
            raise _Blocked("A field name has unsupported syntax.", path=document.path)
        spans.append(_FieldSpan(item, name.end(), start, end, field_comma))
    if spans:
        final = spans[-1]
        closing = _trivia(text, final.comma + 1 if final.comma is not None else final.value_end)
    else:
        closing = _trivia(text, comma + 1 if comma is not None else key_end)
    return _EntrySpan(entry, key_start, key_end, comma, tuple(spans), closing)


def _edits(text: str, changes: list[tuple[int, int, str]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(changes, key=lambda item: (item[0], item[1])):
        if start < cursor or not 0 <= start <= end <= len(text):
            raise _Blocked("Requested edits overlap; separate the conflicting transformations.")
        pieces.extend((text[cursor:start], replacement))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _parse(path: str, text: str) -> _Document:
    document = _Document(path, text)
    _Parser(document).parse()
    if not document.valid:
        raise _Blocked(f"{path}: repair the incomplete bibliography syntax first.", path=path)
    return document


def _read(root: Path, names: set[str]) -> tuple[dict[str, bytes], dict[str, _Document]]:
    if len(names) > _MAX_DOCUMENTS:
        raise _Blocked("The declared bibliography resource count exceeds the supported limit.")
    originals: dict[str, bytes] = {}
    documents: dict[str, _Document] = {}
    total = entries = 0
    for name in sorted(names):
        cancellation_point()
        path = root / name
        try:
            parts = Path(name).parts
            if any(
                root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)
            ):
                raise ValueError("symbolic-link resources are unsupported")
            allowance = min(_MAX_FILE_BYTES, _MAX_TOTAL_BYTES - total)
            with path.open("rb") as stream:
                raw = stream.read(allowance + 1)
            if len(raw) > allowance:
                raise ValueError("resource exceeds the per-file or total bibliography byte limit")
            total += len(raw)
            text = raw.decode("utf-8")
            if "\r" in text.replace("\r\n", ""):
                raise ValueError("bare carriage-return newlines are unsupported")
            document = _parse(name, text)
        except (OSError, UnicodeError, ValueError) as error:
            raise _Blocked(f"{name}: {error}.", path=name) from error
        entries += len(document.entries)
        if entries > _MAX_ENTRIES:
            raise _Blocked("The bibliography entry count exceeds the supported limit.")
        originals[name] = raw
        documents[name] = document
    return originals, documents


def _references(documents: dict[str, _Document]) -> list[_Reference]:
    return [
        _Reference(document, entry)
        for document in documents.values()
        for entry in document.entries
        if entry.key is not None
    ]


def _unique_entries(documents: dict[str, _Document]) -> dict[str, _Reference]:
    result: dict[str, _Reference] = {}
    for reference in _references(documents):
        key = reference.entry.key
        assert key is not None
        if key in result:
            raise _Blocked(f"Citation key {key!r} is duplicated; identify entries unambiguously.")
        result[key] = reference
    return result


def _comments(text: str, start: int, end: int, fields: tuple[_Field, ...]) -> str:
    """Retain percent comments outside removed value atoms verbatim."""
    protected = {
        atom.start - (atom.kind == "literal"): atom.end + (atom.kind == "literal")
        for item in fields
        for atom in item.atoms
    }
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


def _remove_field(document: _Document, span: _FieldSpan) -> tuple[int, int, str]:
    start = span.field.start
    end = span.comma + 1 if span.comma is not None else span.value_end
    return start, end, _comments(document.text, start, end, (span.field,))


def _remove_entry(document: _Document, span: _EntrySpan) -> tuple[int, int, str]:
    start, end = span.entry.start, span.end
    return start, end, _comments(document.text, start, end, span.entry.fields)


def _replace_documents(
    documents: dict[str, _Document], edits: dict[str, list[tuple[int, int, str]]]
) -> int:
    changed = 0
    for path, changes in edits.items():
        document = documents[path]
        text = _edits(document.text, changes)
        if text != document.text:
            documents[path] = _parse(path, text)
            changed += 1
    return changed


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _format(documents: dict[str, _Document]) -> int:
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for document in documents.values():
        cancellation_point()
        newline = _newline(document.text)
        for entry in document.entries:
            if entry.key is None:
                continue
            span = _layout(document, entry)
            if not span.fields:
                continue
            assert span.comma is not None
            previous = span.comma + 1
            for item in span.fields:
                gap = document.text[previous : item.field.start]
                if not gap or gap.isspace():
                    edits[document.path].append((previous, item.field.start, newline + "  "))
                assignment = document.text[item.name_end : item.value_start]
                if re.fullmatch(r"\s*=\s*", assignment):
                    edits[document.path].append((item.name_end, item.value_start, " = "))
                if item.comma is not None:
                    gap = document.text[item.value_end : item.comma]
                    if not gap or gap.isspace():
                        edits[document.path].append((item.value_end, item.comma, ""))
                    previous = item.comma + 1
                else:
                    edits[document.path].append((item.value_end, item.value_end, ","))
                    previous = item.value_end
            gap = document.text[previous : span.closing]
            if not gap or gap.isspace():
                edits[document.path].append((previous, span.closing, newline))
    return _replace_documents(documents, edits)


def _remove_selected(documents: dict[str, _Document], names: tuple[str, ...]) -> int:
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    count = 0
    for document in documents.values():
        for entry in document.entries:
            if entry.key is not None:
                for item in _layout(document, entry).fields:
                    if item.field.name in names:
                        edits[document.path].append(_remove_field(document, item))
                        count += 1
    _replace_documents(documents, edits)
    return count


def _expression(value: str, wrapper: str = "{") -> str:
    closing = "}" if wrapper == "{" else '"'
    expression = wrapper + value + closing
    test = _Document("replacement", "@misc{key, value=" + expression + "}")
    _Parser(test).parse()
    if (
        not test.valid
        or len(test.entries) != 1
        or len(test.entries[0].fields) != 1
        or len(test.entries[0].fields[0].atoms) != 1
        or test.entries[0].fields[0].atoms[0].value != value
    ):
        raise _Blocked(
            "A replacement has unbalanced braces or unsupported value syntax.", uncertain=False
        )
    return expression


def _fields(documents: dict[str, _Document], options: BibliographyTransformOptions) -> int:
    before = {path: document.text for path, document in documents.items()}
    entries = _unique_entries(documents)
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
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
            edits[document.path].append((item.value_start, item.value_end, expression))
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
                edits[path].append((end, end, ","))
            elif not span.fields and span.comma is None:
                edits[path].append((span.key_end, span.key_end, ","))
            value = newline + newline.join(f"  {name} = {expression}," for name, expression in rows)
            edits[path].append((span.closing, span.closing, value + newline))
    _replace_documents(documents, edits)
    return sum(document.text != before[path] for path, document in documents.items())


def _literal_graph(inspection: _Inspection, graph: _Graph, documents: dict[str, _Document]) -> None:
    reasons = [*graph.reasons, *graph.unused_reasons]
    if len(inspection.roots) != 1:
        reasons.append("Key changes and pruning require a package with exactly one document root")
    keys, _, uncertain = _entry_index(_references(documents))
    reasons.extend(uncertain)
    for path in _reachable_sources(inspection):
        for command in _commands_before_end(inspection.sources[path]):
            if command.name in {"bibentry", "entrydata", "entryset", "defbibentryset"}:
                reasons.append(f"{path}:{command.line}: \\{command.name} key use is unsupported")
    for document in documents.values():
        for entry in document.entries:
            if entry.kind == "preamble" and any(
                "\\" in atom.value for item in entry.fields for atom in item.atoms
            ):
                reasons.append(f"{document.path}: executable bibliography preamble is unsupported")
    for key, _, _ in graph.citations:
        if key not in keys and key not in graph.inline_keys:
            reasons.append(f"Citation key {key!r} is unresolved")
    if reasons:
        raise _Blocked(reasons[0] + ".")


def _mapping(
    inspection: _Inspection,
    graph: _Graph,
    documents: dict[str, _Document],
    options: BibliographyTransformOptions,
) -> tuple[dict[str, str], dict[str, bytes], int]:
    _literal_graph(inspection, graph, documents)
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
    aliases, _, _ = _entry_index(_references(documents))
    for alias, references in aliases.items():
        key = references[0].entry.key
        if key in merges:
            mapping[alias] = mapping[key]
    if (set(mapping) | set(mapping.values())) & graph.inline_keys:
        raise _Blocked("The reviewed mapping overlaps an inline bibliography key.", uncertain=False)
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for key, reference in entries.items():
        document = reference.document
        span = _layout(document, reference.entry)
        if key in merges:
            edits[document.path].append(_remove_entry(document, span))
            continue
        if key in renames:
            edits[document.path].append((span.key_start, span.key_end, renames[key]))
        for item in span.fields:
            if item.field.name not in _STRUCTURAL_FIELDS:
                continue
            if len(item.field.atoms) != 1 or item.field.atoms[0].kind != "literal":
                raise _Blocked(f"{key!r}/{item.field.name}: only literal key lists can be updated.")
            atom = item.field.atoms[0]
            if _keys(atom.value) is None:
                raise _Blocked(f"{key!r}/{item.field.name}: the key list is not literal.")
            for match in re.finditer(r"[^,\s]+", atom.value):
                old = match[0]
                if old in mapping and mapping[old] != old:
                    edits[document.path].append(
                        (atom.start + match.start(), atom.start + match.end(), mapping[old])
                    )
    _replace_documents(documents, edits)
    _, _, reasons = _entry_index(_references(documents))
    if reasons:
        raise _Blocked("The reviewed mapping is ambiguous: " + reasons[0] + ".", uncertain=False)
    sources: dict[str, bytes] = {}
    count = 0
    for path in sorted(_reachable_sources(inspection)):
        source = inspection.sources[path]
        changes: list[tuple[int, int, str]] = []
        for command in _commands_before_end(source):
            name = command.name.lower()
            if name not in _CITATIONS | _MULTICITES:
                continue
            arguments = command.arguments if name in _MULTICITES else command.arguments[:1]
            for argument in arguments:
                for item in _items(argument, True):
                    if item.value in mapping and mapping[item.value] != item.value:
                        changes.append((item.start, item.end, mapping[item.value]))
        if changes:
            text = _edits(source.text, changes)
            sources[path] = (("\ufeff" if source.bom else "") + text).encode("utf-8")
            count += len(changes)
    return mapping, sources, count


def _prune(
    inspection: _Inspection,
    graph: _Graph,
    documents: dict[str, _Document],
    mapping: dict[str, str],
) -> int:
    # Mapped citations are resolved against the already mapped bibliography.
    remapped = _Graph(
        graph.main,
        graph.resources,
        [(mapping.get(key, key), path, line) for key, path, line in graph.citations],
        graph.inline_keys,
        graph.nocite_all,
        graph.reasons,
        graph.unused_reasons,
    )
    _literal_graph(inspection, remapped, documents)
    references = _references(documents)
    keys, relationships, _ = _entry_index(references)
    queue = deque(references if graph.nocite_all else [])
    for key, _, _ in remapped.citations:
        queue.extend(keys.get(key, []))
    keep: set[tuple[str, int]] = set()
    while queue:
        cancellation_point()
        reference = queue.popleft()
        if reference.identity in keep:
            continue
        keep.add(reference.identity)
        for key in relationships.get(reference.identity, set()):
            queue.extend(keys[key])
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    count = 0
    for reference in references:
        if reference.identity not in keep:
            document = reference.document
            edits[document.path].append(_remove_entry(document, _layout(document, reference.entry)))
            count += 1
    _replace_documents(documents, edits)
    return count


def _order(documents: dict[str, _Document]) -> int:
    edits: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    _unique_entries(documents)
    for document in documents.values():
        spans = [_layout(document, entry) for entry in document.entries if entry.key is not None]
        ordered = sorted(spans, key=lambda span: span.entry.key or "")
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
                (
                    current.entry.start,
                    current.end,
                    document.text[replacement.entry.start : replacement.end],
                )
            )
    return _replace_documents(documents, edits)


def _finding(rule: str, message: str, error: _Blocked | None = None) -> Finding:
    return Finding(
        _PREFIX + rule,
        message,
        severity="error" if error else "info",
        status=("inconclusive" if error.uncertain else "failed") if error else "passed",
        path=error.path if error else None,
        suggestion=(
            "Resolve this condition and repeat the complete bibliography proposal; "
            "no requested transformation was returned."
        )
        if error
        else None,
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
    requested = [rule for attribute, rule in _OPERATIONS if getattr(options, attribute)]
    if not requested:
        return {}, [], []
    findings: list[Finding] = []
    active = requested[0]
    try:
        inspection = _Inspection(root, main)
        graph = _graph(inspection)
        reached = _reachable_sources(inspection)
        scope_errors = [
            item
            for item in inspection.findings
            if item.path in reached | {inspection.main} and item.rule in _GRAPH_FAILURES
        ]
        if inspection.main is None or not graph.resources or scope_errors:
            reason = (
                scope_errors[0].message
                if scope_errors
                else (
                    "Select a readable main source with supported literal bibliography resources."
                )
            )
            raise _Blocked(reason)
        originals, documents = _read(root, graph.resources)
        sources: dict[str, bytes] = {}
        mapping: dict[str, str] = {}
        if options.field_edits:
            active = "field_edits"
            count = _fields(documents, options)
            findings.append(
                _finding(active, f"Reviewed metadata edits change {count} resource(s).")
            )
        if options.remove_fields:
            active = "remove_fields"
            count = _remove_selected(documents, options.remove_fields)
            findings.append(
                _finding(active, f"Proposed removal of {count} explicitly selected field(s).")
            )
        if options.key_mapping:
            active = "key_mapping"
            mapping, sources, count = _mapping(inspection, graph, documents, options)
            findings.append(
                _finding(
                    active,
                    f"Reviewed key mapping updates {count} literal citation(s); "
                    f"{len(options.merge_keys)} donor entry/entries are removed.",
                )
            )
        if options.cited_only:
            active = "cited_only"
            count = _prune(inspection, graph, documents, mapping)
            findings.append(
                _finding(
                    active,
                    f"Proposed removal of {count} unreachable entry/entries; "
                    "nocite, relationships and all string declarations are retained.",
                )
            )
        if options.order_entries:
            active = "order_entries"
            count = _order(documents)
            findings.append(_finding(active, f"Literal key ordering changes {count} resource(s)."))
        if options.format_entries:
            active = "format_entries"
            count = _format(documents)
            findings.append(_finding(active, f"Lossless field layout changes {count} resource(s)."))
        proposed = {
            path: document.text.encode("utf-8")
            for path, document in documents.items()
            if document.text.encode("utf-8") != originals[path]
        }
        proposed.update(sources)
        for path in sources:
            source = inspection.sources[path]
            originals[path] = (("\ufeff" if source.bom else "") + source.text).encode("utf-8")
        changes = [
            Change(
                path,
                "bibliography-transform",
                "Apply the explicitly selected bibliography edits.",
                diff="".join(
                    difflib.unified_diff(
                        originals[path].decode("utf-8").splitlines(keepends=True),
                        proposed[path].decode("utf-8").splitlines(keepends=True),
                        fromfile=path,
                        tofile=path,
                    )
                ),
            )
            for path in sorted(proposed)
        ]
        return proposed, changes, findings
    except _Blocked as error:
        # Never expose a partial plan, including independent earlier operations.
        return {}, [], [_finding(active, str(error), error)]
