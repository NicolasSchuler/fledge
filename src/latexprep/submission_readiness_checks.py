"""Cheap, deterministic submission-readiness advisories over the literal source graph.

The default-on observations here (missing generated bibliography, hyperref load
order, EPS figures and private-note comment markers) are advisories: they never
encode a publisher's acceptance rules and never block preparation. A missing
generated bibliography is only a note unless the user requires one. Policies that
need a user value (shipped bibliography coverage, raster figure colour space) run
only when explicitly configured; their missing evidence is inconclusive, not a
pass. No source is modified.

This module owns the private-comment scan, the hyperref load order and the EPS note,
and runs the others: the shipped-bibliography checks (``bbl_checks``) and the figure
colour-space policy (``figure_color_space``).
"""

from __future__ import annotations

import re
import unicodedata
from functools import partial
from pathlib import Path, PurePosixPath

from .analysed_source import AnalysedSource, reuse_or_analyse
from .bbl_checks import BblOptions, check_bbl
from .cancellation import cancellation_point
from .comment_markers import MarkerCategory, matching_category
from .errors import PreparationError
from .expanded_document import ExpandedDocument, Location, reachable_sources
from .figure_color_space import FIGURE_COLOR_SPACES, color_space_finding
from .file_scan import MAX_SCAN_MATCHES, ByteBudget, inventory_files
from .finding_text import location_label, more_suffix
from .finding_weight import NOTE, Weight
from .findings import EvidenceKind, Finding
from .literal_text import PACKAGE_COMMANDS, first_argument_names, plain_text
from .redaction import redact_secrets
from .source_graph import SourceGraph
from .tex_commands import ROOT_SUFFIXES
from .tex_comments import marked_comments, tex_comments
from .tex_lexer import definition_spans, in_conditional, in_half_open_span

# Matches and figures named in a message; the details list every one.
_MAX_LISTED = 3
_DEFAULT_BBL_OPTIONS = BblOptions()
# A comment scan that finds markers warns; one that could not read every file only informs.
_PRIVATE_MARKER_WEIGHT = Weight("warning", "info", EvidenceKind.HEURISTIC)
_HYPERREF_WEIGHT = Weight("warning", "warning", EvidenceKind.DERIVED)

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
_PRIVATE_MARKER_CATEGORIES = (
    MarkerCategory.REVIEW_CORRESPONDENCE,
    MarkerCategory.DISTRIBUTION_RESTRICTION,
    MarkerCategory.PRIVATE_NOTE,
)
_INITIALS_NOTE = re.compile(r"^[\s%]*\[?([A-Z]{2,3})\]?\s*:\s+\S")
# A name written out in an \author argument is split into at most this many words when
# deriving initials, and a run of two or three words yields one set of initials.
_MAX_NAME_WORDS = 12
_INITIALS_RUN_LENGTHS = (2, 3)
_NAME_WORD = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*")
_AUTHOR_SEPARATOR = re.compile(r"\\and\b")


def _author_initials(document: ExpandedDocument) -> frozenset[str]:
    """Initials, in capitals, that the literal ``\\author`` names can be abbreviated to.

    A run of two or three consecutive words, in either order, and the first and last word
    of each name give one set of initials. Names that are not literal text give none.
    """
    initials: set[str] = set()
    for command in document.commands:
        if command.name != "author" or not command.arguments:
            continue
        for name in _AUTHOR_SEPARATOR.split(command.arguments[0].value):
            words = _NAME_WORD.findall(plain_text(name)[0])[:_MAX_NAME_WORDS]
            letters = [unicodedata.normalize("NFKD", word)[0].upper() for word in words]
            if len(letters) >= min(_INITIALS_RUN_LENGTHS):
                initials.add(letters[0] + letters[-1])
            for size in _INITIALS_RUN_LENGTHS:
                for start in range(len(letters) - size + 1):
                    run = letters[start : start + size]
                    initials.update(("".join(run), "".join(reversed(run))))
    return frozenset(initials)


def _private_marker(comment: str, author_initials: frozenset[str] = frozenset()) -> str | None:
    """The private-note category of one comment, or ``None`` for ordinary comments.

    Capital letters before a colon only count as an author's note when they are the
    initials of a literal ``\\author`` name; acronyms such as ``MLP:`` are ordinary.
    """
    category = matching_category(comment, _PRIVATE_MARKER_CATEGORIES)
    if category is not None:
        return category.value
    initials = _INITIALS_NOTE.match(comment)
    return "author-initials note" if initials and initials.group(1) in author_initials else None


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
                severity=_HYPERREF_WEIGHT.severity("passed"),
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
                severity=_HYPERREF_WEIGHT.severity("failed"),
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
            f"EPS/PS figures are included ({len(eps)}): {shown}. pdfLaTeX and LuaLaTeX convert "
            "them only through epstopdf with shell escape, which Fledge's isolated build "
            "disables; arXiv converts EPS itself, other services may not.",
            severity=NOTE.severity("failed"),
            status="failed",
            path=eps[0] if len(eps) == 1 else main,
            details={"figures": eps[:MAX_SCAN_MATCHES]},
        )
    ]


def _private_marker_matches(
    root: Path, author_initials: frozenset[str]
) -> tuple[list[dict[str, object]], list[str]]:
    """Comments with private-note markers in the shipped sources, and why the scan is partial."""
    files, issues = inventory_files(root)
    matches: list[dict[str, object]] = []
    budget = ByteBudget()
    for name, path in files.items():
        cancellation_point()
        if path.suffix.lower() not in ROOT_SUFFIXES:
            continue
        try:
            text = budget.read(path).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            issues.append(f"Unreadable, non-UTF-8 or over-budget source: {redact_secrets(name)}")
            continue
        markers = marked_comments(
            tex_comments(text),
            partial(_private_marker, author_initials=author_initials),
            MAX_SCAN_MATCHES - len(matches),
        )
        matches.extend({"path": name, "line": line, "reason": reason} for line, reason in markers)
        if len(matches) >= MAX_SCAN_MATCHES:
            break
    return matches, issues


def _private_markers(root: Path, author_initials: frozenset[str]) -> Finding:
    matches, issues = _private_marker_matches(root, author_initials)
    details: dict[str, object] = {
        "matches": matches,
        "issues": issues[:MAX_SCAN_MATCHES],
        "scope": "TeX comments in supplied .tex/.ltx/.latex files, outside common literal "
        "regions; only locations and marker categories are reported.",
    }
    rule = "submission.private_comment_markers"
    if matches:
        first = matches[0]
        first_line = first["line"]
        listed = ", ".join(
            f"{location_label(item)} ({item['reason']})" for item in matches[:_MAX_LISTED]
        )
        return Finding(
            rule,
            "Comments with private-note markers "
            f"({len(matches)}{'+' if len(matches) >= MAX_SCAN_MATCHES else ''}): {listed}"
            f"{more_suffix(len(matches) - _MAX_LISTED)}.",
            severity=_PRIVATE_MARKER_WEIGHT.severity("failed"),
            status="failed",
            path=str(first["path"]) if len({item["path"] for item in matches}) == 1 else None,
            line=first_line if len(matches) == 1 and isinstance(first_line, int) else None,
            evidence_kind=_PRIVATE_MARKER_WEIGHT.evidence_kind,
            details=details,
        )
    if issues:
        return Finding(
            rule,
            f"Comment marker scan is incomplete: {issues[0]}",
            severity=_PRIVATE_MARKER_WEIGHT.severity("inconclusive"),
            status="inconclusive",
            details=details,
        )
    return Finding(
        rule,
        "No private-note markers in TeX comments.",
        severity=_PRIVATE_MARKER_WEIGHT.severity("passed"),
        status="passed",
        details=details,
    )


def check_submission_readiness(
    root: Path,
    main: str | None,
    bbl_options: BblOptions = _DEFAULT_BBL_OPTIONS,
    *,
    required_color_space: str | None = None,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Run default advisories and the readiness policies configured in ``bbl_options``.

    ``required_color_space`` comes from the figure artwork settings. ``analysed`` shares
    the source analysis of the same root and main file with other checks.
    """
    if required_color_space is not None and required_color_space not in FIGURE_COLOR_SPACES:
        raise PreparationError("required_color_space must be 'rgb' or 'cmyk'")
    source = reuse_or_analyse(root, main, analysed)
    graph = source.graph
    document = source.expanded if graph.main is not None else None
    # Without literal author names, initials cannot be told from acronyms and are not reported.
    initials = _author_initials(document) if document else frozenset()
    findings = [_private_markers(root, initials)]
    if graph.main is None or document is None:
        return findings
    selected = reachable_sources(graph)
    findings.extend(check_bbl(root, graph, selected, bbl_options))
    findings.extend(_hyperref_order(document))
    figures = _figures(graph, selected)
    findings.extend(_eps_finding(figures, graph.main))
    if required_color_space is not None:
        findings.append(color_space_finding(root, graph, figures, required_color_space, graph.main))
    return findings
