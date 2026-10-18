"""Opt-in, publisher-neutral checks of literal manuscript structures.

This is a bounded lexical interpretation, not TeX execution. Source order is
explicitly distinguished from rendered order and heuristic advice from policy
violations. No source is modified and no manuscript content is generated.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, get_args

from .expanded_document import (
    MACRO_DEFINITION_COMMANDS,
    EnvironmentRegion,
    ExpandedDocument,
    Location,
    command_end,
    commands_before_end,
    definition_spans,
    environment_regions,
    expand_document,
)
from .file_scan import check_status, format_locations, more_suffix, plural
from .literal_text import (
    HEADING_DEPTHS,
    KNOWN_TEXT_COMMANDS,
    MATH_ENVIRONMENT_PATTERN,
    PACKAGE_COMMANDS,
    REFERENCE_COMMANDS,
    TEXT_WRAPPERS,
    Declaration,
    first_argument_names,
    is_placeholder_text,
    literal_list,
    normalize_heading,
    plain_argument_text,
    plain_text,
)
from .models import EvidenceKind, Finding, PreparationError, Severity
from .option_config import require_booleans
from .scheduler import cancellation_point
from .tex_lexer import (
    Command,
    conditional_spans,
    in_closed_span,
    in_conditional,
    in_half_open_span,
    merge_spans,
    split_argument_items,
)

AbbreviationPolicy = Literal["forbid", "define"]
_ABBREVIATION_POLICIES: tuple[AbbreviationPolicy, ...] = get_args(AbbreviationPolicy)


@dataclass(frozen=True)
class ManuscriptCheckOptions:
    allowed_packages: tuple[str, ...] | None = None
    inventory_packages: bool = False
    check_layout_manipulation: bool = False
    forbid_abstract_citations: bool = False
    abstract_abbreviation_policy: AbbreviationPolicy | None = None
    abstract_abbreviation_exceptions: tuple[str, ...] = ()
    max_heading_depth: int | None = None
    check_empty_sections: bool = False
    check_hardcoded_references: bool = False
    required_metadata: tuple[str, ...] = ()
    check_orcid: bool = False
    required_declarations: tuple[str, ...] = ()
    require_float_captions: bool = False
    require_float_labels: bool = False
    check_float_label_order: bool = False
    require_float_references: bool = False
    check_float_reference_order: bool = False
    require_figure_descriptions: bool = False
    require_line_numbers: bool = False
    forbid_line_numbers: bool = False

    def __post_init__(self) -> None:
        require_booleans(
            self,
            "inventory_packages",
            "check_layout_manipulation",
            "forbid_abstract_citations",
            "check_empty_sections",
            "check_hardcoded_references",
            "check_orcid",
            "require_float_captions",
            "require_float_labels",
            "check_float_label_order",
            "require_float_references",
            "check_float_reference_order",
            "require_figure_descriptions",
            "require_line_numbers",
            "forbid_line_numbers",
        )
        if self.require_line_numbers and self.forbid_line_numbers:
            raise PreparationError("Choose require_line_numbers or forbid_line_numbers, not both")
        for name in (
            "allowed_packages",
            "abstract_abbreviation_exceptions",
            "required_metadata",
            "required_declarations",
        ):
            values = getattr(self, name)
            if values is None and name == "allowed_packages":
                continue
            if (
                not isinstance(values, tuple)
                or any(
                    not isinstance(value, str) or not value or value != value.strip()
                    for value in values
                )
                or len(set(values)) != len(values)
            ):
                raise PreparationError(f"{name} must contain unique nonempty strings")
        if self.allowed_packages is not None and any(
            literal_list(value) != [value] for value in self.allowed_packages
        ):
            raise PreparationError("allowed_packages must contain single literal package names")
        # A tuple, not a set: an unhashable configured value must reach this message.
        if self.abstract_abbreviation_policy not in (None, *_ABBREVIATION_POLICIES):
            raise PreparationError("abstract_abbreviation_policy must be 'forbid' or 'define'")
        if self.max_heading_depth is not None and (
            type(self.max_heading_depth) is not int or not -1 <= self.max_heading_depth <= 5
        ):
            raise PreparationError("max_heading_depth must be an integer from -1 to 5")
        if set(self.required_metadata) - set(_METADATA):
            raise PreparationError(
                "required_metadata must name title, author, affiliation, "
                "email or corresponding_author"
            )


_METADATA = {
    "title": {"title"},
    "author": {"author"},
    "affiliation": {"affiliation"},
    "email": {"email"},
    "corresponding_author": {"correspondingauthor"},
}
_CITATIONS = {
    "cite",
    "citep",
    "citet",
    "parencite",
    "textcite",
    "autocite",
    "footcite",
    "citeauthor",
    "citeyear",
    "citeyearpar",
    "Citep",
    "Citet",
    "Cite",
    "supercite",
}
_ORCID = {"orcid", "ORCID", "orcidlink"}
_LAYOUT = {
    "newpage",
    "clearpage",
    "cleardoublepage",
    "pagebreak",
    "enlargethispage",
    "resizebox",
    "scalebox",
    "tiny",
    "scriptsize",
    "footnotesize",
    "small",
    "linespread",
    "setstretch",
    "geometry",
    "newgeometry",
    "restoregeometry",
    "fontsize",
    "baselinestretch",
}
_LENGTHS = {
    "textwidth",
    "textheight",
    "oddsidemargin",
    "evensidemargin",
    "topmargin",
    "headheight",
    "headsep",
    "footskip",
    "baselineskip",
    "columnsep",
    "parskip",
    "abovecaptionskip",
    "belowcaptionskip",
    "floatsep",
    "textfloatsep",
    "intextsep",
}
_FLOAT_ENVIRONMENTS = frozenset({"figure", "figure*", "table", "table*"})
# Every command these policies interpret; anything else in the document body is
# an unexpanded construct that may hide or generate what a policy looks for.
_INTERPRETED_COMMANDS = (
    set(HEADING_DEPTHS)
    | _CITATIONS
    | REFERENCE_COMMANDS
    | _ORCID
    | _LAYOUT
    | _LENGTHS
    | PACKAGE_COMMANDS
    | TEXT_WRAPPERS
    # One canonical non-content inventory is shared with the literal text scan so
    # the two cannot disagree about which commands are merely structural.
    | KNOWN_TEXT_COMMANDS
    | {name for names in _METADATA.values() for name in names}
    | {
        "documentclass",
        "begin",
        "end",
        "endinput",
        "input",
        "include",
        "label",
        "caption",
        "Description",
        "declaration",
        "abstract",
        "keywords",
        "thanks",
        "and",
        "sep",
        "maketitle",
        "date",
        "today",
        "includegraphics",
        "graphicspath",
        "bibliography",
        "bibliographystyle",
        "addbibresource",
        "printbibliography",
        "nocite",
        "tableofcontents",
        "listoffigures",
        "listoftables",
        "appendix",
        "centering",
        "raggedright",
        "raggedleft",
        "flushleft",
        "hfill",
        "vfill",
        "noindent",
        "par",
        "newline",
        "linebreak",
        "item",
        "hline",
        "cline",
        "toprule",
        "midrule",
        "bottomrule",
        "cmidrule",
        "multicolumn",
        "multirow",
        "rule",
        "url",
        "href",
        "footnote",
        "footnotemark",
        "footnotetext",
        "textsuperscript",
        "textsubscript",
        "textwidth",
        "linewidth",
        "columnwidth",
        "setlength",
        "addtolength",
        "hspace",
        "vspace",
        "hskip",
        "vskip",
        "kern",
        "setcounter",
        "addtocounter",
        "refstepcounter",
        "stepcounter",
        "pagestyle",
        "thispagestyle",
        "pagenumbering",
        "hypersetup",
        "selectfont",
        "normalfont",
        "normalsize",
        "TeX",
        "LaTeX",
        "textcolor",
        "color",
        "bfseries",
        "itshape",
        "sffamily",
        "ttfamily",
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathcal",
        "frac",
        "sqrt",
        "sum",
        "prod",
        "int",
        "left",
        "right",
        "leq",
        "geq",
        "times",
        "cdot",
        "ldots",
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "theta",
        "lambda",
        "mu",
        "pi",
        "sigma",
        "phi",
        "omega",
        "in",
        "to",
        "infty",
        "not",
        "nonumber",
        "quad",
        "qquad",
        "displaystyle",
        "text",
        "overline",
        "hat",
        "bar",
        "\\",
        "%",
        "&",
        "#",
        "_",
        "{",
        "}",
        ",",
        ";",
        "!",
        " ",
        "(",
        ")",
        "[",
        "]",
    }
)
_INTERPRETED_ENVIRONMENTS = {
    "document",
    "abstract",
    "keywords",
    "keyword",
    "IEEEkeywords",
    "figure",
    "figure*",
    "table",
    "table*",
    "center",
    "flushleft",
    "flushright",
    "tabular",
    "tabular*",
    "tabularx",
    "array",
    "itemize",
    "enumerate",
    "description",
    "quote",
    "quotation",
    "equation",
    "equation*",
    "align",
    "align*",
    "aligned",
    "gather",
    "gather*",
    "displaymath",
    "math",
    "split",
    "cases",
    "matrix",
    "pmatrix",
    "bmatrix",
    "minipage",
    "thebibliography",
    "verbatim",
    "Verbatim",
    "lstlisting",
    "minted",
}
_COUNTER_CHANGES = {"setcounter", "addtocounter", "refstepcounter", "stepcounter"}
# Commands blanked before judging whether a section body has content.
_BLANKED_IN_CONTENT = frozenset(
    {
        *HEADING_DEPTHS,
        "label",
        "nocite",
        "centering",
        "raggedright",
        "raggedleft",
        "noindent",
        "declaration",
    }
)
_GENERATED_CONTENT = {
    "includegraphics",
    "rule",
    "bibliography",
    "printbibliography",
    "tableofcontents",
    "listoffigures",
    "listoftables",
}
# Mathematics makes a section nonempty. The environment names are matched as
# prefixes, so starred and related environments such as aligned count as well.
_MATH_CONTENT = re.compile(rf"(?<!\\)\$|\\[([]|\\begin\{{(?:{MATH_ENVIRONMENT_PATTERN})")
# Constructs that could construct, rename or conditionally execute a package
# declaration. Macro definitions cannot: they only name a replacement body.
_PACKAGE_GENERATORS = {
    "csname",
    "expandafter",
    "noexpand",
    "scantokens",
    "catcode",
    "ExplSyntaxOn",
    "directlua",
    "luadirect",
    "luaexec",
    "@ifpackageloaded",
    "@ifclassloaded",
    "@ifpackagelater",
    "@ifclasslater",
    "@ifpackagewith",
    "@ifclasswith",
    "IfFileExists",
    "InputIfFileExists",
    "ProcessOptions",
    "ExecuteOptions",
    "PassOptionsToPackage",
    "PassOptionsToClass",
    "AtEndOfPackage",
    "AtEndOfClass",
}
# Inside a conditional, only these commands cannot generate a package declaration.
_INERT_IN_PACKAGE_CONDITIONALS = (
    _INTERPRETED_COMMANDS
    | MACRO_DEFINITION_COMMANDS
    | {"ProvidesPackage", "ProvidesClass", "ProvidesFile", "NeedsTeXFormat"}
)
_ABBREVIATION = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
# Expanded words directly before "(" that may define the abbreviation that follows.
_ABBREVIATION_DEFINITION = re.compile(
    r"""
    (                                     # the expanded words:
        (?:\b[A-Za-z][A-Za-z-]*\s+){1,10} # up to ten words, each followed by space,
        [A-Za-z][A-Za-z-]*                # then the last word
    )
    \s*\($                                # directly before an opening parenthesis
    """,
    re.VERBOSE,
)
_HARDCODED_REFERENCE = re.compile(
    r"""
    \b(?:Figures?|Figs?\.?|Tables?|Sections?|Secs?\.?|Equations?|Eqs?\.?)  # object kind
    \s*[~ ]\s*                                                          # space or tie
    \(?\d+(?:\.\d+)*\)?                                                 # number, as in (5)
    """,
    re.VERBOSE,
)
_ORCID_IDENTIFIER = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", re.ASCII)
_LINE_NUMBER_COMMANDS = {"linenumbers", "runninglinenumbers", "pagewiselinenumbers"}
_LINE_NUMBER_OPTIONS = {"lineno", "linenumbers"}
_MAX_REPORTED_CONSTRUCTS = 20
_SCOPE = (
    "Selected literal source graph; macros are not expanded and rendered output is not inferred."
)


class _Heading(TypedDict):
    path: str | None
    line: int | None
    title: str
    depth: int
    start: int
    body_start: int
    end: int
    uncertainty: list[str]


class _MetadataValue(TypedDict):
    path: str | None
    line: int | None
    value: str
    uncertainty: list[str]
    placeholder: bool


class _Weight(NamedTuple):
    """The severity and evidence kind a check reports when it does not pass."""

    severity: Severity
    evidence_kind: EvidenceKind


_POLICY = _Weight("error", EvidenceKind.DERIVED)
_ADVISORY = _Weight("warning", EvidenceKind.HEURISTIC)


class _CheckOutcome(NamedTuple):
    """What one check established: confirmed violations and the gaps that remain."""

    violations: Sequence[Mapping[str, object]]
    uncertainty: Iterable[str]


# Offending names spelled out in a message; the details list every one.
_MAX_NAMED = 3


def _listing(label: str, names: Iterable[str]) -> str:
    """``Missing: a, b, c (+2 more)``: the first few distinct names under a label."""
    unique = list(dict.fromkeys(names))
    return f"{label}: {', '.join(unique[:_MAX_NAMED])}{more_suffix(len(unique) - _MAX_NAMED)}"


def _unknown_reasons(unknown: Sequence[tuple[int, str]], start: int, end: int) -> list[str]:
    """Describe unexpanded body constructs inside one checked region only."""
    labels = sorted({label for offset, label in unknown if start <= offset < end})
    if not labels:
        return []
    return [
        "unexpanded or context-dependent constructs: "
        + ", ".join(labels[:_MAX_REPORTED_CONSTRUCTS])
    ]


@dataclass(frozen=True, slots=True)
class _Reading:
    """The expanded document and the body constructs these checks cannot interpret."""

    document: ExpandedDocument
    # Offset and label of each unexpanded body construct, in source order.
    unknown: list[tuple[int, str]]
    # Everything a document-wide scan cannot interpret.
    uncertainty: list[str]

    def unknown_reasons(self, start: int, end: int) -> list[str]:
        return _unknown_reasons(self.unknown, start, end)

    def scope(self, region: EnvironmentRegion) -> None:
        """Attach only the interpretation gaps that fall inside this region."""
        if in_conditional(self.document.conditionals, region.command.start):
            region.uncertainty.append("conditional source control flow")
        region.uncertainty.extend(self.unknown_reasons(region.start, region.end))


def _interpret(document: ExpandedDocument) -> _Reading:
    """Find body constructs outside the interpreted inventory.

    Preamble declarations configure the toolchain; they are not manuscript content,
    and a macro definition only names a replacement body.
    """
    definitions = definition_spans(document.text, document.commands)
    unknown: list[tuple[int, str]] = []
    for command in document.commands:
        cancellation_point()
        if command.start < document.body_start or in_half_open_span(definitions, command.start):
            continue
        if command.name not in _INTERPRETED_COMMANDS:
            unknown.append((command.start, "\\" + command.name))
        if command.name in {"begin", "end"} and command.arguments:
            environment = command.arguments[0].value.strip()
            if environment not in _INTERPRETED_ENVIRONMENTS:
                unknown.append((command.start, "environment " + environment))
        if command.name in _COUNTER_CHANGES:
            unknown.append((command.start, "explicit counter modification"))
    uncertainty = [
        *document.presence_uncertainty,
        *_unknown_reasons(unknown, 0, len(document.text)),
    ]
    return _Reading(document, unknown, uncertainty)


def _policy_finding(
    rule: str,
    title: str,
    outcome: _CheckOutcome,
    *,
    failure: str | None = None,
    weight: _Weight = _POLICY,
    details: Mapping[str, object] | None = None,
    other_gaps: bool = False,
    listing: str = "",
) -> Finding:
    """Report confirmed violations as failures even when other constructs are unclear.

    Callers only record a violation for a construct they could interpret, so a
    violation is never in doubt. Remaining interpretation gaps stay in the details
    and decide the outcome only when nothing was confirmed. ``failure`` summarizes
    the violations; it defaults to the title. ``listing`` names the offending items
    after the locations. Any remaining gap, or ``other_gaps`` reported in ``details``
    beyond the outcome's own, marks the evidence incomplete.
    """
    violations = outcome.violations
    reasons = sorted(set(outcome.uncertainty))
    status = check_status(failed=bool(violations), incomplete=bool(reasons))
    location = violations[0] if len(violations) == 1 else {}
    path, line = location.get("path"), location.get("line")
    match status:
        case "failed":
            where = format_locations(violations)
            message = (failure or title.rstrip(".")) + (": " + where if where else "") + "."
            if listing:
                message += " " + listing + "."
            if reasons:
                message += " Other constructs remain inconclusive: " + reasons[0].rstrip(".") + "."
        case "inconclusive":
            message = title + " Inconclusive: " + reasons[0].rstrip(".") + "."
        case _:
            message = title
    return Finding(
        rule,
        message,
        severity="info" if status == "passed" else weight.severity,
        status=status,
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) else None,
        evidence_kind=weight.evidence_kind,
        incomplete=bool(reasons) or other_gaps,
        details={
            "matches": [dict(item) for item in violations],
            "uncertainty": reasons,
            "scope": _SCOPE,
            **(details or {}),
        },
    )


def _scoped_regions(
    reading: _Reading, kinds: Iterable[str]
) -> tuple[list[EnvironmentRegion], list[str]]:
    """Environment regions with the unexpanded constructs inside each one."""
    regions, reasons = environment_regions(reading.document, frozenset(kinds))
    for region in regions:
        region.uncertainty.extend(reading.unknown_reasons(region.start, region.end))
    return regions, reasons


def _headings(document: ExpandedDocument) -> list[_Heading]:
    result: list[_Heading] = []
    pending: list[int] = []
    for command in document.commands:
        if command.name not in HEADING_DEPTHS:
            continue
        cancellation_point()
        depth = HEADING_DEPTHS[command.name]
        value, reasons = plain_argument_text(command)
        if command.depth:
            reasons.append("heading inside a group or macro argument")
        if in_conditional(document.conditionals, command.start):
            reasons.append("conditional source control flow")
        while pending and result[pending[-1]]["depth"] >= depth:
            result[pending.pop()]["end"] = command.start
        location = document.location(command.start)
        result.append(
            {
                "path": location.path,
                "line": location.line,
                "title": value,
                "depth": depth,
                "start": command.start,
                "body_start": command_end(command),
                "end": len(document.text),
                "uncertainty": reasons,
            }
        )
        pending.append(len(result) - 1)
    return result


def _content(document: ExpandedDocument, start: int, end: int) -> tuple[bool, list[str]]:
    text = document.text[start:end]
    commands = document.between(start, end)
    edits = [
        (command.start - start, min(command_end(command), end) - start)
        for command in commands
        if command.name in _BLANKED_IN_CONTENT
    ]
    for opening, closing in reversed(edits):
        text = text[:opening] + " " * (closing - opening) + text[closing:]
    plain, reasons = plain_text(text)
    if any(command.name in _GENERATED_CONTENT for command in commands):
        return True, []
    if _MATH_CONTENT.search(text):
        return True, []
    # Unknown commands prevent an empty-content claim even when their arguments
    # happen to contain text; semantic content cannot be inferred from an argument.
    return bool(plain.strip()), reasons


def _metadata(document: ExpandedDocument) -> tuple[dict[str, list[_MetadataValue]], list[str]]:
    values: dict[str, list[_MetadataValue]] = {name: [] for name in _METADATA}
    # Metadata lives in the preamble, so only the declarations' own arguments and
    # control flow that could add or remove a declaration bear on this reading.
    reasons = list(document.presence_uncertainty)
    for command in document.commands:
        for name, commands in _METADATA.items():
            if command.name not in commands:
                continue
            value, unknown = plain_argument_text(command)
            if command.depth:
                unknown.append("metadata inside a group or macro argument")
            if in_conditional(document.conditionals, command.start):
                unknown.append("conditional source control flow")
            location = document.location(command.start)
            values[name].append(
                {
                    "path": location.path,
                    "line": location.line,
                    "value": value,
                    "uncertainty": unknown,
                    "placeholder": is_placeholder_text(value),
                }
            )
    return values, reasons


def read_literal_metadata(
    root: Path, main: str | None, fields: tuple[str, ...] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Return canonical, unambiguous literal values and any interpretation gaps.

    Only title/author/affiliation/email/correspondingauthor commands are supported.
    Multiple declarations may represent overwrites or repeated authors and are
    consequently not returned as a resolved PDF metadata value. When fields are
    selected, only their values and field-specific uncertainty are returned;
    uncertainty about the source graph still applies to every selection.
    """
    metadata, reasons = _metadata(expand_document(root, main))
    values: dict[str, str] = {}
    for key, declarations in metadata.items():
        if fields is not None and key not in fields:
            continue
        if len(declarations) > 1:
            reasons.append(f"multiple {key} declarations have unresolved combination semantics")
        for declaration in declarations:
            reasons.extend(declaration["uncertainty"])
        if (
            len(declarations) == 1
            and not declarations[0]["uncertainty"]
            and not declarations[0]["placeholder"]
            and declarations[0]["value"]
        ):
            values[key] = declarations[0]["value"]
    return values, sorted(set(reasons))


def _package_declarations(document: ExpandedDocument) -> tuple[list[Declaration], list[str]]:
    """Direct package declarations in each selected source, and what could hide others."""
    declarations: list[Declaration] = []
    reasons = list(document.graph_uncertainty)
    for path in sorted(document.selected):
        source = document.graph.sources[path]
        # Only constructs that could build, rename or conditionally execute a
        # declaration matter here. An ordinary macro definition cannot, so its
        # presence must not turn the whole inventory inconclusive.
        conditionals = conditional_spans(source.commands)
        definitions = definition_spans(source.masked, source.commands)
        for command in commands_before_end(source):
            conditional = in_conditional(conditionals, command.start)
            if command.name == "endinput" and (
                command.depth
                or conditional
                or source.masked[command_end(command) :].split("\n", 1)[0].strip()
            ):
                reasons.append(f"{path}: package declarations have unresolved endinput context")
            if command.name in _PACKAGE_GENERATORS:
                reasons.append(
                    f"{path}:{command.line}: \\{command.name} can generate a package declaration"
                )
            if command.name not in PACKAGE_COMMANDS:
                if (
                    conditional
                    and not in_half_open_span(definitions, command.start)
                    and command.name not in _INERT_IN_PACKAGE_CONDITIONALS
                ):
                    reasons.append(
                        f"{path}:{command.line}: unexpanded constructs inside a conditional "
                        "can generate package declarations"
                    )
                continue
            names = first_argument_names(command)
            unknown = []
            if command.depth or conditional:
                unknown.append("grouped, macro-generated or conditional package declaration")
            if names is None:
                unknown.append("nonliteral package declaration")
            reasons.extend(unknown)
            declarations.append(
                {
                    "path": path,
                    "line": command.line,
                    "names": names,
                    "options": [argument.value for argument in command.options],
                    "uncertainty": unknown,
                }
            )
    return declarations, reasons


def _package_checks(document: ExpandedDocument, options: ManuscriptCheckOptions) -> list[Finding]:
    if options.allowed_packages is None and not options.inventory_packages:
        return []
    declarations, reasons = _package_declarations(document)
    scope = (
        "Direct literal package declarations in selected local sources, classes and styles; "
        "transitive system package loading and effective options are not established."
    )
    findings = [
        Finding(
            "manuscript.package_inventory",
            "Inventoried direct local package declarations.",
            severity="info",
            status="inconclusive" if reasons else "passed",
            evidence_kind=EvidenceKind.DIRECT,
            details={
                "declarations": declarations,
                "uncertainty": sorted(set(reasons)),
                "scope": scope,
            },
        )
    ]
    allowed = options.allowed_packages
    if allowed is not None:
        matches = []
        not_allowed: list[str] = []
        for declaration in declarations:
            names = declaration["names"]
            if names is None or declaration["uncertainty"]:
                continue
            disallowed = [name for name in names if name not in allowed]
            if disallowed:
                matches.append({**declaration, "disallowed": disallowed})
                not_allowed.extend(disallowed)
        findings.append(
            _policy_finding(
                "manuscript.package_allowlist",
                "Checked the configured direct-package allowlist.",
                _CheckOutcome(matches, reasons),
                failure=f"{len(matches)} of "
                f"{plural(len(declarations), 'direct package declaration')} not allowed",
                listing=_listing("Not allowed", not_allowed),
                details={"allowed": list(allowed), "declarations": declarations, "scope": scope},
            )
        )
    return findings


def _is_layout_override(document: ExpandedDocument, command: Command) -> bool:
    match command.name:
        case "hspace" | "vspace" | "hskip" | "vskip" | "kern":
            value = (
                command.arguments[0].value
                if command.arguments
                else document.text[command.name_end :][:80]
            )
            return bool(re.match(r"\s*-", value))
        case "setlength" | "addtolength" if command.arguments:
            return command.arguments[0].value.strip().lstrip("\\") in _LENGTHS
        case name if name in _LENGTHS:
            following = document.text[command_end(command) :][:80]
            return bool(re.match(r"\s*(?:=\s*)?[-+]?\d", following))
        case "begin" if command.arguments:
            return command.arguments[0].value in _FLOAT_ENVIRONMENTS and any(
                "H" in argument.value or "!" in argument.value for argument in command.options
            )
        case name:
            return name in _LAYOUT


def _layout(document: ExpandedDocument) -> list[dict[str, object]]:
    # A command inside a conditional may never execute, so it is reported as an
    # interpretation gap rather than as a confirmed override.
    return [
        document.located(
            command.start,
            command=command.name,
            source=document.text[command.start : min(command_end(command), command.start + 160)],
        )
        for command in document.commands
        if _is_layout_override(document, command)
        and not in_conditional(document.conditionals, command.start)
    ]


def _abstract_regions(reading: _Reading) -> tuple[list[EnvironmentRegion], list[str]]:
    """Abstract environments and \\abstract{...} declarations, each with its own gaps."""
    regions, reasons = _scoped_regions(reading, {"abstract"})
    for command in reading.document.commands:
        if command.name != "abstract":
            continue
        start = command.arguments[0].start if command.arguments else command_end(command)
        end = command.arguments[0].end if command.arguments else command_end(command)
        unknown = (
            [] if command.arguments and not command.depth else ["ambiguous abstract declaration"]
        )
        region = EnvironmentRegion("abstract", command, start, end, uncertainty=unknown)
        reading.scope(region)
        regions.append(region)
    return regions, reasons


def _abbreviation_candidates(value: str, options: ManuscriptCheckOptions) -> list[tuple[str, bool]]:
    """First occurrences of uppercase tokens that the policy reports, with definition status."""
    candidates = []
    seen: set[str] = set(options.abstract_abbreviation_exceptions)
    for match in _ABBREVIATION.finditer(value):
        token = match.group()
        if token in seen:
            continue
        seen.add(token)
        # This is a review candidate, never proof of a scientifically adequate definition.
        definition = _ABBREVIATION_DEFINITION.search(value[: match.start()])
        initials = "".join(word[0].upper() for word in definition[1].split()) if definition else ""
        defined = bool(
            definition and value[match.end() :].startswith(")") and initials.endswith(token)
        )
        if options.abstract_abbreviation_policy == "forbid" or not defined:
            candidates.append((token, defined))
    return candidates


def _abstract_checks(reading: _Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    if not options.forbid_abstract_citations and options.abstract_abbreviation_policy is None:
        return []
    document = reading.document
    regions, delimiter_reasons = _abstract_regions(reading)
    reasons = [*document.graph_uncertainty, *delimiter_reasons]
    for region in regions:
        reasons.extend(region.uncertainty)
    citations = []
    abbreviations = []
    abbreviation_names: list[str] = []
    for region in regions:
        cancellation_point()
        if region.uncertainty:
            continue
        literal = list(document.text[region.start : region.end])
        for command in document.between(region.start, region.end):
            if command.name in _CITATIONS:
                citations.append(document.located(command.start, command=command.name))
                start = command.start - region.start
                end = min(command_end(command), region.end) - region.start
                literal[start:end] = [" "] * (end - start)
        value, unknown = plain_text("".join(literal))
        reasons.extend(unknown)
        for token, defined in _abbreviation_candidates(value, options):
            abbreviations.append(
                document.located(
                    region.command.start, abbreviation=token, definition_candidate=defined
                )
            )
            abbreviation_names.append(token)
    results = []
    if options.forbid_abstract_citations:
        results.append(
            _policy_finding(
                "manuscript.abstract_citations",
                "Checked the abstract citation policy.",
                _CheckOutcome(citations, reasons),
                failure=f"{plural(len(citations), 'citation command')} in the abstract",
            )
        )
    if options.abstract_abbreviation_policy:
        results.append(
            _policy_finding(
                "manuscript.abstract_abbreviations",
                "Checked uppercase abbreviation candidates in abstracts.",
                _CheckOutcome(abbreviations, reasons),
                failure=f"{plural(len(abbreviations), 'abstract abbreviation candidate')} "
                "to review",
                listing=_listing("Abbreviations", abbreviation_names),
                weight=_ADVISORY,
                details={
                    "policy": options.abstract_abbreviation_policy,
                    "exceptions": list(options.abstract_abbreviation_exceptions),
                    "method": "Uppercase tokens of two or more characters; first occurrence "
                    "must follow expanded words in parentheses for a definition candidate. "
                    "Human review required.",
                },
            )
        )
    return results


def _heading_depth_finding(headings: list[_Heading], reasons: list[str], maximum: int) -> Finding:
    deep = [
        heading for heading in headings if heading["depth"] > maximum and not heading["uncertainty"]
    ]
    return _policy_finding(
        "manuscript.heading_depth",
        "Checked the configured literal heading depth.",
        _CheckOutcome(deep, reasons),
        failure=f"{len(deep)} of {plural(len(headings), 'literal heading')} deeper than depth "
        f"{maximum}",
        details={"maximum_depth": maximum, "headings": headings, "depths": dict(HEADING_DEPTHS)},
    )


def _empty_sections_finding(document: ExpandedDocument, headings: list[_Heading]) -> Finding:
    empty = []
    # Only the section's own heading and body decide whether it looks empty;
    # unexpanded constructs elsewhere cannot fill this subtree.
    unknown = list(document.graph_uncertainty)
    for heading in headings:
        nonempty, content_reasons = _content(document, heading["body_start"], heading["end"])
        local = [*heading["uncertainty"], *content_reasons]
        unknown.extend(local)
        if not nonempty and not local:
            empty.append(heading)
    return _policy_finding(
        "manuscript.empty_sections",
        "Checked apparently empty section subtrees.",
        _CheckOutcome(empty, unknown),
        failure=f"{len(empty)} of {plural(len(headings), 'section subtree')} with no "
        "observable content",
        weight=_ADVISORY,
    )


def _hardcoded_references_finding(reading: _Reading) -> Finding:
    document = reading.document
    # Blank command invocations and their arguments, preserving source offsets.
    buffer = list(document.text)
    for command in document.commands:
        start, stop = command.start, command_end(command)
        buffer[start:stop] = [" "] * (stop - start)
    matches = [
        document.located(match.start(), text=match.group())
        for match in _HARDCODED_REFERENCE.finditer("".join(buffer))
        if not in_conditional(document.conditionals, match.start())
    ]
    return _policy_finding(
        "manuscript.hardcoded_references",
        "Checked hard-coded reference candidates.",
        _CheckOutcome(matches, reading.uncertainty),
        failure=plural(len(matches), "hard-coded reference candidate"),
        weight=_ADVISORY,
    )


def _required_declarations_finding(
    document: ExpandedDocument,
    headings: list[_Heading],
    reasons: list[str],
    required: tuple[str, ...],
) -> Finding:
    declarations: list[dict[str, object]] = []
    unknown = list(reasons)
    required_names = {normalize_heading(name) for name in required}
    for heading in headings:
        if normalize_heading(heading["title"]) not in required_names:
            continue
        nonempty, content_reasons = _content(document, heading["body_start"], heading["end"])
        unknown.extend(content_reasons)
        declarations.append({**heading, "nonempty": nonempty})
    for command in document.commands:
        if command.name != "declaration":
            continue
        name, name_reasons = plain_argument_text(command)
        value, content_reasons = plain_argument_text(command, 1)
        unknown.extend([*name_reasons, *content_reasons])
        if command.depth:
            unknown.append("declaration inside a group or macro argument")
        if in_conditional(document.conditionals, command.start):
            unknown.append("conditional source control flow")
        declarations.append(
            document.located(
                command.start, title=name, nonempty=bool(value) and not is_placeholder_text(value)
            )
        )
    # An absent declaration could be generated by anything the scan could not
    # interpret, so a confirmed absence requires a fully interpreted source.
    missing = (
        [
            {"declaration": name, "reason": "missing or empty literal declaration"}
            for name in required
            if not any(
                normalize_heading(str(item["title"])) == normalize_heading(name)
                and item["nonempty"]
                for item in declarations
            )
        ]
        if not unknown
        else []
    )
    return _policy_finding(
        "manuscript.required_declarations",
        "Checked required nonempty declarations.",
        _CheckOutcome(missing, unknown),
        failure=f"{len(missing)} of {plural(len(required), 'required declaration')} missing or "
        "empty",
        listing=_listing("Missing", (str(item["declaration"]) for item in missing)),
        details={
            "declarations": declarations,
            "required": list(required),
            "adequacy": "Presence only; applicability and content require human review.",
        },
    )


def _heading_and_declaration_checks(
    reading: _Reading, options: ManuscriptCheckOptions
) -> list[Finding]:
    document = reading.document
    findings = []
    headings = _headings(document)
    reasons = [
        *reading.uncertainty,
        *(reason for heading in headings for reason in heading["uncertainty"]),
    ]
    if options.max_heading_depth is not None:
        findings.append(_heading_depth_finding(headings, reasons, options.max_heading_depth))
    if options.check_empty_sections:
        findings.append(_empty_sections_finding(document, headings))
    if options.check_hardcoded_references:
        findings.append(_hardcoded_references_finding(reading))
    if options.required_declarations:
        findings.append(
            _required_declarations_finding(
                document, headings, reasons, options.required_declarations
            )
        )
    return findings


def _required_metadata_finding(document: ExpandedDocument, required: tuple[str, ...]) -> Finding:
    metadata, reasons = _metadata(document)
    for name in required:
        reasons.extend(reason for item in metadata[name] for reason in item["uncertainty"])
        if len(metadata[name]) > 1:
            reasons.append(f"multiple {name} declarations have unresolved combination semantics")
    # Absent metadata could be declared by any construct the scan could not
    # read, so only a fully interpreted preamble confirms that it is missing.
    missing = (
        [
            {"field": name, "reason": "missing, empty or placeholder literal metadata"}
            for name in required
            if not any(item["value"] and not item["placeholder"] for item in metadata[name])
        ]
        if not reasons
        else []
    )
    return _policy_finding(
        "manuscript.required_metadata",
        "Checked configured literal metadata presence.",
        _CheckOutcome(missing, reasons),
        failure=f"{len(missing)} of {plural(len(required), 'required metadata field')} missing "
        "or empty",
        listing=_listing("Missing", (str(item["field"]) for item in missing)),
        details={
            "metadata": metadata,
            "required": list(required),
            "coverage": "Document-level presence; per-author completeness "
            "and identity are not established.",
        },
        # The details list every metadata field, so an unclear optional field is
        # reported evidence too and keeps the result from being waived.
        other_gaps=any(item["uncertainty"] for items in metadata.values() for item in items),
    )


def _orcid_checksum_valid(identifier: str) -> bool:
    """Check the ISO 7064 MOD 11-2 character of a syntactically valid ORCID identifier."""
    digits = identifier.replace("-", "")
    total = 0
    for digit in digits[:15]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


def _orcid_finding(reading: _Reading) -> Finding:
    document = reading.document
    identifiers = []
    invalid = []
    reasons = list(reading.uncertainty)
    for command in document.commands:
        if command.name not in _ORCID:
            continue
        value = command.arguments[0].value.strip() if command.arguments else ""
        if "\\" in value or not command.arguments:
            reasons.append("ORCID is not a literal argument")
            continue
        if in_conditional(document.conditionals, command.start):
            reasons.append("ORCID declared inside conditional source control flow")
            continue
        identifier = re.sub(r"^https://orcid\.org/", "", value)
        syntax = bool(_ORCID_IDENTIFIER.fullmatch(identifier))
        checksum = syntax and _orcid_checksum_valid(identifier)
        item = document.located(
            command.start, identifier=value, syntax_valid=syntax, checksum_valid=checksum
        )
        identifiers.append(item)
        if not syntax or not checksum:
            invalid.append(item)
    return _policy_finding(
        "manuscript.orcid",
        "Checked literal ORCID syntax and checksums.",
        _CheckOutcome(invalid, reasons),
        failure=f"{len(invalid)} of {plural(len(identifiers), 'literal ORCID identifier')} invalid",
        details={
            "identifiers": identifiers,
            "identity": "Checksum validity does not establish ownership or identity.",
        },
    )


def _metadata_checks(reading: _Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    results = []
    if options.required_metadata:
        results.append(_required_metadata_finding(reading.document, options.required_metadata))
    if options.check_orcid:
        results.append(_orcid_finding(reading))
    return results


def _declared_packages(command: Command) -> list[str]:
    if command.name not in PACKAGE_COMMANDS:
        return []
    return first_argument_names(command) or []


def _line_number_activation(command: Command) -> str | None:
    """Name the literal line-number activation a command performs, if any."""
    option_names = {name for item in command.options for name in literal_list(item.value) or []}
    environment = (
        command.arguments[0].value.strip()
        if command.name == "begin" and command.arguments
        else None
    )
    packages = _declared_packages(command)
    if command.name == "documentclass" and option_names & _LINE_NUMBER_OPTIONS:
        return "class option " + ", ".join(sorted(option_names & _LINE_NUMBER_OPTIONS))
    if "lineno" in packages and "linenumbers" in option_names:
        return "lineno package option linenumbers"
    if command.name in _LINE_NUMBER_COMMANDS:
        return "\\" + command.name
    if environment in _LINE_NUMBER_COMMANDS:
        return f"{environment} environment"
    return None


def _line_number_checks(
    document: ExpandedDocument, options: ManuscriptCheckOptions
) -> list[Finding]:
    """Find literal lineno activation; class-specific review modes are not interpreted."""
    if not (options.require_line_numbers or options.forbid_line_numbers):
        return []
    definitions = definition_spans(document.text, document.commands)
    activations: list[dict[str, object]] = []
    loaded: list[dict[str, object]] = []
    reasons = list(document.graph_uncertainty)
    for command in document.commands:
        cancellation_point()
        kind = _line_number_activation(command)
        if kind is None:
            if "lineno" in _declared_packages(command):
                loaded.append(document.location(command.start).as_dict())
            continue
        location = document.location(command.start)
        where = format_locations([location.as_dict()])
        if in_half_open_span(definitions, command.start):
            reasons.append(f"{where}: {kind} inside a macro definition")
        elif command.depth or in_conditional(document.conditionals, command.start):
            reasons.append(f"{where}: grouped or conditional {kind}")
        else:
            activations.append({**location.as_dict(), "activation": kind})
    details: dict[str, object] = {
        "policy": "require" if options.require_line_numbers else "forbid",
        "activations": activations,
        "lineno_loaded": loaded,
        "scope": "Literal lineno activation (\\linenumbers and its variants, the linenumbers "
        "package option or a lineno/linenumbers class option); class-specific review modes "
        "and macro-generated activation are not interpreted.",
    }
    if options.forbid_line_numbers:
        return [
            _policy_finding(
                "manuscript.line_numbers",
                "No literal line-number activation was found.",
                _CheckOutcome(activations, reasons),
                failure=f"Line numbering is active at {plural(len(activations), 'location')}",
                details=details,
            )
        ]
    missing = (
        []
        if activations or reasons
        else [
            {
                "path": document.graph.main,
                "line": None,
                "reason": "no literal line-number activation",
            }
        ]
    )
    return [
        _policy_finding(
            "manuscript.line_numbers",
            "Line numbering is activated literally.",
            _CheckOutcome(missing, [] if activations else reasons),
            failure="No literal line-number activation was found",
            details=details,
        )
    ]


class _FloatRule(NamedTuple):
    """One float policy: its rule, the title it reports, and its failure summary."""

    rule: str
    title: str
    # {count} is the number of violations; {floats}, {labels} and {figures} are counts
    # with their nouns: the inspected floats, the violating labels and the violating figures.
    failure: str


_FLOAT_CAPTIONS = _FloatRule(
    "manuscript.float_captions",
    "Checked literal figure/table captions.",
    "{count} of {floats} without a caption",
)
_FLOAT_LABELS = _FloatRule(
    "manuscript.float_labels",
    "Checked literal figure/table labels.",
    "{count} of {floats} without a label",
)
_FLOAT_LABEL_ORDER = _FloatRule(
    "manuscript.float_label_order",
    "Checked float label/caption source order.",
    "{labels} placed before the caption",
)
_FLOAT_REFERENCES = _FloatRule(
    "manuscript.float_references",
    "Checked external literal float references.",
    "{count} of {floats} without an external reference",
)
_FLOAT_REFERENCE_ORDER = _FloatRule(
    "manuscript.float_reference_order",
    "Checked first-reference source order.",
    "{count} of {floats} first referenced out of source order",
)
_FIGURE_DESCRIPTIONS = _FloatRule(
    "manuscript.figure_descriptions",
    "Checked literal figure descriptions.",
    "{figures} without a usable Description",
)
_FLOAT_RULES = (
    _FLOAT_CAPTIONS,
    _FLOAT_LABELS,
    _FLOAT_LABEL_ORDER,
    _FLOAT_REFERENCES,
    _FLOAT_REFERENCE_ORDER,
    _FIGURE_DESCRIPTIONS,
)


@dataclass(slots=True)
class _FloatRecord:
    """What the source shows about one figure or table environment."""

    location: Location
    kind: str
    # False when the float's own region could not be interpreted.
    confirmed: bool
    source_offset: int
    captions: list[dict[str, object]]
    labels: list[str]
    first_reference_offset: int | None
    # Labels that precede the float's first caption, as reported violations.
    misplaced_labels: list[dict[str, object]]
    # Whether a figure has a nonempty, non-placeholder Description; None for tables.
    described: bool | None

    def summary(self) -> dict[str, object]:
        return {
            **self.location.as_dict(),
            "kind": self.kind,
            "confirmed": self.confirmed,
            "source_offset": self.source_offset,
        }

    def details(self) -> dict[str, object]:
        return {
            **self.summary(),
            "captions": self.captions,
            "labels": self.labels,
            "first_reference_offset": self.first_reference_offset,
        }


@dataclass
class _FloatScan:
    """Reads each float region while collecting per-rule interpretation gaps."""

    document: ExpandedDocument
    floats: list[EnvironmentRegion]
    uncertainty: dict[_FloatRule, list[str]] = field(
        default_factory=lambda: {rule: [] for rule in _FLOAT_RULES}
    )
    first_references: dict[str, int] = field(default_factory=dict)
    label_counts: Counter[str] = field(default_factory=Counter)
    labels_seen: set[str] = field(default_factory=set)
    records: list[_FloatRecord] = field(default_factory=list)

    def note(self, reasons: Iterable[str], *rules: _FloatRule) -> None:
        for rule in rules:
            self.uncertainty[rule].extend(reasons)

    def run(self) -> None:
        self.label_counts.update(
            command.arguments[0].value.strip()
            for command in self.document.commands
            if command.name == "label" and command.arguments
        )
        self._collect_first_references()
        for region in self.floats:
            cancellation_point()
            self.records.append(self._record(region))

    def _collect_first_references(self) -> None:
        float_spans = merge_spans((region.command.start, region.end) for region in self.floats)
        for command in self.document.commands:
            if command.name not in REFERENCE_COMMANDS:
                continue
            if in_closed_span(float_spans, command.start):
                continue
            names = first_argument_names(command)
            if names is None or command.depth:
                self.note(
                    ["grouped or nonliteral reference"], _FLOAT_REFERENCES, _FLOAT_REFERENCE_ORDER
                )
                continue
            for argument in split_argument_items(command.arguments[0]):
                self.first_references.setdefault(argument.value, argument.start)

    def _record(self, region: EnvironmentRegion) -> _FloatRecord:
        document = self.document
        # A float whose own region could not be interpreted contributes reasons,
        # never violations: an unexpanded construct inside it may be the caption,
        # label or description that appears to be missing.
        confirmed = not region.uncertainty
        kind = region.kind.rstrip("*")
        captions = [command for command in region.commands if command.name == "caption"]
        caption_data = []
        for command in captions:
            value, unknown = plain_argument_text(command)
            self.note(unknown, _FLOAT_CAPTIONS)
            if command.depth:
                self.note(["caption inside a group or macro argument"], _FLOAT_CAPTIONS)
            caption_data.append(document.located(command.start, value=value))
        if len(captions) > 1:
            self.note(["multiple captions within one float"], _FLOAT_LABEL_ORDER)
        first_caption_end = min((command_end(command) for command in captions), default=None)
        labels = []
        misplaced = []
        for command in region.commands:
            if command.name != "label":
                continue
            names = first_argument_names(command)
            if command.arguments and not command.arguments[0].value.strip():
                continue
            if names is None or len(names) != 1 or command.depth:
                self.note(
                    ["grouped, empty or nonliteral float label"],
                    _FLOAT_LABELS,
                    _FLOAT_LABEL_ORDER,
                    _FLOAT_REFERENCES,
                    _FLOAT_REFERENCE_ORDER,
                )
                continue
            label = names[0]
            labels.append(label)
            if label in self.labels_seen or self.label_counts[label] > 1:
                self.note(
                    ["duplicate float labels have ambiguous reference targets"],
                    _FLOAT_REFERENCES,
                    _FLOAT_REFERENCE_ORDER,
                )
            self.labels_seen.add(label)
            if confirmed and (first_caption_end is None or first_caption_end > command.start):
                misplaced.append(document.located(command.start, label=label, kind=kind))
        first = min(
            (self.first_references[label] for label in labels if label in self.first_references),
            default=None,
        )
        if first is None:
            self.note(["a float lacks a label or external first reference"], _FLOAT_REFERENCE_ORDER)
        return _FloatRecord(
            location=document.location(region.command.start),
            kind=kind,
            confirmed=confirmed,
            source_offset=region.command.start,
            captions=caption_data,
            labels=labels,
            first_reference_offset=first,
            misplaced_labels=misplaced,
            described=self._described(region) if kind == "figure" else None,
        )

    def _described(self, region: EnvironmentRegion) -> bool:
        descriptions = [command for command in region.commands if command.name == "Description"]
        if len(descriptions) > 1:
            self.note(["multiple descriptions within one figure"], _FIGURE_DESCRIPTIONS)
        usable = False
        for command in descriptions:
            value, unknown = plain_argument_text(command)
            self.note(unknown, _FIGURE_DESCRIPTIONS)
            if command.depth:
                self.note(["description inside a group or macro argument"], _FIGURE_DESCRIPTIONS)
            usable = usable or (bool(value) and not is_placeholder_text(value))
        return usable


def _float_violations(records: list[_FloatRecord]) -> dict[_FloatRule, list[dict[str, object]]]:
    confirmed = [record for record in records if record.confirmed]
    out_of_order: list[dict[str, object]] = []
    for kind in ("figure", "table"):
        previous = -1
        for record in records:
            position = record.first_reference_offset
            if record.kind != kind or position is None:
                continue
            if position < previous and record.confirmed:
                out_of_order.append(record.details())
            previous = max(previous, position)
    return {
        _FLOAT_CAPTIONS: [
            record.details()
            for record in confirmed
            if not any(caption["value"] for caption in record.captions)
        ],
        _FLOAT_LABELS: [record.details() for record in confirmed if not record.labels],
        _FLOAT_LABEL_ORDER: [label for record in records for label in record.misplaced_labels],
        _FLOAT_REFERENCES: [
            {**record.summary(), "labels": record.labels}
            for record in confirmed
            if record.first_reference_offset is None
        ],
        _FLOAT_REFERENCE_ORDER: out_of_order,
        _FIGURE_DESCRIPTIONS: [
            {**record.summary(), "reason": "missing, empty or placeholder Description"}
            for record in confirmed
            if record.described is False
        ],
    }


def _float_checks(reading: _Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    enabled = [
        rule
        for rule, selected in (
            (_FLOAT_CAPTIONS, options.require_float_captions),
            (_FLOAT_LABELS, options.require_float_labels),
            (_FLOAT_LABEL_ORDER, options.check_float_label_order),
            (_FLOAT_REFERENCES, options.require_float_references),
            (_FLOAT_REFERENCE_ORDER, options.check_float_reference_order),
            (_FIGURE_DESCRIPTIONS, options.require_figure_descriptions),
        )
        if selected
    ]
    if not enabled:
        return []
    floats, structure_reasons = _scoped_regions(reading, _FLOAT_ENVIRONMENTS)
    # Unexpanded constructs outside every float cannot caption, label or hide one.
    reasons = [*reading.document.graph_uncertainty, *structure_reasons]
    for region in floats:
        reasons.extend(region.uncertainty)
    scan = _FloatScan(reading.document, floats)
    scan.run()
    violations = _float_violations(scan.records)
    details = {
        "floats": [record.details() for record in scan.records],
        "order": "Source appearance and source first reference only; "
        "rendered float order is not measured.",
        "description_quality": "Presence and obvious placeholders only; "
        "human review is needed for adequacy.",
    }
    return [
        _policy_finding(
            rule.rule,
            rule.title,
            _CheckOutcome(violations[rule], [*reasons, *scan.uncertainty[rule]]),
            failure=rule.failure.format(
                count=len(violations[rule]),
                floats=plural(len(floats), "float"),
                labels=plural(len(violations[rule]), "float label"),
                figures=plural(len(violations[rule]), "figure"),
            ),
            details=details,
        )
        for rule in enabled
    ]


_DEFAULT_OPTIONS = ManuscriptCheckOptions()


def check_manuscript_policies(
    root: Path,
    main: str | None,
    options: ManuscriptCheckOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Run selected source policy checks; defaults enable no additional manuscript policy."""
    if options == ManuscriptCheckOptions():
        return []
    document = expand_document(root, main)
    reading = _interpret(document)
    results = _package_checks(document, options)
    if options.check_layout_manipulation:
        results.append(
            _policy_finding(
                "manuscript.layout_manipulation",
                "Checked layout override candidates.",
                _CheckOutcome(_layout(document), reading.uncertainty),
                weight=_ADVISORY,
            )
        )
    results.extend(_abstract_checks(reading, options))
    results.extend(_heading_and_declaration_checks(reading, options))
    results.extend(_metadata_checks(reading, options))
    results.extend(_float_checks(reading, options))
    results.extend(_line_number_checks(document, options))
    return results
