"""Bounded, source-preserving bibliography coverage and metadata diagnostics.

Only declared local resources contribute to a selected manuscript's evidence.
This lexical subset does not execute TeX, expand bibliography string macros,
apply a bibliography backend's inheritance rules, contact URLs, or propose edits.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from .bibliography import _MAX_FILE_BYTES, _Document, _doi_value, _Entry, _Field, _Parser
from .manuscript import _commands_before_end, _reachable_sources
from .models import Finding, PreparationError
from .scheduler import cancellation_point
from .source import _Inspection, _items

_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_DOCUMENTS = 128
_MAX_ROOTS = 32
_MAX_ENTRIES = 100_000
_MAX_FUZZY_ENTRIES = 2_000
_MAX_FUZZY_PAIRS = 20_000
_MAX_FUZZY_TEXT = 2_000
_MAX_FIELD_CHARS = 64 * 1024
_MAX_REASONS = 32
_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_CITATIONS = frozenset(
    "cite citep citet citealp citealt citeauthor citeyear citeyearpar "
    "parencite textcite autocite footcite footcitetext smartcite supercite "
    "fullcite footfullcite notecite pnotecite fnotecite citeurl citetitle nocite".split()
)
_MULTICITES = frozenset(
    "cites parencites textcites autocites footcites footcitetexts smartcites supercites".split()
)
_RELATIONS = frozenset({"crossref", "xref", "xdata", "related", "entryset"})
_INHERITANCE = frozenset({"crossref", "xref", "xdata"})
_DYNAMIC = frozenset(
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
_GRAPH_FAILURES = frozenset(
    {
        "source-encoding",
        "source-size-limit",
        "source-argument-limit",
        "source-command-limit",
        "source-literal-unclosed",
        "source-input-cycle",
        "source-input-depth",
        "source-expansion-limit",
        "source-dynamic-path",
        "source-nested-file-command",
        "source-conditional-dependency",
        "source-path-literal-context",
        "source-external-path",
        "source-ambiguous-dependency",
        "source-case-mismatch",
        "source-custom-path",
        "source-unsupported-command",
        "source-missing-dependency",
        "source-link",
        "source-context-ambiguity",
        "source-subfile-structure",
        "source-font-options",
    }
)


@dataclass(frozen=True)
class BibliographyOptions:
    check_citation_coverage: bool = True
    check_uncited_entries: bool = True
    all_roots: bool = False
    required_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    doi_entry_types: tuple[str, ...] = ()
    check_page_ranges: bool = True
    check_urls: bool = True
    check_capitalization: bool = False
    check_fuzzy_duplicates: bool = False
    fuzzy_duplicate_threshold: float = 0.93

    def __post_init__(self) -> None:
        for name in (
            "check_citation_coverage",
            "check_uncited_entries",
            "all_roots",
            "check_page_ranges",
            "check_urls",
            "check_capitalization",
            "check_fuzzy_duplicates",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
        threshold = self.fuzzy_duplicate_threshold
        if type(threshold) not in {float, int} or not 0 < threshold <= 1:
            raise PreparationError("fuzzy_duplicate_threshold must be greater than 0 and at most 1")
        if (
            not isinstance(self.doi_entry_types, tuple)
            or any(
                not isinstance(value, str) or (value != "*" and not _NAME.fullmatch(value))
                for value in self.doi_entry_types
            )
            or len(set(self.doi_entry_types)) != len(self.doi_entry_types)
        ):
            raise PreparationError("doi_entry_types must be unique lowercase entry types or '*'")
        if not isinstance(self.required_fields, tuple):
            raise PreparationError("required_fields must be a tuple of entry-type and field tuples")
        kinds: set[str] = set()
        for row in self.required_fields:
            if not isinstance(row, tuple) or len(row) != 2:
                raise PreparationError("Each required_fields row must contain a type and fields")
            kind, fields = row
            if not isinstance(kind, str) or (kind != "*" and not _NAME.fullmatch(kind)):
                raise PreparationError("Required-field entry types must be lowercase or '*'")
            if kind in kinds:
                raise PreparationError("Required-field entry types must be unique")
            kinds.add(kind)
            if (
                not isinstance(fields, tuple)
                or not fields
                or any(
                    not isinstance(spec, str)
                    or any(not _NAME.fullmatch(name) for name in spec.split("|"))
                    or len(set(spec.split("|"))) != len(spec.split("|"))
                    for spec in fields
                )
                or len(set(fields)) != len(fields)
            ):
                raise PreparationError(
                    "Required fields must be unique lowercase names or 'field|alternative'"
                )


_DEFAULT_OPTIONS = BibliographyOptions()


@dataclass(frozen=True)
class _Reference:
    document: _Document
    entry: _Entry

    @property
    def identity(self) -> tuple[str, int]:
        return self.document.path, self.entry.start


@dataclass
class _Graph:
    main: str | None
    resources: set[str] = field(default_factory=set)
    citations: list[tuple[str, str, int]] = field(default_factory=list)
    inline_keys: set[str] = field(default_factory=set)
    nocite_all: bool = False
    reasons: list[str] = field(default_factory=list)
    unused_reasons: list[str] = field(default_factory=list)


def _keys(value: str) -> list[str] | None:
    keys = [part.strip() for part in value.split(",")]
    if not keys or any(
        not key or any(char.isspace() or char in "\\{}#$^~&%\x00" for char in key) for key in keys
    ):
        return None
    return keys


def _reason_details(reasons: list[str]) -> list[str]:
    unique = sorted(set(reasons))
    if len(unique) > _MAX_REASONS:
        return [
            *unique[:_MAX_REASONS],
            f"{len(unique) - _MAX_REASONS} further uncertainties omitted",
        ]
    return unique


def _graph(inspection: _Inspection) -> _Graph:
    graph = _Graph(inspection.main)
    selected = _reachable_sources(inspection)
    if inspection.main not in selected:
        graph.reasons.append("A unique readable main source could not be selected")
    if not inspection.main:
        return graph
    scope = selected | {inspection.main}
    for item in inspection.findings:
        if item.path in scope and item.rule in _GRAPH_FAILURES:
            graph.reasons.append(f"{item.path}: {item.message}")
    for name in sorted(selected):
        cancellation_point()
        source = inspection.sources[name]
        commands = _commands_before_end(source)
        for command in commands:
            if command.name == "subfile":
                graph.reasons.append(
                    f"{name}:{command.line}: subfile bibliography content context is unsupported"
                )
            if command.name.startswith(("if", "If")) or command.name in _DYNAMIC:
                graph.reasons.append(
                    f"{name}:{command.line}: conditional or macro execution is not expanded"
                )
            if command.name == "endinput":
                # Tokens on its physical line may still run, even though lexical
                # traversal deliberately stops before them.
                graph.reasons.append(
                    f"{name}:{command.line}: endinput token buffering is unverified"
                )
            if command.name in _FILTERS or (
                command.name == "begin"
                and command.arguments
                and command.arguments[0].value.strip() in {"refsection", "refsegment"}
            ):
                graph.reasons.append(
                    f"{name}:{command.line}: bibliography scope/filter is unsupported"
                )
            if command.name == "printbibliography" and command.options:
                graph.unused_reasons.append(
                    f"{name}:{command.line}: bibliography print filters/options are not interpreted"
                )
            if command.name in {"bibliography", "addbibresource"} and command.arguments:
                for argument in _items(command.arguments[0], command.name == "bibliography"):
                    target = inspection.references.get((name, argument.start, argument.end))
                    if target and target[1] in {"bibliography", "addbibresource"}:
                        graph.resources.add(target[0])
                    else:
                        graph.reasons.append(
                            f"{name}:{command.line}: bibliography resource could not be resolved"
                        )
            citation = command.name.lower() in _CITATIONS | _MULTICITES
            inline = command.name == "bibitem"
            if not citation and not inline:
                if "cite" in command.name.lower() and command.name not in _DYNAMIC:
                    graph.reasons.append(
                        f"{name}:{command.line}: unsupported citation-like command \\{command.name}"
                    )
                continue
            arguments = (
                command.arguments if command.name.lower() in _MULTICITES else command.arguments[:1]
            )
            if not arguments:
                graph.reasons.append(
                    f"{name}:{command.line}: citation has no supported key argument"
                )
            if command.depth:
                graph.reasons.append(
                    f"{name}:{command.line}: grouped citation execution is unverified"
                )
            if (
                command.name.lower() in _MULTICITES
                and len(command.arguments) + len(command.options) >= 6
            ):
                graph.reasons.append(
                    f"{name}:{command.line}: multicite exceeds lexical argument coverage"
                )
            for argument in arguments:
                keys = _keys(argument.value)
                if keys is None or source.text[argument.start : argument.end] != argument.value:
                    graph.reasons.append(
                        f"{name}:{command.line}: citation key contains dynamic markup"
                    )
                    continue
                for key in keys:
                    if key == "*" and command.name.lower() == "nocite":
                        graph.nocite_all = True
                    elif inline:
                        graph.inline_keys.add(key)
                    else:
                        graph.citations.append((key, name, command.line))
    graph.reasons = _reason_details(graph.reasons)
    graph.unused_reasons = _reason_details(graph.unused_reasons)
    return graph


def _load_documents(root: Path, names: set[str]) -> tuple[list[_Document], list[str]]:
    documents: list[_Document] = []
    reasons: list[str] = []
    total = entries = 0
    for index, name in enumerate(sorted(names)):
        cancellation_point()
        if index >= _MAX_DOCUMENTS:
            reasons.append(f"Bibliography resource count exceeds {_MAX_DOCUMENTS}")
            break
        path = root / name
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("resource is missing or is a symbolic link")
            allowance = min(_MAX_FILE_BYTES, _MAX_TOTAL_BYTES - total)
            if path.stat().st_size > allowance:
                raise ValueError("resource exceeds the per-file or total bibliography byte budget")
            with path.open("rb") as stream:
                raw = stream.read(allowance + 1)
            if len(raw) > allowance:
                raise ValueError("resource exceeds the per-file or total bibliography byte budget")
            total += len(raw)
            document = _Document(name, raw.decode("utf-8-sig"))
            _Parser(document).parse()
        except (OSError, UnicodeError, ValueError) as error:
            reasons.append(f"{name}: {error}")
            continue
        if not document.valid:
            reasons.append(f"{name}: bibliography syntax is incomplete")
        entries += len(document.entries)
        if entries > _MAX_ENTRIES:
            reasons.append(f"Bibliography entry count exceeds {_MAX_ENTRIES}")
            break
        documents.append(document)
    return documents, reasons


def _scalar(value: _Field) -> str | None:
    if len(value.atoms) != 1 or value.atoms[0].kind not in {"literal", "number"}:
        return None
    text = value.atoms[0].value.strip()
    if len(text) > _MAX_FIELD_CHARS or "\\" in text:
        return None
    return text


def _fields(entry: _Entry, name: str) -> list[_Field]:
    return [item for item in entry.fields if item.name == name]


def _content_state(entry: _Entry, names: list[str]) -> str:
    uncertain = False
    for name in names:
        matches = _fields(entry, name)
        if len(matches) > 1:
            uncertain = True
        elif matches:
            value = _scalar(matches[0])
            if value is None:
                uncertain = True
            elif value.translate(str.maketrans("", "", "{}")).strip():
                return "present"
    if uncertain or any(item.name in _INHERITANCE for item in entry.fields):
        return "unknown"
    return "missing"


def _finding(
    rule: str,
    message: str,
    status: str,
    *,
    reference: _Reference | None = None,
    severity: str = "warning",
    evidence: str = "derived",
    details: dict[str, object] | None = None,
) -> Finding:
    data = dict(details or {})
    if reference:
        data.update({"entry": reference.entry.key, "entry_type": reference.entry.kind})
    return Finding(
        f"bibliography.{rule}",
        message,
        severity="info" if status == "passed" else severity,
        status=status,
        path=reference.document.path if reference else None,
        line=reference.document.line(reference.entry.start) if reference else None,
        evidence=evidence,
        details=data,
    )


def _summarize(
    rule: str,
    findings: list[Finding],
    checked: int,
    reasons: list[str],
    *,
    scope: dict[str, object],
    description: str,
    severity: str = "warning",
) -> list[Finding]:
    if reasons:
        findings.append(
            _finding(
                rule,
                f"{description} is incomplete: {reasons[0]}.",
                "inconclusive",
                severity=severity,
                details={**scope, "checked": checked, "uncertainty": _reason_details(reasons)},
            )
        )
    elif not findings:
        findings.append(
            _finding(
                rule,
                f"{description}: no issue found in {checked} supported item(s).",
                "passed",
                details={**scope, "checked": checked},
            )
        )
    return findings


def _entry_index(
    references: list[_Reference],
) -> tuple[dict[str, list[_Reference]], dict[tuple[str, int], set[str]], list[str]]:
    keys: dict[str, list[_Reference]] = defaultdict(list)
    relationships: dict[tuple[str, int], set[str]] = defaultdict(set)
    reasons: list[str] = []
    sets: list[tuple[str, list[str]]] = []
    for reference in references:
        cancellation_point()
        entry = reference.entry
        if entry.key:
            keys[entry.key].append(reference)
        for item in entry.fields:
            if item.name not in _RELATIONS | {"ids"}:
                continue
            value = _scalar(item)
            targets = _keys(value) if value is not None else None
            if targets is None:
                reasons.append(f"{reference.document.path}: {entry.key} has dynamic {item.name}")
            elif item.name == "ids":
                for target in targets:
                    keys[target].append(reference)
            else:
                relationships[reference.identity].update(targets)
                if item.name == "entryset" and entry.key:
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
    return keys, relationships, _reason_details(reasons)


def _coverage(
    graphs: list[_Graph],
    references: list[_Reference],
    reasons: list[str],
    options: BibliographyOptions,
    scope: dict[str, object],
) -> list[Finding]:
    findings: list[Finding] = []
    used: set[tuple[str, int]] = set()
    unused_reasons = list(reasons)
    for graph in graphs:
        local = [item for item in references if item.document.path in graph.resources]
        keys, relationships, key_reasons = _entry_index(local)
        uncertain = [*reasons, *graph.reasons, *key_reasons]
        uncertainty_details = _reason_details(uncertain)
        unused_reasons.extend([*uncertain, *graph.unused_reasons])
        coverage: list[Finding] = []
        queue: deque[_Reference] = deque(local if graph.nocite_all else [])
        for key, path, line in graph.citations:
            cancellation_point()
            if key in keys:
                queue.extend(keys[key])
            elif key not in graph.inline_keys:
                coverage.append(
                    Finding(
                        "bibliography.citation_coverage",
                        f"Citation key {key!r} has no entry in this root's inspected resources.",
                        severity="warning" if uncertain else "error",
                        status="inconclusive" if uncertain else "failed",
                        path=path,
                        line=line,
                        evidence="derived",
                        details={
                            "key": key,
                            "document": graph.main,
                            "resources": sorted(graph.resources),
                            "uncertainty": uncertainty_details,
                        },
                    )
                )
        if options.check_citation_coverage:
            findings.extend(
                _summarize(
                    "citation_coverage",
                    coverage,
                    len(graph.citations),
                    uncertain,
                    scope={**scope, "document": graph.main, "resources": sorted(graph.resources)},
                    description="Supported literal citation coverage",
                )
            )
        reached: set[tuple[str, int]] = set()
        while queue:
            cancellation_point()
            reference = queue.popleft()
            if reference.identity in reached:
                continue
            reached.add(reference.identity)
            for target in relationships.get(reference.identity, set()):
                queue.extend(keys.get(target, []))
        used.update(reached)
    if options.check_uncited_entries:
        unused: list[Finding] = []
        uncertainty_details = _reason_details(unused_reasons)
        for reference in references:
            cancellation_point()
            if reference.identity in used:
                continue
            unused.append(
                _finding(
                    "uncited_entries",
                    f"Entry {reference.entry.key!r} has no scanned use in the selected scope.",
                    "inconclusive" if unused_reasons else "failed",
                    reference=reference,
                    evidence="heuristic",
                    details={**scope, "uncertainty": uncertainty_details},
                )
            )
        findings.extend(
            _summarize(
                "uncited_entries",
                unused,
                len(references),
                unused_reasons,
                scope=scope,
                description="Citation and relationship reachability",
            )
        )
    return findings


def _page_state(value: str) -> tuple[str, str]:
    if not value or any(character in value for character in "{}"):
        return ("failed", "empty page field") if not value else ("inconclusive", "brace markup")
    unknown = False
    for part in value.split(","):
        part = part.strip()
        if re.fullmatch(r"[A-Za-z]*\d+", part):
            continue
        match = re.fullmatch(r"([A-Za-z]*)(\d+)\s*(?:--?|[–—])\s*([A-Za-z]*)(\d+)", part)
        if match:
            first_prefix, first, last_prefix, last = match.groups()
            if first_prefix != last_prefix:
                unknown = True
            elif (len(first.lstrip("0")), first.lstrip("0")) > (
                len(last.lstrip("0")),
                last.lstrip("0"),
            ):
                return "failed", "descending page endpoints"
        elif not part or re.fullmatch(r"[\d\s–—-]+", part):
            return "failed", "malformed numeric page range"
        else:
            unknown = True
    return ("inconclusive", "unsupported page notation") if unknown else ("passed", "")


def _url_state(value: str) -> tuple[str, str]:
    if any(character in value for character in "{}"):
        return "inconclusive", "brace markup"
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return "failed", "empty URL, whitespace or control characters"
    if re.search(r"%(?![0-9a-fA-F]{2})", value) or any(char in value for char in '<>"'):
        return "failed", "invalid percent escape or reserved delimiter"
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return "failed", "an absolute URL scheme is missing"
        if parts.scheme.lower() not in {"http", "https", "ftp"}:
            return "inconclusive", "URL scheme is outside the supported HTTP/HTTPS/FTP subset"
        if not parts.netloc or not parts.hostname:
            return "failed", "a network host is missing"
        parts.hostname.encode("idna")
        _ = parts.port
    except (ValueError, UnicodeError):
        return "failed", "host or port syntax is malformed"
    return "passed", ""


def _unprotected_tokens(value: str) -> list[str]:
    depth = 0
    plain: list[str] = []
    for index, char in enumerate(value):
        if index % 4096 == 0:
            cancellation_point()
        if char == "{":
            depth += 1
            plain.append(" ")
        elif char == "}":
            depth = max(0, depth - 1)
            plain.append(" ")
        else:
            plain.append(char if depth == 0 else " ")
    return sorted(
        {
            token
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", "".join(plain))
            if sum(char.isupper() for char in token) >= 2
            or any(char.isupper() for char in token[1:])
        }
    )


def _metadata(
    references: list[_Reference],
    reasons: list[str],
    options: BibliographyOptions,
    scope: dict[str, object],
) -> list[Finding]:
    findings: list[Finding] = []
    requirements = dict(options.required_fields)
    enabled = {
        "required_fields": bool(requirements),
        "missing_doi": bool(options.doi_entry_types),
        "page_ranges": options.check_page_ranges,
        "url_syntax": options.check_urls,
        "capitalization": options.check_capitalization,
    }
    rows: dict[str, list[Finding]] = {name: [] for name, active in enabled.items() if active}
    counts: dict[str, int] = defaultdict(int)
    for reference in references:
        cancellation_point()
        entry = reference.entry
        for spec in (*requirements.get("*", ()), *requirements.get(entry.kind, ())):
            counts["required_fields"] += 1
            state = _content_state(entry, spec.split("|"))
            if state == "missing" and reasons:
                state = "unknown"
            if state != "present":
                rows["required_fields"].append(
                    _finding(
                        "required_fields",
                        f"Entry {entry.key!r}: required field {spec!r} is "
                        + (
                            "missing or empty."
                            if state == "missing"
                            else "not statically established."
                        ),
                        "failed" if state == "missing" else "inconclusive",
                        reference=reference,
                        severity="error",
                        details={"requirement": spec},
                    )
                )
        if "*" in options.doi_entry_types or entry.kind in options.doi_entry_types:
            counts["missing_doi"] += 1
            state = _content_state(entry, ["doi"])
            if state != "present":
                rows["missing_doi"].append(
                    _finding(
                        "missing_doi",
                        f"Entry {entry.key!r}: DOI presence is "
                        + (
                            "missing or empty; a DOI may not exist."
                            if state == "missing"
                            else "unverified."
                        ),
                        "failed" if state == "missing" else "inconclusive",
                        reference=reference,
                    )
                )
        for rule, name, active in (
            ("page_ranges", "pages", options.check_page_ranges),
            ("url_syntax", "url", options.check_urls),
            ("capitalization", "title", options.check_capitalization),
        ):
            if not active:
                continue
            fields = _fields(entry, name)
            if not fields:
                continue
            counts[rule] += 1
            value = _scalar(fields[0]) if len(fields) == 1 else None
            tokens: list[str] = []
            if value is None:
                status, reason = "inconclusive", "macro, concatenation, markup or repeated field"
            elif rule == "page_ranges":
                status, reason = _page_state(value)
            elif rule == "url_syntax":
                status, reason = _url_state(value)
            else:
                tokens = _unprotected_tokens(value)
                status, reason = (
                    ("failed", "case-sensitive tokens may need brace protection")
                    if tokens
                    else ("passed", "")
                )
            if status != "passed":
                rows[rule].append(
                    _finding(
                        rule,
                        f"Entry {entry.key!r}: {name} check found {reason}.",
                        status,
                        reference=reference,
                        evidence="heuristic" if rule == "capitalization" else "derived",
                        details={"field": name, **({"tokens": tokens} if tokens else {})},
                    )
                )
    for rule, row in rows.items():
        findings.extend(
            _summarize(
                rule,
                row,
                counts[rule],
                reasons,
                scope=scope,
                description={
                    "required_fields": "Configured field presence",
                    "missing_doi": "Requested DOI presence",
                    "page_ranges": "Literal page-range syntax",
                    "url_syntax": "Literal URL syntax (offline)",
                    "capitalization": "Style-dependent capitalization advisory",
                }[rule],
                severity="error" if rule == "required_fields" else "warning",
            )
        )
    return findings


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def _fuzzy(
    references: list[_Reference],
    reasons: list[str],
    options: BibliographyOptions,
    scope: dict[str, object],
) -> list[Finding]:
    findings: list[Finding] = []
    reasons = list(reasons)
    groups: dict[tuple[str, str], list[tuple[_Reference, str, str | None]]] = defaultdict(list)
    for index, reference in enumerate(references):
        cancellation_point()
        if index >= _MAX_FUZZY_ENTRIES:
            reasons.append(f"Fuzzy entry count exceeds {_MAX_FUZZY_ENTRIES}")
            break
        values: dict[str, str] = {}
        for name in ("title", "author", "year"):
            fields = _fields(reference.entry, name)
            if name == "year" and not fields:
                fields = _fields(reference.entry, "date")
            value = _scalar(fields[0]) if len(fields) == 1 else None
            if value is not None and len(value) <= _MAX_FUZZY_TEXT:
                values[name] = _normalized(value)
        if (
            len(values) != 3
            or not all(values.values())
            or re.fullmatch(r"\d{4}(?: \d{2}){0,2}", values["year"]) is None
        ):
            reasons.append(
                f"{reference.document.path}: {reference.entry.key} "
                "lacks supported title/author/year evidence"
            )
            continue
        doi_fields = _fields(reference.entry, "doi")
        doi = _doi_value(doi_fields[0])[0] if len(doi_fields) == 1 else None
        groups[(values["author"], values["year"][:4])].append((reference, values["title"], doi))
    comparisons = 0
    for group in groups.values():
        for index, (reference, title, doi) in enumerate(group):
            for previous, other_title, other_doi in group[:index]:
                cancellation_point()
                if comparisons >= _MAX_FUZZY_PAIRS:
                    reasons.append(f"Fuzzy comparison count exceeds {_MAX_FUZZY_PAIRS}")
                    return _summarize(
                        "fuzzy_duplicates",
                        findings,
                        comparisons,
                        reasons,
                        scope=scope,
                        description="Likely-work duplicate search",
                    )
                comparisons += 1
                if doi is not None and doi == other_doi:
                    continue  # Exact DOI identity belongs to BIB007, not a title heuristic.
                similarity = SequenceMatcher(None, title, other_title, autojunk=False).ratio()
                if similarity >= options.fuzzy_duplicate_threshold:
                    findings.append(
                        _finding(
                            "fuzzy_duplicates",
                            f"Entries {previous.entry.key!r} and {reference.entry.key!r} "
                            "may describe the same work; identity and version require review.",
                            "failed",
                            reference=reference,
                            evidence="heuristic",
                            details={
                                "first_entry": previous.entry.key,
                                "first_path": previous.document.path,
                                "first_line": previous.document.line(previous.entry.start),
                                "title_similarity": similarity,
                                "threshold": options.fuzzy_duplicate_threshold,
                                "matching": "normalized author and year",
                            },
                        )
                    )
    return _summarize(
        "fuzzy_duplicates",
        findings,
        comparisons,
        reasons,
        scope=scope,
        description="Likely-work duplicate search",
    )


def check_bibliography_details(
    root: Path,
    main: str | None = None,
    options: BibliographyOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Inspect offline evidence without writing files or resolving remote metadata.

    Metadata in a bibliography-only directory can be inspected without a TeX root;
    citation coverage then remains inconclusive. With TeX roots, only selected
    resources are checked. ``all_roots`` unions usage but resolves keys separately
    for each root. Passed summaries describe only the supported literal subset.
    """
    if not isinstance(options, BibliographyOptions):
        raise PreparationError("Bibliography checks require BibliographyOptions")
    if not any(
        (
            options.check_citation_coverage,
            options.check_uncited_entries,
            options.required_fields,
            options.doi_entry_types,
            options.check_page_ranges,
            options.check_urls,
            options.check_capitalization,
            options.check_fuzzy_duplicates,
        )
    ):
        return []
    inspection = _Inspection(root, main)
    graphs = [_graph(inspection)]
    reasons: list[str] = []
    if options.all_roots:
        roots = inspection.roots
        for item in inspection.findings:
            if (
                item.path
                and Path(item.path).suffix.lower() in {".tex", ".ltx", ".latex"}
                and item.rule
                in {
                    "source-encoding",
                    "source-size-limit",
                    "source-argument-limit",
                    "source-command-limit",
                    "source-link",
                }
            ):
                reasons.append(f"All-root discovery is incomplete: {item.path}: {item.message}")
        if len(roots) > _MAX_ROOTS:
            reasons.append(f"Document root count exceeds {_MAX_ROOTS}")
        graphs = []
        for selected in roots[:_MAX_ROOTS]:
            cancellation_point()
            graphs.append(
                _graph(inspection if inspection.main == selected else _Inspection(root, selected))
            )
        if not graphs:
            graphs = [_graph(inspection)]
    names = set().union(*(graph.resources for graph in graphs))
    metadata_scope_reasons = list(reasons)
    if not names and not inspection.roots and main is None:
        # A standalone .bib command has no manuscript resource selection. Its
        # explicitly stated metadata scope is every local bibliography file.
        names = {name for name in inspection.files if Path(name).suffix.lower() == ".bib"}
    elif not names and any(graph.reasons for graph in graphs):
        metadata_scope_reasons.append("No complete bibliography resource selection is available")
    if inspection.roots or main is not None:
        metadata_scope_reasons.extend(reason for graph in graphs for reason in graph.reasons)
    documents, input_reasons = _load_documents(root, names)
    reasons.extend(input_reasons)
    metadata_scope_reasons.extend(input_reasons)
    references = [
        _Reference(document, entry)
        for document in documents
        if document.valid
        for entry in document.entries
        if entry.key is not None
    ]
    scope: dict[str, object] = {
        "scope": "all literal document roots"
        if options.all_roots
        else "selected literal document graph",
        "documents": [graph.main for graph in graphs if graph.main],
        "resources": sorted(names),
        "source_expansion": "not performed",
        "network": "not used",
    }
    if not inspection.roots and main is None:
        scope["scope"] = "bibliography-only directory; citation scope unavailable"
    findings = _coverage(graphs, references, reasons, options, scope)
    findings.extend(_metadata(references, metadata_scope_reasons, options, scope))
    if options.check_fuzzy_duplicates:
        findings.extend(_fuzzy(references, metadata_scope_reasons, options, scope))
    return findings
