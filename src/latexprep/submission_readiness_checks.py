"""Cheap, deterministic submission-readiness checks over the literal source graph.

The default-on observations here (missing generated bibliography, index or glossary
files, hyperref load order and private-note comment markers) are advisories: they never
encode a publisher's acceptance rules and never block preparation. A missing generated
file is only a note unless the settings say that the receiving service does not run the
tool that writes it. EPS figures are the exception that blocks: the isolated build
cannot convert them, and an old TeX Live drops them without an error. Policies that need
a user value (shipped bibliography coverage, raster figure colour space, the engines a
service offers) run only when explicitly configured; their missing evidence is
inconclusive, not a pass. No source is modified.

This module owns the private-comment scan, the hyperref load order, the EPS check and
the engine note, and runs the others: the shipped-bibliography checks (``bbl_checks``),
the generated index and glossary files (``generated_file_checks``) and the figure
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
from .finding_weight import Weight
from .findings import EvidenceKind, Finding
from .generated_file_checks import check_generated_lists
from .literal_text import PACKAGE_COMMANDS, first_argument_names, plain_text
from .redaction import redact_secrets
from .setting_choices import Engine
from .source_graph import SourceGraph
from .tex_commands import ROOT_SUFFIXES
from .tex_comments import marked_comments, tex_comments
from .tex_lexer import split_argument_items
from .tex_spans import definition_spans, in_conditional, in_half_open_span

# Matches and figures named in a message; the details list every one.
_MAX_LISTED = 3
_DEFAULT_BBL_OPTIONS = BblOptions()
# A comment scan that finds markers warns; one that could not read every file only informs.
_PRIVATE_MARKER_WEIGHT = Weight("warning", "info", EvidenceKind.HEURISTIC)
_HYPERREF_WEIGHT = Weight("warning", "warning", EvidenceKind.DERIVED)
# An EPS figure the isolated build cannot convert blocks: an old TeX Live drops it silently.
_EPS_WEIGHT = Weight("error", "error", EvidenceKind.DERIVED)
_ENGINE_NAMES: dict[Engine, str] = {
    "pdflatex": "pdfLaTeX",
    "xelatex": "XeLaTeX",
    "lualatex": "LuaLaTeX",
}

# Each package documents that it must be loaded after hyperref. Keep this list
# small: an entry is only added with a documented load-order requirement.
_AFTER_HYPERREF = {
    "cleveref": "patches hyperref's reference commands",
    "hypcap": "patches hyperref's figure anchors",
    "bookmark": "loads hyperref itself, so later hyperref options clash",
    "glossaries": "creates hyperlinks only when hyperref is already loaded",
    "glossaries-extra": "creates hyperlinks only when hyperref is already loaded",
}

# Packages that stop with an error under every engine but the ones listed, as their
# documentation states and as TeX Live 2026 shows. Keep this list to packages that refuse
# an engine outright; a package that only behaves differently does not belong here.
_ENGINE_PACKAGES: dict[str, tuple[Engine, ...]] = {
    "fontspec": ("xelatex", "lualatex"),
    "unicode-math": ("xelatex", "lualatex"),
    "polyglossia": ("xelatex", "lualatex"),
    "fontsetup": ("xelatex", "lualatex"),
    "xltxtra": ("xelatex", "lualatex"),
    "xeCJK": ("xelatex",),
    "mathspec": ("xelatex",),
    "luacode": ("lualatex",),
    "luatexja": ("lualatex",),
    "luaotfload": ("lualatex",),
}
_ENGINE_PACKAGE_WEIGHT = Weight("warning", "warning", EvidenceKind.DERIVED)

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
    r"""Return the initials, in capitals, that the literal ``\author`` names can be abbreviated to.

    A run of two or three consecutive words, in either order, and the first and last word
    of each name give one set of initials. Names that are not literal text give none.

    Args:
        document: The expanded document whose ``\author`` commands are read.

    Returns:
        The initials of every literal author name.
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
    r"""Return the private-note category of one comment, or ``None`` for ordinary comments.

    Capital letters before a colon only count as an author's note when they are the
    initials of a literal ``\author`` name; acronyms such as ``MLP:`` are ordinary.

    Args:
        comment: The text of the comment.
        author_initials: The initials of the literal author names.

    Returns:
        The marker category's value, ``"author-initials note"`` for a note headed by an
        author's initials, or ``None`` for an ordinary comment.
    """
    category = matching_category(comment, _PRIVATE_MARKER_CATEGORIES)
    if category is not None:
        return category.value
    initials = _INITIALS_NOTE.match(comment)
    return "author-initials note" if initials and initials.group(1) in author_initials else None


def _label(location: Location) -> str:
    """Return the report label of a location in the expanded document."""
    return location_label(location.as_dict())


def _package_positions(document: ExpandedDocument) -> dict[str, tuple[int, Location]]:
    """Return the first unconditional literal preamble declaration of each package name.

    Declarations at a nonzero group depth, inside a conditional or inside a definition
    are skipped, and the scan stops at the start of the document body.

    Args:
        document: The expanded document to scan.

    Returns:
        For each package name, its offset within the first declaration and the location
        of the declaration itself.
    """
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
            or first_argument_names(command) is None
        ):
            continue
        location = document.location(command.start)
        for item in split_argument_items(command.arguments[0]):
            positions.setdefault(item.value, (item.start, location))
    return positions


def _hyperref_order(document: ExpandedDocument) -> list[Finding]:
    """Report packages declared before hyperref that must be loaded after it.

    Args:
        document: The expanded document whose preamble is scanned.

    Returns:
        One failed finding per package declared too early, one passed finding when every
        such package follows hyperref, or no finding when hyperref or all of those packages
        are absent.
    """
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
    r"""Return the targets of the ``\includegraphics`` references that selected sources make.

    Args:
        graph: The literal source graph.
        selected: The names of the sources whose references count.

    Returns:
        The figure names, sorted and without repeats.
    """
    return sorted(
        {
            target
            for (name, _, _), (target, command) in graph.references.items()
            if command == "includegraphics" and name in selected
        }
    )


def converted_figure_name(figure: str) -> str:
    """Return the name epstopdf gives the PDF it converts ``figure`` to, beside the figure.

    epstopdf's default suffix is ``-<extension>-converted-to``, so ``figs/plot.eps``
    becomes ``figs/plot-eps-converted-to.pdf``.

    Args:
        figure: The EPS or PS figure, relative to the project.

    Returns:
        The converted figure, relative to the project.
    """
    path = PurePosixPath(figure)
    return path.with_name(f"{path.stem}-{path.suffix[1:]}-converted-to.pdf").as_posix()


def _eps_finding(figures: list[str], shipped: set[str], main: str) -> list[Finding]:
    """Report the EPS and PS figures among the included figures; block those not converted.

    No engine converts EPS in the isolated build: pdfLaTeX and LuaLaTeX need shell escape
    for epstopdf and XeLaTeX needs Ghostscript, and both are disabled. TeX Live 2023 and
    earlier then print the file name in place of the figure without an error, so the
    build alone cannot be relied on to fail. A figure whose epstopdf output is shipped
    builds with TeX Live 2024 and later, which use that file, and only warns.

    Args:
        figures: The names of the included figures.
        shipped: The project's files, which may hold the converted PDFs.
        main: The main file, the finding's path when several figures are listed.

    Returns:
        A blocking finding naming the figures without a converted PDF, otherwise a
        warning for figures included through their converted PDFs, or no finding when
        there are no EPS or PS figures.
    """
    eps = [name for name in figures if PurePosixPath(name).suffix.lower() in {".eps", ".ps"}]
    if not eps:
        return []
    unconverted = [name for name in eps if converted_figure_name(name) not in shipped]
    listed = unconverted or eps
    shown = ", ".join(listed[:_MAX_LISTED]) + more_suffix(len(listed) - _MAX_LISTED)
    if unconverted:
        message = (
            f"EPS/PS figures cannot be converted in the isolated build ({len(unconverted)}): "
            f"{shown}. pdfLaTeX and LuaLaTeX convert them only through epstopdf, which needs "
            "shell escape, and XeLaTeX only through Ghostscript; both are disabled. TeX Live "
            "2023 and earlier then print the file name in place of the figure without an error."
        )
    else:
        message = (
            f"EPS/PS figures are included through their converted PDFs ({len(eps)}): {shown}. "
            "TeX Live 2024 and later use a shipped <name>-eps-converted-to.pdf; earlier releases "
            "and XeLaTeX ignore it without shell escape and lose the figure."
        )
    return [
        Finding(
            "source.eps_figures",
            message,
            severity=_EPS_WEIGHT.severity("failed") if unconverted else "warning",
            status="failed",
            path=listed[0] if len(listed) == 1 else main,
            evidence_kind=_EPS_WEIGHT.evidence_kind,
            suggestion="Convert each figure to PDF (for example with epstopdf), include the PDF "
            "(\\includegraphics{figs/plot} finds figs/plot.pdf) and ship it with the sources.",
            details={
                "figures": eps[:MAX_SCAN_MATCHES],
                "unconverted": unconverted[:MAX_SCAN_MATCHES],
                "converted": [converted_figure_name(name) for name in eps[:MAX_SCAN_MATCHES]]
                if not unconverted
                else [],
            },
        )
    ]


def _private_marker_matches(
    root: Path, author_initials: frozenset[str]
) -> tuple[list[dict[str, object]], list[str]]:
    """Find comments with private-note markers in the shipped sources, and why the scan is partial.

    Only files with a TeX root suffix are read, within one byte budget, and the scan stops
    after ``MAX_SCAN_MATCHES`` matches.

    Args:
        root: The project root whose files are scanned.
        author_initials: The initials of the literal author names.

    Returns:
        The matches, each with its path, line and marker category, and the issues that
        made the scan incomplete.
    """
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
    """Report the private-note markers in the TeX comments of the shipped sources.

    Args:
        root: The project root whose files are scanned.
        author_initials: The initials of the literal author names.

    Returns:
        A failed finding listing the markers, an inconclusive one when there are none but
        the scan is incomplete, or a passed one.
    """
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


def _venue_engine(engine: Engine | None, offered: tuple[Engine, ...], main: str) -> list[Finding]:
    """Report whether the receiving service offers the engine the document builds with.

    Args:
        engine: The engine the document builds with, or ``None`` when not known.
        offered: The engines the service compiles with; empty when not configured.
        main: The main file, which the finding names.

    Returns:
        A warning when the engine is not offered, a pass when it is, or nothing when the
        service's engines or the document's engine are not known.
    """
    if not offered or engine is None:
        return []
    names = ", ".join(_ENGINE_NAMES[item] for item in offered)
    details: dict[str, object] = {"engine": engine, "venue_engines": list(offered)}
    if engine in offered:
        return [
            Finding(
                "source.venue_engine",
                f"The document builds with {_ENGINE_NAMES[engine]}, which the receiving "
                "service offers.",
                severity="info",
                status="passed",
                path=main,
                details=details,
            )
        ]
    return [
        Finding(
            "source.venue_engine",
            f"The document builds with {_ENGINE_NAMES[engine]}, but the receiving service "
            f"compiles only with {names} (submission_checks.venue_engines).",
            severity="warning",
            status="failed",
            path=main,
            evidence_kind=EvidenceKind.DERIVED,
            suggestion=f"Build with an engine the service offers ({names}), or submit where "
            f"{_ENGINE_NAMES[engine]} is offered.",
            details=details,
        )
    ]


def _engine_packages(document: ExpandedDocument, engine: Engine | None, main: str) -> list[Finding]:
    """Report packages the document loads that the engine it builds with cannot run.

    Args:
        document: The expanded document whose preamble is scanned.
        engine: The engine the document builds with, or ``None`` when not known.
        main: The main file, which the passed finding names.

    Returns:
        One warning per package the engine cannot run, a pass when it runs all of them, or
        nothing when the engine is not known or no such package is declared.
    """
    if engine is None:
        return []
    positions = _package_positions(document)
    declared = sorted(name for name in positions if name in _ENGINE_PACKAGES)
    if not declared:
        return []
    scope = (
        "First unconditional literal declarations in the expanded preamble; packages "
        "loaded inside local style files are not inspected."
    )
    refused = [name for name in declared if engine not in _ENGINE_PACKAGES[name]]
    if not refused:
        return [
            Finding(
                "source.engine_packages",
                f"{', '.join(declared)} run with {_ENGINE_NAMES[engine]}, the engine the "
                "document builds with.",
                severity=_ENGINE_PACKAGE_WEIGHT.severity("passed"),
                status="passed",
                path=main,
                details={"engine": engine, "packages": declared, "scope": scope},
            )
        ]
    # The engines that run every declared package, in the order of _ENGINE_NAMES.
    common = [
        item for item in _ENGINE_NAMES if all(item in _ENGINE_PACKAGES[name] for name in declared)
    ]
    if common:
        choices = " or ".join(_ENGINE_NAMES[item] for item in common)
        program = common[0]
        suggestion = (
            f"Build with {choices}: add `% !TEX program = {program}` as the first line of "
            f"{main}, which Fledge and most editors follow, pass --engine {program}, or set "
            f'engine = "{program}" in fledge.toml.'
        )
    else:
        suggestion = (
            "No engine runs all of " + ", ".join(declared) + "; replace the packages that "
            "need another engine, then choose the engine the rest need."
        )
    findings = []
    for name in refused:
        location = positions[name][1]
        needs = " or ".join(_ENGINE_NAMES[item] for item in _ENGINE_PACKAGES[name])
        findings.append(
            Finding(
                "source.engine_packages",
                f"{name} needs {needs}, but the document builds with "
                f"{_ENGINE_NAMES[engine]}; the build will stop with an error.",
                severity=_ENGINE_PACKAGE_WEIGHT.severity("failed"),
                status="failed",
                path=location.path,
                line=location.line,
                evidence_kind=EvidenceKind.DERIVED,
                suggestion=suggestion,
                details={
                    "package": name,
                    "engine": engine,
                    "engines": list(_ENGINE_PACKAGES[name]),
                    "scope": scope,
                },
            )
        )
    return findings


def check_submission_readiness(
    root: Path,
    main: str | None,
    bbl_options: BblOptions = _DEFAULT_BBL_OPTIONS,
    *,
    required_color_space: str | None = None,
    engine: Engine | None = None,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Run default advisories and the readiness policies configured in ``bbl_options``.

    Only the private-comment scan runs when the project has no main file.

    Args:
        root: The project root to check.
        main: The main file as the caller named it; ``None`` selects the single document root.
        bbl_options: The shipped-bibliography policies and what the receiving service runs
            and offers.
        required_color_space: The colour space raster figures must use, from the figure
            artwork settings, or ``None`` for no colour-space policy.
        engine: The engine the document builds with, compared with the engines the
            receiving service offers, or ``None`` when not known.
        analysed: The source analysis of the same root and main file, shared with other
            checks, or ``None`` to analyse here.

    Returns:
        The findings of the advisories and the configured policies.

    Raises:
        PreparationError: ``required_color_space`` is neither ``'rgb'`` nor ``'cmyk'``.
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
    findings.extend(
        check_generated_lists(
            graph, selected, venue_runs_index_tools=bbl_options.venue_runs_index_tools
        )
    )
    findings.extend(_hyperref_order(document))
    figures = _figures(graph, selected)
    findings.extend(_eps_finding(figures, set(graph.files), graph.main))
    findings.extend(_engine_packages(document, engine, graph.main))
    findings.extend(_venue_engine(engine, bbl_options.venue_engines, graph.main))
    if required_color_space is not None:
        findings.append(color_space_finding(root, graph, figures, required_color_space, graph.main))
    return findings
