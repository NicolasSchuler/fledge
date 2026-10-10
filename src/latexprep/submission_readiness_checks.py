"""Cheap, deterministic submission-readiness advisories over the literal source graph.

The default-on observations here (missing generated bibliography, hyperref load
order, EPS figures and private-note comment markers) are advisories: they never
encode a publisher's acceptance rules and never block preparation. A missing
generated bibliography is only a note unless the user requires one. Policies that
need a user value (shipped bibliography coverage, raster figure colour space) run
only when explicitly configured; their missing evidence is inconclusive, not a
pass. No source is modified.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from .bibliography import _resource_declarations
from .bibliography_checks import _graph
from .manuscript import _PACKAGE_COMMANDS, _commands_before_end, _literal_list, _reachable_sources
from .manuscript_checks import _definition_spans, _Document, _within
from .manuscript_checks import _read as _read_document
from .models import Finding, PreparationError
from .scheduler import cancellation_point
from .source import MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES, _Inspection, in_conditional
from .submission_checks import MAX_SCAN_MATCHES, _comments, _inventory, _read, _redact

FIGURE_COLOR_SPACES = ("rgb", "cmyk")
_MAX_LISTED = 3
_MAX_IMAGE_HEADER_BYTES = 4 * 1024 * 1024
_MANUSCRIPT_SUFFIXES = {".tex", ".ltx", ".latex"}

# Each package documents that it must be loaded after hyperref. Keep this list
# small: an entry is only added with a documented load-order requirement.
_AFTER_HYPERREF = {
    "cleveref": "patches hyperref's reference commands",
    "hypcap": "patches hyperref's figure anchors",
    "bookmark": "loads hyperref itself, so later hyperref options clash",
    "glossaries": "creates hyperlinks only when hyperref is already loaded",
    "glossaries-extra": "creates hyperlinks only when hyperref is already loaded",
}

# Narrow, explicit private-note markers. Only the category and location are
# reported; comment text is never copied into the report.
_INITIALS_EXCLUDED = frozenset(
    "XXX TBD DOI URL PDF CCS ACM MSC NB PS ID TOC API SQL CPU GPU RAM CSS XML CSV TEX BIB "
    "FIG TAB SEC EQ REF CMD EOF".split()
)
_PRIVATE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "review correspondence",
        re.compile(
            r"(?i)\b(?:reviewers?|referees?)\s*#\s*\d+\b|\b(?:reviewer|referee)\s+\d+\b|"
            r"\b(?:responses?|repl(?:y|ies)|answers?)\s+(?:to|for)\s+(?:the\s+)?"
            r"(?:reviewers?|referees?|editors?)\b|"
            r"\b(?:requested|asked|suggested|raised|demanded)\s+by\s+(?:the\s+)?"
            r"(?:reviewers?|referees?|editors?|meta-?reviewers?)\b|"
            r"\b(?:for|in)\s+(?:the|our)\s+rebuttal\b"
        ),
    ),
    (
        "distribution restriction",
        re.compile(
            r"(?i)\b(?:do\s+not|don['\u2019]?t|never)\s+"
            r"(?:submit|share|distribute|publish|circulate|upload|release)\b|"
            r"\bnot\s+for\s+(?:distribution|publication|submission|circulation)\b|"
            r"\b(?:internal|private)\s+(?:use\s+)?only\b|"
            r"\bconfidential\b(?!\s+(?:comput|transaction|virtual|vms?\b|container|inference|"
            r"data|information|learning|ml\b|ai\b|channel|cloud|enclave))"
        ),
    ),
    (
        "private note",
        re.compile(
            r"(?i)\b(?:private|personal)\s+(?:notes?|comments?|remarks?)\b|"
            r"\bnote\s+to\s+(?:self|myself|ourselves|co-?authors?)\b"
        ),
    ),
)
_INITIALS_NOTE = re.compile(r"^[\s%]*\[?([A-Z]{2,3})\]?\s*:\s+\S")


def _location(item: Mapping[str, object]) -> str:
    path, line = item.get("path"), item.get("line")
    if not isinstance(path, str):
        return ""
    return f"{path}:{line}" if isinstance(line, int) else path


def _listed(items: Sequence[Mapping[str, object]]) -> str:
    places = list(dict.fromkeys(filter(None, (_location(item) for item in items))))
    shown = ", ".join(places[:_MAX_LISTED])
    return shown + (f" (+{len(places) - _MAX_LISTED} more)" if len(places) > _MAX_LISTED else "")


def _first_declaration(
    inspection: _Inspection, selected: set[str], names: set[str]
) -> tuple[str | None, int | None]:
    for name in sorted(selected, key=lambda item: (item != inspection.main, item)):
        for command in _commands_before_end(inspection.sources[name]):
            if command.name in names:
                return name, command.line
    return None, None


def _uses_biblatex(inspection: _Inspection, selected: set[str]) -> bool:
    for name in selected:
        for command in _commands_before_end(inspection.sources[name]):
            if command.name == "addbibresource":
                return True
            if command.name in _PACKAGE_COMMANDS and command.arguments:
                if "biblatex" in (_literal_list(command.arguments[0].value) or []):
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


def _bibliography_findings(
    root: Path,
    inspection: _Inspection,
    selected: set[str],
    check_coverage: bool,
    require_bbl: bool,
) -> list[Finding]:
    main = inspection.main
    assert main is not None
    resources, _ = _resource_declarations(inspection, selected)
    bib = sorted(name for name in resources if name.lower().endswith(".bib"))
    expected = (PurePosixPath(main).parent / (PurePosixPath(main).stem + ".bbl")).as_posix()
    if not bib:
        if not check_coverage:
            return []
        return [
            Finding(
                "bibliography.bbl_coverage",
                f"{main} declares no literal .bib resource; shipped .bbl coverage was not checked.",
                "info",
                "not_applicable",
                path=main,
            )
        ]
    backend = "biblatex" if _uses_biblatex(inspection, selected) else "BibTeX"
    path, line = _first_declaration(inspection, selected, {"bibliography", "addbibresource"})
    others = sorted(
        name for name in inspection.files if name.lower().endswith(".bbl") and name != expected
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
    findings: list[Finding] = []
    present = expected in inspection.files
    if present:
        findings.append(
            Finding(
                "source.bibliography_bbl",
                f"{expected} is shipped beside {main} for its {backend} bibliography.",
                "info",
                "passed",
                path=expected,
                details=details,
            )
        )
    else:
        elsewhere = (
            f" Other .bbl files ({', '.join(others[:_MAX_LISTED])}) do not match the main "
            "file's name."
            if others
            else ""
        )
        # Most venues run BibTeX or Biber themselves, so a missing .bbl is a note
        # unless the user requires one (for services such as arXiv).
        consequence = (
            "Services that compile without running BibTeX or Biber, such as arXiv, would "
            "render empty citations."
            if require_bbl
            else "This only matters for services that compile without running BibTeX or "
            "Biber, such as arXiv; set submission_checks.require_bbl = true to require it."
        )
        findings.append(
            Finding(
                "source.bibliography_bbl",
                f"{main} reads {', '.join(bib[:_MAX_LISTED])} through {backend}, but {expected} "
                f"is not in the package. {consequence}{elsewhere}",
                "warning" if require_bbl else "info",
                "failed",
                path=path or main,
                line=line,
                evidence="derived",
                details=details,
            )
        )
    if check_coverage:
        findings.append(_coverage(root, inspection, expected, present))
    return findings


def _coverage(root: Path, inspection: _Inspection, expected: str, present: bool) -> Finding:
    rule = "bibliography.bbl_coverage"
    if not present:
        # TEX009 reports the missing file itself; this result only records that
        # coverage could not be measured, so it does not block on its own.
        return Finding(
            rule,
            f"Citation coverage was not checked: no {expected} is shipped (TEX009).",
            "warning",
            "inconclusive",
            path=expected,
            suggestion=f"Build locally and ship the generated {expected} beside the main "
            "file; coverage is then checked.",
            details={"blocked_by": "TEX009", "bbl": expected},
        )
    try:
        text = _read(root / expected, MAX_SOURCE_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return Finding(
            rule,
            f"{expected} is unreadable, linked, not UTF-8 or over the scan limit.",
            "error",
            "inconclusive",
            path=expected,
        )
    keys, form = _bbl_keys(text)
    graph = _graph(inspection)
    first: dict[str, dict[str, object]] = {}
    for key, path, line in graph.citations:
        first.setdefault(key, {"key": key, "path": path, "line": line})
    missing = [first[key] for key in sorted(set(first) - keys)]
    details: dict[str, object] = {
        "bbl": expected,
        "bbl_format": form,
        "bbl_entries": len(keys),
        "cited_keys": len(first),
        "missing": missing[:MAX_SCAN_MATCHES],
        "uncited_bbl_entries": sorted(keys - set(first))[:MAX_SCAN_MATCHES],
        "nocite_all": graph.nocite_all,
        "uncertainty": graph.reasons,
        "scope": "Literal citation keys in the selected source graph against generated .bbl "
        "keys; \\nocite{*} additions and rendered entry content are not checked.",
    }
    if missing:
        names = ", ".join(str(item["key"]) for item in missing[:_MAX_LISTED])
        more = f" (+{len(missing) - _MAX_LISTED} more)" if len(missing) > _MAX_LISTED else ""
        return Finding(
            rule,
            f"{expected} lacks {len(missing)} cited key(s): {names}{more}; first cited at "
            f"{_listed(missing)}.",
            "error",
            "failed",
            path=expected,
            evidence="derived",
            details=details,
        )
    if graph.reasons:
        return Finding(
            rule,
            f"{expected} covers every literal citation key, but citations remain uncertain: "
            f"{graph.reasons[0]}",
            "error",
            "inconclusive",
            path=expected,
            details=details,
        )
    return Finding(
        rule,
        f"{expected} contains all {len(first)} literal cited key(s).",
        "info",
        "passed",
        path=expected,
        details=details,
    )


def _package_positions(document: _Document) -> dict[str, tuple[int, dict[str, object]]]:
    """First unconditional literal preamble declaration of each package name."""
    definitions = _definition_spans(document.text, document.commands)
    positions: dict[str, tuple[int, dict[str, object]]] = {}
    for command in document.commands:
        if command.start >= document.body_start:
            break
        if (
            command.name not in _PACKAGE_COMMANDS
            or not command.arguments
            or command.depth
            or in_conditional(document.conditionals, command.start)
            or _within(definitions, command.start)
        ):
            continue
        for name in _literal_list(command.arguments[0].value) or []:
            positions.setdefault(name, (command.start, document.location(command.start)))
    return positions


def _hyperref_order(document: _Document) -> list[Finding]:
    positions = _package_positions(document)
    if "hyperref" not in positions or not set(_AFTER_HYPERREF) & set(positions):
        return []
    hyperref_offset, hyperref_location = positions["hyperref"]
    early = sorted(
        (offset, name, location)
        for name, (offset, location) in positions.items()
        if name in _AFTER_HYPERREF and offset < hyperref_offset
    )
    if not early:
        checked = sorted(set(_AFTER_HYPERREF) & set(positions))
        path = hyperref_location.get("path")
        return [
            Finding(
                "source.hyperref_load_order",
                f"{', '.join(checked)} declared after hyperref.",
                "info",
                "passed",
                path=path if isinstance(path, str) else None,
                details={"checked_packages": checked},
            )
        ]
    findings = []
    for _, name, location in early:
        path = location.get("path")
        line = location.get("line")
        findings.append(
            Finding(
                "source.hyperref_load_order",
                f"{name} is loaded at {_location(location)} before hyperref "
                f"({_location(hyperref_location)}); {name} {_AFTER_HYPERREF[name]}.",
                "warning",
                "failed",
                path=path if isinstance(path, str) else None,
                line=line if isinstance(line, int) else None,
                evidence="derived",
                suggestion=f"Load {name} after \\usepackage{{hyperref}}.",
                details={
                    "package": name,
                    "hyperref": hyperref_location,
                    "scope": "First unconditional literal declarations in the expanded "
                    "preamble; packages loaded inside local style files are not inspected.",
                },
            )
        )
    return findings


def _figures(inspection: _Inspection, selected: set[str]) -> list[str]:
    return sorted(
        {
            target
            for (name, _, _), (target, command) in inspection.references.items()
            if command == "includegraphics" and name in selected
        }
    )


def _eps_finding(figures: list[str], main: str) -> list[Finding]:
    eps = [name for name in figures if PurePosixPath(name).suffix.lower() in {".eps", ".ps"}]
    if not eps:
        return []
    shown = ", ".join(eps[:_MAX_LISTED]) + (
        f" (+{len(eps) - _MAX_LISTED} more)" if len(eps) > _MAX_LISTED else ""
    )
    return [
        Finding(
            "source.eps_figures",
            f"{len(eps)} EPS/PS figure(s) are included: {shown}. pdfLaTeX and LuaLaTeX convert "
            "them only through epstopdf with shell escape, which Fledge's isolated build "
            "disables; arXiv converts EPS itself, other services may not.",
            "info",
            "failed",
            path=eps[0] if len(eps) == 1 else main,
            details={"figures": eps[:MAX_SCAN_MATCHES]},
        )
    ]


def _png_space(data: bytes) -> str | None:
    if len(data) < 26 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return {0: "gray", 2: "rgb", 3: "rgb", 4: "gray", 6: "rgb"}.get(data[25])


def _jpeg_space(data: bytes) -> tuple[str | None, int | None]:
    """Colour family from the SOF component count; Adobe APP14 refines 3/4 components."""
    if not data.startswith(b"\xff\xd8"):
        return None, None
    cursor = 2
    transform = None
    while cursor + 4 <= len(data):
        if data[cursor] != 0xFF:
            return None, transform
        marker = data[cursor + 1]
        if marker == 0xFF:
            cursor += 1
            continue
        if marker in {0x01, *range(0xD0, 0xD8)}:
            cursor += 2
            continue
        length = int.from_bytes(data[cursor + 2 : cursor + 4], "big")
        payload = data[cursor + 4 : cursor + 2 + length]
        if length < 2 or len(payload) != length - 2:
            return None, transform
        if marker == 0xEE and payload.startswith(b"Adobe") and len(payload) >= 12:
            transform = payload[11]
        elif marker in {*range(0xC0, 0xD0)} - {0xC4, 0xC8, 0xCC} and len(payload) >= 6:
            components = payload[5]
            return {1: "gray", 3: "rgb", 4: "cmyk"}.get(components), transform
        elif marker in {0xD9, 0xDA}:
            return None, transform
        cursor += 2 + length
    return None, transform


def _color_finding(
    root: Path, inspection: _Inspection, figures: list[str], required: str, main: str
) -> Finding:
    violations: list[dict[str, object]] = []
    measured: list[dict[str, object]] = []
    uncertainty: list[str] = []
    if inspection.uncertainties:
        uncertainty.append("the literal figure graph has unresolved or uncertain inputs")
    for name in figures:
        cancellation_point()
        suffix = PurePosixPath(name).suffix.lower()
        if suffix == ".pdf":
            continue  # Measured by the figure_artwork input trace after a build.
        if suffix not in {".png", ".jpg", ".jpeg"}:
            uncertainty.append(f"{name}: {suffix or 'extensionless'} figure colour is not parsed")
            continue
        try:
            data = _read(root / name, _MAX_IMAGE_HEADER_BYTES, prefix=True)
        except OSError:
            uncertainty.append(f"{name}: figure is unreadable or linked")
            continue
        transform = None
        if suffix == ".png":
            space = _png_space(data)
        else:
            space, transform = _jpeg_space(data)
        if space is None:
            uncertainty.append(f"{name}: no supported colour header was found")
            continue
        record: dict[str, object] = {"path": name, "color_space": space}
        if transform is not None:
            record["adobe_transform"] = transform
        measured.append(record)
        if space not in {"gray", required}:
            violations.append(record)
    details: dict[str, object] = {
        "required": required,
        "measured": measured[:MAX_SCAN_MATCHES],
        "violations": violations[:MAX_SCAN_MATCHES],
        "uncertainty": uncertainty[:MAX_SCAN_MATCHES],
        "scope": "PNG IHDR colour type and JPEG frame component count (Adobe APP14 recorded) "
        "of literal raster figures; grayscale satisfies either policy. PDF figures are "
        "measured separately by the figure_artwork input trace; embedded ICC profiles are "
        "not interpreted.",
    }
    if violations:
        return Finding(
            "figure.color_space",
            f"{len(violations)} raster figure(s) are not {required.upper()} or grayscale: "
            f"{_listed(violations)}.",
            "error",
            "failed",
            path=str(violations[0]["path"]) if len(violations) == 1 else main,
            evidence="direct",
            details=details,
        )
    if uncertainty:
        return Finding(
            "figure.color_space",
            f"Raster figure colour spaces are incomplete: {uncertainty[0]}.",
            "error",
            "inconclusive",
            path=main,
            details=details,
        )
    return Finding(
        "figure.color_space",
        f"{len(measured)} raster figure(s) are {required.upper()} or grayscale.",
        "info",
        "passed",
        path=main,
        details=details,
    )


def _private_markers(root: Path) -> list[Finding]:
    files, issues = _inventory(root)
    matches: list[dict[str, object]] = []
    total = 0
    for name, path in files.items():
        cancellation_point()
        if path.suffix.lower() not in _MANUSCRIPT_SUFFIXES:
            continue
        try:
            raw = _read(path, min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - total)))
            total += len(raw)
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            issues.append(f"Unreadable, non-UTF-8 or over-budget source: {_redact(name)}")
            continue
        for line, comment in _comments(text):
            reason = next(
                (label for label, pattern in _PRIVATE_MARKERS if pattern.search(comment)), None
            )
            initials = _INITIALS_NOTE.match(comment)
            if reason is None and initials and initials.group(1) not in _INITIALS_EXCLUDED:
                reason = "author-initials note"
            if reason is not None:
                matches.append({"path": name, "line": line, "reason": reason})
            if len(matches) >= MAX_SCAN_MATCHES:
                break
        if len(matches) >= MAX_SCAN_MATCHES:
            break
    details: dict[str, object] = {
        "matches": matches,
        "issues": issues[:MAX_SCAN_MATCHES],
        "scope": "TeX comments in supplied .tex/.ltx/.latex files, outside common literal "
        "regions; only locations and marker categories are reported.",
    }
    if matches:
        first = matches[0]
        first_line = first["line"]
        return [
            Finding(
                "submission.private_comment_markers",
                f"{len(matches)}{'+' if len(matches) >= MAX_SCAN_MATCHES else ''} comment(s) "
                f"carry private-note markers: "
                + ", ".join(
                    f"{_location(item)} ({item['reason']})" for item in matches[:_MAX_LISTED]
                )
                + (f" (+{len(matches) - _MAX_LISTED} more)" if len(matches) > _MAX_LISTED else "")
                + ".",
                "warning",
                "failed",
                path=str(first["path"]) if len({item["path"] for item in matches}) == 1 else None,
                line=first_line if len(matches) == 1 and isinstance(first_line, int) else None,
                evidence="heuristic",
                details=details,
            )
        ]
    if issues:
        return [
            Finding(
                "submission.private_comment_markers",
                f"Comment marker scan is incomplete: {issues[0]}",
                "info",
                "inconclusive",
                details=details,
            )
        ]
    return [
        Finding(
            "submission.private_comment_markers",
            "No private-note markers in TeX comments.",
            "info",
            "passed",
            details=details,
        )
    ]


def check_submission_readiness(
    root: Path,
    main: str | None,
    *,
    check_bbl_coverage: bool = False,
    require_bbl: bool = False,
    required_color_space: str | None = None,
) -> list[Finding]:
    """Run default advisories and explicitly configured readiness policies."""
    if type(check_bbl_coverage) is not bool:
        raise PreparationError("check_bbl_coverage must be a boolean")
    if type(require_bbl) is not bool:
        raise PreparationError("require_bbl must be a boolean")
    if required_color_space is not None and required_color_space not in FIGURE_COLOR_SPACES:
        raise PreparationError("required_color_space must be 'rgb' or 'cmyk'")
    findings = _private_markers(root)
    inspection = _Inspection(root, main)
    if inspection.main is None:
        return findings
    selected = _reachable_sources(inspection)
    findings.extend(
        _bibliography_findings(root, inspection, selected, check_bbl_coverage, require_bbl)
    )
    findings.extend(_hyperref_order(_read_document(root, inspection.main, inspection)))
    figures = _figures(inspection, selected)
    findings.extend(_eps_finding(figures, inspection.main))
    if required_color_space is not None:
        findings.append(
            _color_finding(root, inspection, figures, required_color_space, inspection.main)
        )
    return findings
