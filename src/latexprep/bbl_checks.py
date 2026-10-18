"""Shipped generated bibliography (.bbl): presence, citation coverage and biblatex format.

Most venues run BibTeX or Biber themselves, so a missing .bbl is only a scope note
unless the user requires one. Coverage and format checks run only when configured,
and their missing evidence is inconclusive, not a pass. No source is modified.

This module owns reading the keys out of a generated .bbl (BibTeX's ``\\bibitem`` form
and biblatex's ``\\entry`` form), the checks that compare them with the citations of
the selected manuscript, and their options, ``BblOptions``, which a settings file writes
in the ``[submission_checks]`` table. The .bib files themselves are read by
``bibtex_syntax``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .cancellation import cancellation_point
from .citation_graph import build_citation_graph, resource_declarations
from .expanded_document import commands_before_end
from .file_safety import read_regular_file
from .file_scan import MAX_SCAN_MATCHES
from .finding_text import list_locations, more_suffix
from .findings import EvidenceKind, Finding
from .literal_text import PACKAGE_COMMANDS, first_argument_names
from .option_config import require_text, validate_scalars
from .rules import code_for
from .source_graph import SourceGraph
from .tex_lexer import MAX_SOURCE_BYTES

_BBL_RULE = "source.bibliography_bbl"
_COVERAGE_RULE = "bibliography.bbl_coverage"
_FORMAT_RULE = "bibliography.bbl_format_version"
_MAX_LISTED = 3
# A label in brackets after \bibitem never spans a whole file.
_MAX_LABEL_CHARACTERS = 4096
# biblatex compares exactly these first two lines when it reads a .bbl.
_BIBLATEX_BBL_SIGNATURE = "% $ biblatex auxiliary file $"
_BIBLATEX_BBL_VERSION = re.compile(r"% \$ biblatex bbl format version (\S+) \$")
_BIBITEM = re.compile(r"\\bibitem\b")
_BIBITEM_KEY = re.compile(r"\{([^{}]+)\}")
_ENTRY_START = re.compile(r"\\entry\s*\{")
_ENTRY_KEY = re.compile(r"\\entry\s*\{([^{}]+)\}\s*\{")
_KEY_ALIAS = re.compile(r"\\keyalias\s*\{([^{}]+)\}")
_BBL_FORMAT_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}")


def validate_bbl_format_version(value: object) -> None:
    """Reject a biblatex .bbl format version that is not a short dotted number, or ``None``."""
    require_text(
        "biblatex_bbl_format_version",
        value,
        expected='a version such as "3.3", as written on the second line of a biblatex .bbl file',
        pattern=_BBL_FORMAT_VERSION,
        allow_none=True,
    )


@dataclass(frozen=True)
class BblOptions:
    """Which checks of the shipped .bbl run: presence, citation coverage and biblatex format."""

    check_bbl_coverage: bool = False
    require_bbl: bool = False
    biblatex_bbl_format_version: str | None = None

    def __post_init__(self) -> None:
        validate_scalars(self)
        validate_bbl_format_version(self.biblatex_bbl_format_version)


class Backend(StrEnum):
    """The bibliography backend a document uses; the values are the reported strings."""

    BIBTEX = "bibtex"
    BIBLATEX = "biblatex"

    @property
    def display_name(self) -> str:
        """The backend as a researcher writes it."""
        return "BibTeX" if self is Backend.BIBTEX else "biblatex"


@dataclass(frozen=True, slots=True)
class _Plan:
    """What a document's bibliography declarations imply about its .bbl."""

    main: str
    expected: str
    bib: list[str]
    backend: Backend
    present: bool


class BblKeys(NamedTuple):
    """The keys a .bbl defines and the backend whose form it has."""

    keys: set[str]
    backend: Backend


def _first_declaration(
    graph: SourceGraph, selected: set[str], names: set[str]
) -> tuple[str | None, int | None]:
    for name in sorted(selected, key=lambda item: (item != graph.main, item)):
        for command in commands_before_end(graph.sources[name]):
            if command.name in names:
                return name, command.line
    return None, None


def _uses_biblatex(graph: SourceGraph, selected: set[str]) -> bool:
    for name in selected:
        for command in commands_before_end(graph.sources[name]):
            if command.name == "addbibresource":
                return True
            if command.name in PACKAGE_COMMANDS and "biblatex" in (
                first_argument_names(command) or []
            ):
                return True
    return False


def _skip_label(text: str, cursor: int) -> int:
    """The offset after the optional ``[label]``; its brackets and braces may nest."""
    depth = brackets = 0
    limit = min(len(text), cursor + _MAX_LABEL_CHARACTERS)
    while cursor < limit:
        char = text[cursor]
        depth += char == "{"
        depth -= char == "}"
        if depth == 0:
            brackets += char == "["
            brackets -= char == "]"
        cursor += 1
        if depth == 0 and brackets == 0:
            break
    return cursor


def _skip_space(text: str, cursor: int) -> int:
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _bibitem_keys(text: str) -> set[str]:
    """Keys of BibTeX ``\\bibitem[label]{key}``; the label may contain nested brackets."""
    keys: set[str] = set()
    for match in _BIBITEM.finditer(text):
        cancellation_point()
        cursor = _skip_space(text, match.end())
        if text.startswith("[", cursor):
            cursor = _skip_space(text, _skip_label(text, cursor))
        key = _BIBITEM_KEY.match(text, cursor)
        if key:
            keys.add(key.group(1).strip())
    return keys


def bbl_keys(text: str) -> BblKeys:
    """The keys defined by the text of a .bbl, read in the form its backend writes."""
    if "\\datalist" in text or _ENTRY_START.search(text):
        keys = set(_ENTRY_KEY.findall(text))
        keys.update(_KEY_ALIAS.findall(text))
        return BblKeys({key.strip() for key in keys}, Backend.BIBLATEX)
    return BblKeys(_bibitem_keys(text), Backend.BIBTEX)


def _read_bbl(root: Path, expected: str) -> str | None:
    """The text of the shipped .bbl, or None when it is a link, special, not UTF-8 or too big."""
    try:
        return read_regular_file(root / expected, MAX_SOURCE_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _unreadable(rule: str, expected: str, **details: object) -> Finding:
    return Finding(
        rule,
        f"{expected} is unreadable, linked, not UTF-8 or over the scan limit.",
        severity="error",
        status="inconclusive",
        path=expected,
        details=details,
    )


def check_bbl(
    root: Path, graph: SourceGraph, selected: set[str], options: BblOptions
) -> list[Finding]:
    """Check the .bbl named after the main file; ``graph`` must have a selected main."""
    main = graph.main
    if main is None:
        raise ValueError("The .bbl checks need a source graph with a selected main file.")
    resources, _ = resource_declarations(graph, selected)
    bib = sorted(name for name in resources if name.lower().endswith(".bib"))
    if not bib:
        return _without_bib(main, options)
    expected = (PurePosixPath(main).parent / (PurePosixPath(main).stem + ".bbl")).as_posix()
    backend = Backend.BIBLATEX if _uses_biblatex(graph, selected) else Backend.BIBTEX
    plan = _Plan(main, expected, bib, backend, expected in graph.files)
    findings = [_presence(graph, selected, plan, options)]
    if options.check_bbl_coverage:
        findings.append(_coverage(root, graph, plan))
    if options.biblatex_bbl_format_version is not None:
        findings.append(_bbl_format(root, plan, options.biblatex_bbl_format_version))
    return findings


def _without_bib(main: str, options: BblOptions) -> list[Finding]:
    unchecked = []
    if options.check_bbl_coverage:
        unchecked.append((_COVERAGE_RULE, "shipped .bbl coverage"))
    if options.biblatex_bbl_format_version is not None:
        unchecked.append((_FORMAT_RULE, "the biblatex .bbl format"))
    return [
        Finding(
            rule,
            f"{main} declares no literal .bib resource; {subject} was not checked.",
            severity="info",
            status="not_applicable",
            path=main,
        )
        for rule, subject in unchecked
    ]


def _missing_bbl_consequence(*, require_bbl: bool) -> str:
    if require_bbl:
        return (
            "Services that compile without running BibTeX or Biber, such as arXiv, would "
            "render empty citations."
        )
    return (
        "Only services that compile without running BibTeX or Biber, such as arXiv, "
        "need it; set submission_checks.require_bbl = true (the arxiv preset does) to "
        "require it."
    )


def _presence(graph: SourceGraph, selected: set[str], plan: _Plan, options: BblOptions) -> Finding:
    others = sorted(
        name for name in graph.files if name.lower().endswith(".bbl") and name != plan.expected
    )
    details: dict[str, object] = {
        "bibliography_files": plan.bib,
        "backend": plan.backend.value,
        "expected_bbl": plan.expected,
        "other_bbl_files": others[:MAX_SCAN_MATCHES],
        "require_bbl": options.require_bbl,
        "scope": "Literal bibliography declarations in the selected source graph; the shipped "
        "file set is the supplied or prepared directory.",
    }
    if plan.present:
        return Finding(
            _BBL_RULE,
            f"{plan.expected} is shipped beside {plan.main} for its "
            f"{plan.backend.display_name} bibliography.",
            severity="info",
            status="passed",
            path=plan.expected,
            details=details,
        )
    elsewhere = (
        f" Other .bbl files ({', '.join(others[:_MAX_LISTED])}) do not match the main file's name."
        if others
        else ""
    )
    # Most venues run BibTeX or Biber themselves, so without an explicit
    # requirement (for services such as arXiv) a missing .bbl is only a scope
    # note, listed on request rather than as a failed result.
    path, line = _first_declaration(graph, selected, {"bibliography", "addbibresource"})
    return Finding(
        _BBL_RULE,
        f"{plan.main} reads {', '.join(plan.bib[:_MAX_LISTED])} through "
        f"{plan.backend.display_name}, but {plan.expected} is not in the package. "
        f"{_missing_bbl_consequence(require_bbl=options.require_bbl)}{elsewhere}",
        severity="warning" if options.require_bbl else "info",
        status="failed" if options.require_bbl else "not_applicable",
        path=path or plan.main,
        line=line,
        evidence_kind=EvidenceKind.DERIVED,
        details=details,
    )


def _format_without_bbl(plan: _Plan, details: dict[str, object]) -> Finding:
    if plan.backend is not Backend.BIBLATEX:
        return Finding(
            _FORMAT_RULE,
            f"The document uses {plan.backend.display_name}, so no biblatex .bbl format applies.",
            severity="info",
            status="not_applicable",
            path=plan.expected,
            details=details,
        )
    # The presence rule reports the missing file itself, as coverage does.
    blocked_by = code_for(_BBL_RULE)
    return Finding(
        _FORMAT_RULE,
        f"The biblatex .bbl format was not checked: no {plan.expected} is shipped ({blocked_by}).",
        severity="warning",
        status="inconclusive",
        path=plan.expected,
        suggestion=f"Build locally and ship the generated {plan.expected} beside the main file; "
        "its format is then checked.",
        details={**details, "blocked_by": blocked_by},
    )


def _format_of(expected: str, text: str, target: str, details: dict[str, object]) -> Finding:
    lines = [line.rstrip() for line in text.splitlines()[:2]]
    if not lines or lines[0] != _BIBLATEX_BBL_SIGNATURE:
        return Finding(
            _FORMAT_RULE,
            f"{expected} was not written by biblatex, so no biblatex .bbl format applies.",
            severity="info",
            status="not_applicable",
            path=expected,
            details=details,
        )
    match = _BIBLATEX_BBL_VERSION.fullmatch(lines[1]) if len(lines) == 2 else None
    if match is None:
        return Finding(
            _FORMAT_RULE,
            f"{expected} has the biblatex signature but no readable format version on line 2.",
            severity="warning",
            status="inconclusive",
            path=expected,
            line=2,
            details=details,
        )
    found = match[1]
    details["found_version"] = found
    if found != target:
        return Finding(
            _FORMAT_RULE,
            f"{expected} has biblatex .bbl format {found}, but the target service expects "
            f"{target}. biblatex there reports a wrong format version and the bibliography may "
            "be missing or broken.",
            severity="error",
            status="failed",
            path=expected,
            line=2,
            details=details,
        )
    return Finding(
        _FORMAT_RULE,
        f"{expected} has the expected biblatex .bbl format {target}.",
        severity="info",
        status="passed",
        path=expected,
        line=2,
        details=details,
    )


def _bbl_format(root: Path, plan: _Plan, target: str) -> Finding:
    details: dict[str, object] = {
        "bbl": plan.expected,
        "target_version": target,
        "backend": plan.backend.value,
    }
    if not plan.present:
        return _format_without_bbl(plan, details)
    text = _read_bbl(root, plan.expected)
    if text is None:
        return _unreadable(_FORMAT_RULE, plan.expected, **details)
    return _format_of(plan.expected, text, target, details)


def _coverage_not_measured(plan: _Plan) -> Finding:
    # The presence rule reports the missing file itself; this result only
    # records that coverage could not be measured, so it does not block on its own.
    blocked_by = code_for(_BBL_RULE)
    return Finding(
        _COVERAGE_RULE,
        f"Citation coverage was not checked: no {plan.expected} is shipped ({blocked_by}).",
        severity="warning",
        status="inconclusive",
        path=plan.expected,
        suggestion=f"Build locally and ship the generated {plan.expected} beside the main "
        "file; coverage is then checked.",
        details={"blocked_by": blocked_by, "bbl": plan.expected},
    )


def _coverage(root: Path, graph: SourceGraph, plan: _Plan) -> Finding:
    expected = plan.expected
    if not plan.present:
        return _coverage_not_measured(plan)
    text = _read_bbl(root, expected)
    if text is None:
        return _unreadable(_COVERAGE_RULE, expected)
    defined = bbl_keys(text)
    keys = defined.keys
    citations = build_citation_graph(graph)
    first: dict[str, dict[str, object]] = {}
    for key, path, line in citations.citations:
        first.setdefault(key, {"key": key, "path": path, "line": line})
    missing = [first[key] for key in sorted(set(first) - keys)]
    details: dict[str, object] = {
        "bbl": expected,
        "bbl_format": defined.backend.value,
        "bbl_entries": len(keys),
        "cited_keys": len(first),
        "missing": missing[:MAX_SCAN_MATCHES],
        "uncited_bbl_entries": sorted(keys - set(first))[:MAX_SCAN_MATCHES],
        "nocite_all": citations.nocite_all,
        "uncertainty": citations.reasons,
        "scope": "Literal citation keys in the selected source graph against generated .bbl "
        "keys; \\nocite{*} additions and rendered entry content are not checked.",
    }
    if missing:
        names = ", ".join(str(item["key"]) for item in missing[:_MAX_LISTED])
        return Finding(
            _COVERAGE_RULE,
            f"{expected} lacks {len(missing)} cited key(s): {names}"
            f"{more_suffix(len(missing) - _MAX_LISTED)}; first cited at "
            f"{list_locations(missing, _MAX_LISTED)}.",
            severity="error",
            status="failed",
            path=expected,
            evidence_kind=EvidenceKind.DERIVED,
            details=details,
        )
    if citations.reasons:
        return Finding(
            _COVERAGE_RULE,
            f"{expected} covers every literal citation key, but citations remain uncertain: "
            f"{citations.reasons[0]}",
            severity="error",
            status="inconclusive",
            path=expected,
            details=details,
        )
    return Finding(
        _COVERAGE_RULE,
        f"{expected} contains all {len(first)} literal cited key(s).",
        severity="info",
        status="passed",
        path=expected,
        details=details,
    )
