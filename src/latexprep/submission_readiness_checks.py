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
from pathlib import Path, PurePosixPath

from .bbl_checks import check_bbl
from .expanded_document import (
    ExpandedDocument,
    Location,
    definition_spans,
    expand_document,
    reachable_sources,
)
from .file_scan import (
    MAX_SCAN_MATCHES,
    ByteBudget,
    inventory_files,
    list_locations,
    location_label,
    marked_comments,
    more_suffix,
    read_file_prefix,
    redact_secrets,
    tex_comments,
)
from .literal_text import PACKAGE_COMMANDS, first_argument_names
from .models import EvidenceKind, Finding, PreparationError
from .scheduler import cancellation_point
from .source_graph import SourceGraph
from .submission_checks import SubmissionOptions
from .tex_lexer import in_conditional, in_half_open_span

FIGURE_COLOR_SPACES = ("rgb", "cmyk")
_MAX_LISTED = 3
_MAX_IMAGE_HEADER_BYTES = 4 * 1024 * 1024
_MANUSCRIPT_SUFFIXES = {".tex", ".ltx", ".latex"}
_DEFAULT_OPTIONS = SubmissionOptions()

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


def _private_marker(comment: str) -> str | None:
    """The private-note category of one comment, or ``None`` for ordinary comments."""
    reason = next((label for label, pattern in _PRIVATE_MARKERS if pattern.search(comment)), None)
    initials = _INITIALS_NOTE.match(comment)
    if reason is None and initials and initials.group(1) not in _INITIALS_EXCLUDED:
        reason = "author-initials note"
    return reason


def _label(location: Location) -> str:
    return location_label(location.as_dict())


def _package_positions(document: ExpandedDocument) -> dict[str, tuple[int, Location]]:
    """First unconditional literal preamble declaration of each package name."""
    definitions = definition_spans(document.text, document.commands)
    positions: dict[str, tuple[int, Location]] = {}
    for command in document.commands:
        if command.start >= document.body_start:
            break
        if (
            command.name not in PACKAGE_COMMANDS
            or not command.arguments
            or command.depth
            or in_conditional(document.conditionals, command.start)
            or in_half_open_span(definitions, command.start)
        ):
            continue
        for name in first_argument_names(command) or []:
            positions.setdefault(name, (command.start, document.location(command.start)))
    return positions


def _hyperref_order(document: ExpandedDocument) -> list[Finding]:
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
        return [
            Finding(
                "source.hyperref_load_order",
                f"{', '.join(checked)} declared after hyperref.",
                severity="info",
                status="passed",
                path=hyperref_location.path,
                details={"checked_packages": checked},
            )
        ]
    findings = []
    for _, name, location in early:
        findings.append(
            Finding(
                "source.hyperref_load_order",
                f"{name} is loaded at {_label(location)} before hyperref "
                f"({_label(hyperref_location)}); {name} {_AFTER_HYPERREF[name]}.",
                severity="warning",
                status="failed",
                path=location.path,
                line=location.line,
                evidence_kind=EvidenceKind.DERIVED,
                suggestion=f"Load {name} after \\usepackage{{hyperref}}.",
                details={
                    "package": name,
                    "hyperref": hyperref_location.as_dict(),
                    "scope": "First unconditional literal declarations in the expanded "
                    "preamble; packages loaded inside local style files are not inspected.",
                },
            )
        )
    return findings


def _figures(graph: SourceGraph, selected: set[str]) -> list[str]:
    return sorted(
        {
            target
            for (name, _, _), (target, command) in graph.references.items()
            if command == "includegraphics" and name in selected
        }
    )


def _eps_finding(figures: list[str], main: str) -> list[Finding]:
    eps = [name for name in figures if PurePosixPath(name).suffix.lower() in {".eps", ".ps"}]
    if not eps:
        return []
    shown = ", ".join(eps[:_MAX_LISTED]) + more_suffix(len(eps) - _MAX_LISTED)
    return [
        Finding(
            "source.eps_figures",
            f"{len(eps)} EPS/PS figure(s) are included: {shown}. pdfLaTeX and LuaLaTeX convert "
            "them only through epstopdf with shell escape, which Fledge's isolated build "
            "disables; arXiv converts EPS itself, other services may not.",
            severity="info",
            status="failed",
            path=eps[0] if len(eps) == 1 else main,
            details={"figures": eps[:MAX_SCAN_MATCHES]},
        )
    ]


def _png_space(data: bytes) -> str | None:
    if len(data) < 26 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return {0: "gray", 2: "rgb", 3: "rgb", 4: "gray", 6: "rgb"}.get(data[25])


def _jpeg_space(data: bytes) -> tuple[str | None, int | None]:
    """Colour family from the SOF component count; Adobe APP14 refines 3/4 components.

    This walker reads a bounded file prefix and stops quietly at the first segment
    it cannot follow. The image-metadata scan in ``metadata_privacy`` walks whole
    files strictly and names each structural defect, so the two stay separate.
    """
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
    root: Path, graph: SourceGraph, figures: list[str], required: str, main: str
) -> Finding:
    violations: list[dict[str, object]] = []
    measured: list[dict[str, object]] = []
    uncertainty: list[str] = []
    if graph.uncertainties:
        uncertainty.append("the literal figure graph has unresolved or uncertain inputs")
    for name in figures:
        cancellation_point()
        suffix = PurePosixPath(name).suffix.lower()
        if suffix == ".pdf":
            continue  # Measured by the included_figure_artwork input trace after a build.
        if suffix not in {".png", ".jpg", ".jpeg"}:
            uncertainty.append(f"{name}: {suffix or 'extensionless'} figure colour is not parsed")
            continue
        try:
            data = read_file_prefix(root / name, _MAX_IMAGE_HEADER_BYTES)
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
        "measured separately by the included_figure_artwork input trace; embedded ICC "
        "profiles are not interpreted.",
    }
    if violations:
        return Finding(
            "figure.color_space",
            f"{len(violations)} raster figure(s) are not {required.upper()} or grayscale: "
            f"{list_locations(violations, _MAX_LISTED)}.",
            severity="error",
            status="failed",
            path=str(violations[0]["path"]) if len(violations) == 1 else main,
            evidence_kind=EvidenceKind.DIRECT,
            details=details,
        )
    if uncertainty:
        return Finding(
            "figure.color_space",
            f"Raster figure colour spaces are incomplete: {uncertainty[0]}.",
            severity="error",
            status="inconclusive",
            path=main,
            details=details,
        )
    return Finding(
        "figure.color_space",
        f"{len(measured)} raster figure(s) are {required.upper()} or grayscale.",
        severity="info",
        status="passed",
        path=main,
        details=details,
    )


def _private_markers(root: Path) -> list[Finding]:
    files, issues = inventory_files(root)
    matches: list[dict[str, object]] = []
    budget = ByteBudget()
    for name, path in files.items():
        cancellation_point()
        if path.suffix.lower() not in _MANUSCRIPT_SUFFIXES:
            continue
        try:
            text = budget.read(path).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            issues.append(f"Unreadable, non-UTF-8 or over-budget source: {redact_secrets(name)}")
            continue
        markers = marked_comments(
            tex_comments(text), _private_marker, MAX_SCAN_MATCHES - len(matches)
        )
        matches.extend({"path": name, "line": line, "reason": reason} for line, reason in markers)
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
        listed = ", ".join(
            f"{location_label(item)} ({item['reason']})" for item in matches[:_MAX_LISTED]
        )
        return [
            Finding(
                "submission.private_comment_markers",
                f"{len(matches)}{'+' if len(matches) >= MAX_SCAN_MATCHES else ''} comment(s) "
                f"carry private-note markers: {listed}"
                f"{more_suffix(len(matches) - _MAX_LISTED)}.",
                severity="warning",
                status="failed",
                path=str(first["path"]) if len({item["path"] for item in matches}) == 1 else None,
                line=first_line if len(matches) == 1 and isinstance(first_line, int) else None,
                evidence_kind=EvidenceKind.HEURISTIC,
                details=details,
            )
        ]
    if issues:
        return [
            Finding(
                "submission.private_comment_markers",
                f"Comment marker scan is incomplete: {issues[0]}",
                severity="info",
                status="inconclusive",
                details=details,
            )
        ]
    return [
        Finding(
            "submission.private_comment_markers",
            "No private-note markers in TeX comments.",
            severity="info",
            status="passed",
            details=details,
        )
    ]


def check_submission_readiness(
    root: Path,
    main: str | None,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
    *,
    required_color_space: str | None = None,
) -> list[Finding]:
    """Run default advisories and the readiness policies configured in ``options``.

    ``required_color_space`` comes from the figure artwork settings.
    """
    if required_color_space is not None and required_color_space not in FIGURE_COLOR_SPACES:
        raise PreparationError("required_color_space must be 'rgb' or 'cmyk'")
    findings = _private_markers(root)
    graph = SourceGraph.build(root, main)
    if graph.main is None:
        return findings
    selected = reachable_sources(graph)
    findings.extend(check_bbl(root, graph, selected, options))
    findings.extend(_hyperref_order(expand_document(root, graph.main, graph)))
    figures = _figures(graph, selected)
    findings.extend(_eps_finding(figures, graph.main))
    if required_color_space is not None:
        findings.append(_color_finding(root, graph, figures, required_color_space, graph.main))
    return findings
