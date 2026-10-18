"""Shipped generated bibliography (.bbl): presence, citation coverage and biblatex format.

Most venues run BibTeX or Biber themselves, so a missing .bbl is only a scope note
unless the user requires one. Coverage and format checks run only when configured,
and their missing evidence is inconclusive, not a pass. No source is modified.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .cancellation import cancellation_point
from .citation_graph import build_citation_graph, resource_declarations
from .expanded_document import commands_before_end
from .file_scan import MAX_SCAN_MATCHES, list_locations, more_suffix, read_regular_file
from .findings import EvidenceKind, Finding
from .literal_text import PACKAGE_COMMANDS, first_argument_names
from .rules import code_for
from .source_graph import SourceGraph
from .submission_checks import SubmissionOptions
from .tex_lexer import MAX_SOURCE_BYTES

_BBL_RULE = "source.bibliography_bbl"
_COVERAGE_RULE = "bibliography.bbl_coverage"
_FORMAT_RULE = "bibliography.bbl_format_version"
_MAX_LISTED = 3
# biblatex compares exactly these first two lines when it reads a .bbl.
_BIBLATEX_BBL_SIGNATURE = "% $ biblatex auxiliary file $"
_BIBLATEX_BBL_VERSION = re.compile(r"% \$ biblatex bbl format version (\S+) \$")


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


def _bibitem_keys(text: str) -> set[str]:
    """Keys of BibTeX \\bibitem[label]{key}; the label may contain nested brackets."""
    keys: set[str] = set()
    for match in re.finditer(r"\\bibitem\b", text):
        cancellation_point()
        cursor = match.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if text.startswith("[", cursor):
            depth = brackets = 0
            limit = min(len(text), cursor + 4096)  # A label never spans a whole file.
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
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
        key = re.match(r"\{([^{}]+)\}", text[cursor:])
        if key:
            keys.add(key.group(1).strip())
    return keys


def _bbl_keys(text: str) -> tuple[set[str], str]:
    if "\\datalist" in text or re.search(r"\\entry\s*\{", text):
        keys = set(re.findall(r"\\entry\s*\{([^{}]+)\}\s*\{", text))
        keys.update(re.findall(r"\\keyalias\s*\{([^{}]+)\}", text))
        return {key.strip() for key in keys}, "biblatex"
    return _bibitem_keys(text), "bibtex"


def _read_bbl(root: Path, expected: str) -> str:
    return read_regular_file(root / expected, MAX_SOURCE_BYTES).decode("utf-8")


def check_bbl(
    root: Path, graph: SourceGraph, selected: set[str], options: SubmissionOptions
) -> list[Finding]:
    """Check the .bbl named after the main file; ``graph`` must have a selected main."""
    main = graph.main
    assert main is not None
    resources, _ = resource_declarations(graph, selected)
    bib = sorted(name for name in resources if name.lower().endswith(".bib"))
    expected = (PurePosixPath(main).parent / (PurePosixPath(main).stem + ".bbl")).as_posix()
    if not bib:
        return _without_bib(main, options)
    backend = "biblatex" if _uses_biblatex(graph, selected) else "BibTeX"
    present = expected in graph.files
    findings = [_presence(graph, selected, main, expected, bib, backend, options.require_bbl)]
    if options.check_bbl_coverage:
        findings.append(_coverage(root, graph, expected, present))
    if options.biblatex_bbl_format_version is not None:
        findings.append(
            _bbl_format(root, expected, present, backend, options.biblatex_bbl_format_version)
        )
    return findings


def _without_bib(main: str, options: SubmissionOptions) -> list[Finding]:
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


def _presence(
    graph: SourceGraph,
    selected: set[str],
    main: str,
    expected: str,
    bib: list[str],
    backend: str,
    require_bbl: bool,
) -> Finding:
    others = sorted(
        name for name in graph.files if name.lower().endswith(".bbl") and name != expected
    )
    details: dict[str, object] = {
        "bibliography_files": bib,
        "backend": backend,
        "expected_bbl": expected,
        "other_bbl_files": others[:MAX_SCAN_MATCHES],
        "require_bbl": require_bbl,
        "scope": "Literal bibliography declarations in the selected source graph; the shipped "
        "file set is the supplied or prepared directory.",
    }
    if expected in graph.files:
        return Finding(
            _BBL_RULE,
            f"{expected} is shipped beside {main} for its {backend} bibliography.",
            severity="info",
            status="passed",
            path=expected,
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
    if require_bbl:
        consequence = (
            "Services that compile without running BibTeX or Biber, such as arXiv, would "
            "render empty citations."
        )
    else:
        consequence = (
            "Only services that compile without running BibTeX or Biber, such as arXiv, "
            "need it; set submission_checks.require_bbl = true (the arxiv preset does) to "
            "require it."
        )
    path, line = _first_declaration(graph, selected, {"bibliography", "addbibresource"})
    return Finding(
        _BBL_RULE,
        f"{main} reads {', '.join(bib[:_MAX_LISTED])} through {backend}, but {expected} "
        f"is not in the package. {consequence}{elsewhere}",
        severity="warning" if require_bbl else "info",
        status="failed" if require_bbl else "not_applicable",
        path=path or main,
        line=line,
        evidence_kind=EvidenceKind.DERIVED,
        details=details,
    )


def _bbl_format(root: Path, expected: str, present: bool, backend: str, target: str) -> Finding:
    details: dict[str, object] = {"bbl": expected, "target_version": target, "backend": backend}
    if not present:
        if backend != "biblatex":
            return Finding(
                _FORMAT_RULE,
                f"The document uses {backend}, so no biblatex .bbl format applies.",
                severity="info",
                status="not_applicable",
                path=expected,
                details=details,
            )
        # The presence rule reports the missing file itself, as coverage does.
        blocked_by = code_for(_BBL_RULE)
        return Finding(
            _FORMAT_RULE,
            f"The biblatex .bbl format was not checked: no {expected} is shipped ({blocked_by}).",
            severity="warning",
            status="inconclusive",
            path=expected,
            suggestion=f"Build locally and ship the generated {expected} beside the main file; "
            "its format is then checked.",
            details={**details, "blocked_by": blocked_by},
        )
    try:
        text = _read_bbl(root, expected)
    except (OSError, UnicodeDecodeError):
        return Finding(
            _FORMAT_RULE,
            f"{expected} is unreadable, linked, not UTF-8 or over the scan limit.",
            severity="error",
            status="inconclusive",
            path=expected,
            details=details,
        )
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


def _coverage(root: Path, graph: SourceGraph, expected: str, present: bool) -> Finding:
    if not present:
        # The presence rule reports the missing file itself; this result only
        # records that coverage could not be measured, so it does not block on its own.
        blocked_by = code_for(_BBL_RULE)
        return Finding(
            _COVERAGE_RULE,
            f"Citation coverage was not checked: no {expected} is shipped ({blocked_by}).",
            severity="warning",
            status="inconclusive",
            path=expected,
            suggestion=f"Build locally and ship the generated {expected} beside the main "
            "file; coverage is then checked.",
            details={"blocked_by": blocked_by, "bbl": expected},
        )
    try:
        text = _read_bbl(root, expected)
    except (OSError, UnicodeDecodeError):
        return Finding(
            _COVERAGE_RULE,
            f"{expected} is unreadable, linked, not UTF-8 or over the scan limit.",
            severity="error",
            status="inconclusive",
            path=expected,
        )
    keys, form = _bbl_keys(text)
    citations = build_citation_graph(graph)
    first: dict[str, dict[str, object]] = {}
    for key, path, line in citations.citations:
        first.setdefault(key, {"key": key, "path": path, "line": line})
    missing = [first[key] for key in sorted(set(first) - keys)]
    details: dict[str, object] = {
        "bbl": expected,
        "bbl_format": form,
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
