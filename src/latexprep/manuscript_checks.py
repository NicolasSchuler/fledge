"""Opt-in, publisher-neutral checks of literal manuscript structures.

This is a bounded lexical interpretation, not TeX execution. Source order is
explicitly distinguished from rendered order and heuristic advice from policy
violations. No source is modified and no manuscript content is generated.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from .manuscript import (
    _PACKAGE_COMMANDS,
    _TEXT_WRAPPERS,
    _commands_before_end,
    _literal_list,
    _normalize_heading,
    _plain_text,
    _reachable_sources,
)
from .models import Finding, PreparationError
from .scheduler import cancellation_point
from .source import (
    _IMPORT_COMMANDS,
    MAX_COMMANDS,
    MAX_INPUT_DEPTH,
    MAX_TOTAL_SOURCE_BYTES,
    _Command,
    _commands,
    _Inspection,
    _items,
    _ParseLimit,
)


@dataclass(frozen=True)
class ManuscriptCheckOptions:
    allowed_packages: tuple[str, ...] | None = None
    inventory_packages: bool = False
    check_layout_manipulation: bool = False
    forbid_abstract_citations: bool = False
    abstract_abbreviation_policy: str | None = None
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

    def __post_init__(self) -> None:
        for name in (
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
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
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
            _literal_list(value) != [value] for value in self.allowed_packages
        ):
            raise PreparationError("allowed_packages must contain single literal package names")
        if self.abstract_abbreviation_policy not in {None, "forbid", "define"}:
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
_HEADINGS = {
    "part": -1,
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
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
_REFERENCES = {"ref", "pageref", "eqref", "autoref", "cref", "Cref", "nameref", "vref"}
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
_SAFE_COMMANDS = (
    set(_HEADINGS)
    | _CITATIONS
    | _REFERENCES
    | _ORCID
    | _LAYOUT
    | _LENGTHS
    | _PACKAGE_COMMANDS
    | _TEXT_WRAPPERS
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
_SAFE_ENVS = {
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
_NONCONTENT = {"label", "nocite", "centering", "raggedright", "raggedleft", "noindent"}
_IGNORED_SOURCE_RULES = {
    "source-edit-marker",
    "source-edit-command",
    "source-duplicate-label",
    "source-unresolved-reference",
}


class _Heading(TypedDict):
    path: object
    line: object
    title: str
    depth: int
    start: int
    body_start: int
    end: int
    uncertainty: list[str]


class _MetadataValue(TypedDict):
    path: object
    line: object
    value: str
    uncertainty: list[str]
    placeholder: bool


class _PackageValue(TypedDict):
    path: str
    line: int
    names: list[str] | None
    options: list[str]
    uncertainty: list[str]


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int
    path: str
    line: int


@dataclass
class _Document:
    inspection: _Inspection
    selected: set[str]
    text: str = ""
    commands: list[_Command] = field(default_factory=list)
    segments: list[_Segment] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    graph_uncertainty: list[str] = field(default_factory=list)
    starts: list[int] = field(default_factory=list)
    command_starts: list[int] = field(default_factory=list)
    newlines: list[int] = field(default_factory=list)

    def location(self, offset: int) -> dict[str, object]:
        if not self.segments:
            return {"path": self.inspection.main, "line": None}
        index = max(0, bisect_right(self.starts, offset) - 1)
        segment = self.segments[index]
        return {
            "path": segment.path,
            "line": segment.line
            + bisect_left(self.newlines, offset)
            - bisect_left(self.newlines, segment.start),
        }

    def between(self, start: int, end: int) -> list[_Command]:
        return self.commands[
            bisect_left(self.command_starts, start) : bisect_left(self.command_starts, end)
        ]


@dataclass
class _Region:
    kind: str
    command: _Command
    start: int
    end: int
    commands: list[_Command] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)


def _stop(command: _Command) -> int:
    return max(
        (arg.end + 1 for arg in (*command.arguments, *command.options)),
        default=command.start + len(command.name) + 1,
    )


def _read(root: Path, main: str | None) -> _Document:
    inspection = _Inspection(root, main)
    selected = _reachable_sources(inspection)
    document = _Document(inspection, selected)
    if inspection.main not in selected:
        document.graph_uncertainty.append("no inspectable selected main source")
    if inspection.subfile_targets:
        document.graph_uncertainty.append(
            "subfile body/preamble content interpretation is not supported by this check"
        )
    scope = selected | {inspection.main}
    document.graph_uncertainty.extend(
        f"{finding.path}: {finding.message}"
        for finding in inspection.findings
        if finding.rule not in _IGNORED_SOURCE_RULES
        and finding.rule.startswith("source-")
        and (finding.path in scope or finding.path is None)
        and finding.status in {"failed", "inconclusive"}
    )
    chunks: list[str] = []
    size = 0
    events = 0
    stopped = False

    def append(value: str, path: str, line: int) -> None:
        nonlocal size
        if not value:
            return
        if size + len(value) > MAX_TOTAL_SOURCE_BYTES:
            raise _ParseLimit
        document.segments.append(_Segment(size, size + len(value), path, line))
        chunks.append(value)
        size += len(value)

    def expand(path: str, stack: tuple[str, ...]) -> None:
        nonlocal events, stopped
        cancellation_point()
        if path in stack or len(stack) >= MAX_INPUT_DEPTH:
            document.graph_uncertainty.append("cyclic or excessively deep literal input graph")
            return
        source = inspection.sources.get(path)
        if source is None:
            document.graph_uncertainty.append(f"{path}: included source was not inspectable")
            return
        cursor = 0
        newlines = [index for index, char in enumerate(source.masked) if char == "\n"]
        for command in _commands_before_end(source):
            events += 1
            if events > MAX_COMMANDS:
                raise _ParseLimit
            if command.name == "endinput":
                if command.depth or source.masked[_stop(command) :].split("\n", 1)[0].strip():
                    document.graph_uncertainty.append(f"{path}: unresolved endinput context")
                append(
                    source.masked[cursor : command.start],
                    path,
                    bisect_right(newlines, cursor - 1) + 1,
                )
                return
            if command.name not in {"input", "include", *_IMPORT_COMMANDS}:
                continue
            argument_index = 1 if command.name in _IMPORT_COMMANDS else 0
            if command.depth or len(command.arguments) <= argument_index:
                document.graph_uncertainty.append(f"{path}: nonliteral or grouped inclusion")
                continue
            arguments = _items(command.arguments[argument_index], False)
            argument = arguments[0]
            resolved = inspection.references.get((path, argument.start, argument.end))
            if resolved is None:
                document.graph_uncertainty.append(f"{path}: unresolved literal inclusion")
                continue
            if len(command.arguments) > argument_index + 1 or command.options:
                document.graph_uncertainty.append(
                    f"{path}: trailing grouped or optional syntax follows a source inclusion"
                )
            append(
                source.masked[cursor : command.start], path, bisect_right(newlines, cursor - 1) + 1
            )
            expand(resolved[0], (*stack, path))
            # The shared lexer records subsequent groups as possible arguments.
            # Input has only one: preserve later groups and bare-input terminators.
            cursor = command.arguments[argument_index].end
            if source.masked[cursor : cursor + 1] == "}":
                cursor += 1
            if stopped:
                return
        closing = next(
            (
                command
                for command in source.commands
                if command.name == "end"
                and not command.depth
                and command.arguments
                and command.arguments[0].value.strip() == "document"
            ),
            None,
        )
        end = closing.start if closing else len(source.masked)
        append(source.masked[cursor:end], path, bisect_right(newlines, cursor - 1) + 1)
        if closing:
            stopped = True

    try:
        if inspection.main in selected:
            assert inspection.main is not None
            expand(inspection.main, ())
        document.text = "".join(chunks)
        document.starts = [segment.start for segment in document.segments]
        document.commands = _commands(document.text)
        if len(document.commands) > MAX_COMMANDS:
            raise _ParseLimit
    except _ParseLimit:
        document.graph_uncertainty.append("expanded manuscript exceeded a bounded parsing limit")
        document.text = "".join(chunks)
        document.starts = [segment.start for segment in document.segments]
        document.commands = []
    document.graph_uncertainty = sorted(set(document.graph_uncertainty))
    document.command_starts = [command.start for command in document.commands]
    document.newlines = [index for index, char in enumerate(document.text) if char == "\n"]
    document.uncertainty = list(document.graph_uncertainty)
    unknown = set()
    for command in document.commands:
        cancellation_point()
        if command.name not in _SAFE_COMMANDS:
            unknown.add("\\" + command.name)
        if command.name in {"begin", "end"} and command.arguments:
            environment = command.arguments[0].value.strip()
            if environment not in _SAFE_ENVS:
                unknown.add("environment " + environment)
        if command.name in {"setcounter", "addtocounter", "refstepcounter", "stepcounter"}:
            unknown.add("explicit counter modification")
    if unknown:
        document.uncertainty.append(
            "unexpanded or context-dependent constructs: " + ", ".join(sorted(unknown)[:20])
        )
    return document


def _value(command: _Command, argument: int = 0) -> tuple[str, list[str]]:
    if len(command.arguments) <= argument:
        return "", ["missing literal argument"]
    value, uncertainty = _plain_text(command.arguments[argument].value)
    if not value.strip() and re.search(r"(?<!\\)\$|\\[([]", command.arguments[argument].value):
        uncertainty.append("only mathematical content is observable")
    return " ".join(value.split()), uncertainty


def _placeholder(value: str) -> bool:
    normalized = value.casefold().strip(" .<>[]")
    return bool(
        re.search(r"\b(?:todo|tbd|fixme|xxx)\b|\?\?", normalized)
        or normalized
        in {
            "placeholder",
            "description",
            "figure description",
            "author name",
            "your name",
            "your title",
            "your affiliation",
            "email address",
        }
        or re.fullmatch(
            r"(?:insert|add|enter|replace with|your) .*(?:here|description|text)", normalized
        )
    )


def _finding(
    rule: str,
    title: str,
    document: _Document,
    violations: Sequence[Mapping[str, object]],
    uncertainty: list[str],
    *,
    advisory: bool = False,
    extra: dict[str, object] | None = None,
) -> Finding:
    reasons = sorted(set(uncertainty))
    status = "inconclusive" if reasons else "failed" if violations else "passed"
    details: dict[str, object] = {
        "matches": [dict(item) for item in violations],
        "uncertainty": reasons,
        "scope": "Selected literal source graph; macros are not expanded "
        "and rendered output is not inferred.",
    }
    if extra:
        details.update(extra)
    location = violations[0] if len(violations) == 1 else {}
    path = location.get("path")
    line = location.get("line")
    return Finding(
        rule,
        title + (" Inconclusive: " + reasons[0] + "." if reasons else ""),
        "info" if status == "passed" else "warning" if advisory else "error",
        status,
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) else None,
        evidence="heuristic" if advisory else "derived",
        details=details,
    )


def _regions(document: _Document, kinds: set[str]) -> tuple[list[_Region], list[str]]:
    result: list[_Region] = []
    stack: list[_Region] = []
    reasons: list[str] = []
    for command in document.commands:
        cancellation_point()
        environment = command.arguments[0].value.strip() if command.arguments else ""
        if command.name == "begin" and environment in kinds:
            region = _Region(environment, command, _stop(command), len(document.text))
            if command.depth:
                region.uncertainty.append("environment declared inside a group or macro argument")
            if stack:
                region.uncertainty.append("nested manuscript environments")
                stack[-1].uncertainty.append("nested manuscript environments")
            stack.append(region)
        elif command.name == "end" and environment in kinds:
            if not stack or stack[-1].kind != environment:
                reasons.append("unmatched or crossed environment delimiters")
                continue
            region = stack.pop()
            region.end = command.start
            result.append(region)
        elif stack:
            stack[-1].commands.append(command)
    for region in stack:
        region.uncertainty.append("environment has no closing delimiter")
        result.append(region)
    return sorted(result, key=lambda region: region.command.start), reasons


def _headings(document: _Document) -> list[_Heading]:
    commands = [command for command in document.commands if command.name in _HEADINGS]
    result: list[_Heading] = []
    pending: list[int] = []
    for command in commands:
        cancellation_point()
        value, reasons = _value(command)
        if command.depth:
            reasons.append("heading inside a group or macro argument")
        while pending and result[pending[-1]]["depth"] >= _HEADINGS[command.name]:
            result[pending.pop()]["end"] = command.start
        location = document.location(command.start)
        result.append(
            {
                "path": location["path"],
                "line": location["line"],
                "title": value,
                "depth": _HEADINGS[command.name],
                "start": command.start,
                "body_start": _stop(command),
                "end": len(document.text),
                "uncertainty": reasons,
            }
        )
        pending.append(len(result) - 1)
    return result


def _content(document: _Document, start: int, end: int) -> tuple[bool, list[str]]:
    text = document.text[start:end]
    commands = document.between(start, end)
    edits = []
    for command in commands:
        if command.name in set(_HEADINGS) | _NONCONTENT | {"declaration"}:
            edits.append((command.start - start, min(_stop(command), end) - start))
    for opening, closing in reversed(edits):
        text = text[:opening] + " " * (closing - opening) + text[closing:]
    plain, reasons = _plain_text(text)
    if any(command.name in {"includegraphics", "rule"} for command in commands):
        return True, []
    if re.search(r"(?<!\\)\$|\\[([]|\\begin\{(?:equation|align|displaymath|math)", text):
        return True, []
    # Unknown commands prevent an empty-content claim even when their arguments
    # happen to contain text; semantic content cannot be inferred from an argument.
    return bool(plain.strip()), reasons


def _metadata(document: _Document) -> tuple[dict[str, list[_MetadataValue]], list[str]]:
    values: dict[str, list[_MetadataValue]] = {name: [] for name in _METADATA}
    reasons = list(document.uncertainty)
    for command in document.commands:
        for name, commands in _METADATA.items():
            if command.name not in commands:
                continue
            value, unknown = _value(command)
            if command.depth:
                unknown.append("metadata inside a group or macro argument")
            location = document.location(command.start)
            values[name].append(
                {
                    "path": location["path"],
                    "line": location["line"],
                    "value": value,
                    "uncertainty": unknown,
                    "placeholder": _placeholder(value),
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
    document = _read(root, main)
    metadata, reasons = _metadata(document)
    values: dict[str, str] = {}
    for key, declarations in metadata.items():
        if fields is not None and key not in fields:
            continue
        if len(declarations) > 1:
            reasons.append(f"multiple {key} declarations have unresolved combination semantics")
        for declaration in declarations:
            reasons.extend(str(reason) for reason in declaration["uncertainty"])
        if (
            len(declarations) == 1
            and not declarations[0]["uncertainty"]
            and not declarations[0]["placeholder"]
            and declarations[0]["value"]
        ):
            values[key] = str(declarations[0]["value"])
    return values, sorted(set(reasons))


def _package_checks(document: _Document, options: ManuscriptCheckOptions) -> list[Finding]:
    if options.allowed_packages is None and not options.inventory_packages:
        return []
    declarations: list[_PackageValue] = []
    reasons = list(document.graph_uncertainty)
    for path in sorted(document.selected):
        commands = _commands_before_end(document.inspection.sources[path])
        conditional = any(command.name.startswith(("if", "If")) for command in commands)
        for command in commands:
            if command.name == "endinput" and (
                command.depth
                or conditional
                or document.inspection.sources[path]
                .masked[_stop(command) :]
                .split("\n", 1)[0]
                .strip()
            ):
                reasons.append(f"{path}: package declarations have unresolved endinput context")
            if command.name not in _PACKAGE_COMMANDS:
                if command.name not in _SAFE_COMMANDS | {
                    "ProvidesPackage",
                    "ProvidesClass",
                    "ProvidesFile",
                    "NeedsTeXFormat",
                }:
                    reasons.append(
                        f"{path}: unexpanded constructs can generate package declarations"
                    )
                continue
            names = _literal_list(command.arguments[0].value) if command.arguments else None
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
    scope = (
        "Direct literal package declarations in selected local sources, classes and styles; "
        "transitive system package loading and effective options are not established."
    )
    findings = [
        Finding(
            "manuscript.package_inventory",
            "Inventoried direct local package declarations.",
            "info",
            "inconclusive" if reasons else "passed",
            evidence="direct",
            details={
                "declarations": declarations,
                "uncertainty": sorted(set(reasons)),
                "scope": scope,
            },
        )
    ]
    if options.allowed_packages is not None:
        matches = [
            {
                **declaration,
                "disallowed": [
                    name for name in declaration["names"] if name not in options.allowed_packages
                ],
            }
            for declaration in declarations
            if declaration["names"] is not None
            and any(name not in options.allowed_packages for name in declaration["names"])
        ]
        findings.append(
            _finding(
                "manuscript.package_allowlist",
                "Checked the configured direct-package allowlist.",
                document,
                matches,
                reasons,
                extra={
                    "allowed": list(options.allowed_packages),
                    "declarations": declarations,
                    "scope": scope,
                },
            )
        )
    return findings


def _layout(document: _Document) -> list[dict[str, object]]:
    matches = []
    for command in document.commands:
        selected = command.name in _LAYOUT
        if command.name in {"hspace", "vspace", "hskip", "vskip", "kern"}:
            value = (
                command.arguments[0].value
                if command.arguments
                else document.text[command.start + len(command.name) + 1 :][:80]
            )
            selected = bool(re.match(r"\s*-", value))
        elif command.name in {"setlength", "addtolength"} and command.arguments:
            selected = command.arguments[0].value.strip().lstrip("\\") in _LENGTHS
        elif command.name in _LENGTHS:
            selected = bool(re.match(r"\s*(?:=\s*)?[-+]?\d", document.text[_stop(command) :][:80]))
        elif command.name == "begin" and command.arguments:
            selected = command.arguments[0].value in {
                "figure",
                "figure*",
                "table",
                "table*",
            } and any(
                "H" in argument.value or "!" in argument.value for argument in command.options
            )
        if selected:
            matches.append(
                {
                    **document.location(command.start),
                    "command": command.name,
                    "source": document.text[
                        command.start : min(_stop(command), command.start + 160)
                    ],
                }
            )
    return matches


def _abstract_checks(document: _Document, options: ManuscriptCheckOptions) -> list[Finding]:
    if not options.forbid_abstract_citations and options.abstract_abbreviation_policy is None:
        return []
    regions, reasons = _regions(document, {"abstract"})
    for command in document.commands:
        if command.name == "abstract":
            unknown = (
                []
                if command.arguments and not command.depth
                else ["ambiguous abstract declaration"]
            )
            regions.append(
                _Region(
                    "abstract",
                    command,
                    command.arguments[0].start if command.arguments else _stop(command),
                    command.arguments[0].end if command.arguments else _stop(command),
                    uncertainty=unknown,
                )
            )
    reasons = [*document.uncertainty, *reasons]
    for region in regions:
        reasons.extend(region.uncertainty)
    citations = []
    abbreviations = []
    text_reasons = []
    for region in regions:
        cancellation_point()
        if region.uncertainty:
            continue
        literal = list(document.text[region.start : region.end])
        for command in document.between(region.start, region.end):
            if command.name in _CITATIONS:
                citations.append({**document.location(command.start), "command": command.name})
                start, end = (
                    command.start - region.start,
                    min(_stop(command), region.end) - region.start,
                )
                literal[start:end] = [" "] * (end - start)
        value, unknown = _plain_text("".join(literal))
        text_reasons.extend(unknown)
        seen: set[str] = set(options.abstract_abbreviation_exceptions)
        for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,}\b", value):
            token = match.group()
            if token in seen:
                continue
            seen.add(token)
            prefix = value[: match.start()]
            # This is a review candidate, never proof of a scientifically adequate definition.
            definition = re.search(
                r"((?:\b[A-Za-z][A-Za-z-]*\s+){1,10}[A-Za-z][A-Za-z-]*)\s*\($", prefix
            )
            initials = (
                "".join(word[0].upper() for word in definition[1].split()) if definition else ""
            )
            defined = bool(
                definition and value[match.end() :].startswith(")") and initials.endswith(token)
            )
            if options.abstract_abbreviation_policy == "forbid" or not defined:
                abbreviations.append(
                    {
                        **document.location(region.command.start),
                        "abbreviation": token,
                        "definition_candidate": defined,
                    }
                )
    results = []
    if options.forbid_abstract_citations:
        results.append(
            _finding(
                "manuscript.abstract_citations",
                "Checked the abstract citation policy.",
                document,
                citations,
                [*reasons, *text_reasons],
            )
        )
    if options.abstract_abbreviation_policy:
        results.append(
            _finding(
                "manuscript.abstract_abbreviations",
                "Checked uppercase abbreviation candidates in abstracts.",
                document,
                abbreviations,
                [*reasons, *text_reasons],
                advisory=True,
                extra={
                    "policy": options.abstract_abbreviation_policy,
                    "exceptions": list(options.abstract_abbreviation_exceptions),
                    "method": "Uppercase tokens of two or more characters; first occurrence "
                    "must follow expanded words in parentheses for a definition candidate. "
                    "Human review required.",
                },
            )
        )
    return results


def _structure_checks(document: _Document, options: ManuscriptCheckOptions) -> list[Finding]:
    findings = []
    headings = _headings(document)
    reasons = [
        *document.uncertainty,
        *(reason for heading in headings for reason in heading["uncertainty"]),
    ]
    if options.max_heading_depth is not None:
        findings.append(
            _finding(
                "manuscript.heading_depth",
                "Checked the configured literal heading depth.",
                document,
                [heading for heading in headings if heading["depth"] > options.max_heading_depth],
                reasons,
                extra={
                    "maximum_depth": options.max_heading_depth,
                    "headings": headings,
                    "depths": dict(_HEADINGS),
                },
            )
        )
    if options.check_empty_sections:
        empty = []
        unknown = list(reasons)
        for heading in headings:
            nonempty, content_reasons = _content(document, heading["body_start"], heading["end"])
            unknown.extend(content_reasons)
            if not nonempty:
                empty.append(heading)
        findings.append(
            _finding(
                "manuscript.empty_sections",
                "Checked apparently empty section subtrees.",
                document,
                empty,
                unknown,
                advisory=True,
            )
        )
    if options.check_hardcoded_references:
        # Blank command invocations and their arguments, preserving source offsets.
        buffer = list(document.text)
        for command in document.commands:
            start, stop = command.start, _stop(command)
            buffer[start:stop] = [" "] * (stop - start)
        plain = "".join(buffer)
        matches = [
            {**document.location(match.start()), "text": match.group()}
            for match in re.finditer(
                r"\b(?:Figures?|Figs?\.?|Tables?|Sections?|Secs?\.?|"
                r"Equations?|Eqs?\.?)\s*[~ ]\s*\(?\d+(?:\.\d+)*\)?",
                plain,
            )
        ]
        findings.append(
            _finding(
                "manuscript.hardcoded_references",
                "Checked hard-coded reference candidates.",
                document,
                matches,
                document.uncertainty,
                advisory=True,
            )
        )
    if options.required_declarations:
        declarations = []
        unknown = list(reasons)
        required_names = {_normalize_heading(name) for name in options.required_declarations}
        for heading in headings:
            if _normalize_heading(str(heading["title"])) not in required_names:
                continue
            nonempty, content_reasons = _content(document, heading["body_start"], heading["end"])
            unknown.extend(content_reasons)
            declarations.append({**heading, "nonempty": nonempty})
        for command in document.commands:
            if command.name != "declaration":
                continue
            name, name_reasons = _value(command)
            value, content_reasons = _value(command, 1)
            unknown.extend([*name_reasons, *content_reasons])
            if command.depth:
                unknown.append("declaration inside a group or macro argument")
            declarations.append(
                {
                    **document.location(command.start),
                    "title": name,
                    "nonempty": bool(value) and not _placeholder(value),
                }
            )
        missing = [
            {"declaration": required, "reason": "missing or empty literal declaration"}
            for required in options.required_declarations
            if not any(
                _normalize_heading(str(item["title"])) == _normalize_heading(required)
                and item["nonempty"]
                for item in declarations
            )
        ]
        findings.append(
            _finding(
                "manuscript.required_declarations",
                "Checked required nonempty declarations.",
                document,
                missing,
                unknown,
                extra={
                    "declarations": declarations,
                    "required": list(options.required_declarations),
                    "adequacy": "Presence only; applicability and content require human review.",
                },
            )
        )
    return findings


def _metadata_checks(document: _Document, options: ManuscriptCheckOptions) -> list[Finding]:
    results = []
    if options.required_metadata:
        metadata, reasons = _metadata(document)
        for name in options.required_metadata:
            reasons.extend(reason for item in metadata[name] for reason in item["uncertainty"])
            if len(metadata[name]) > 1:
                reasons.append(
                    f"multiple {name} declarations have unresolved combination semantics"
                )
        missing = [
            {"field": name, "reason": "missing, empty or placeholder literal metadata"}
            for name in options.required_metadata
            if not any(item["value"] and not item["placeholder"] for item in metadata[name])
        ]
        results.append(
            _finding(
                "manuscript.required_metadata",
                "Checked configured literal metadata presence.",
                document,
                missing,
                reasons,
                extra={
                    "metadata": metadata,
                    "required": list(options.required_metadata),
                    "coverage": "Document-level presence; per-author completeness "
                    "and identity are not established.",
                },
            )
        )
    if options.check_orcid:
        identifiers = []
        invalid = []
        reasons = list(document.uncertainty)
        for command in document.commands:
            if command.name not in _ORCID:
                continue
            value = command.arguments[0].value.strip() if command.arguments else ""
            if "\\" in value or not command.arguments:
                reasons.append("ORCID is not a literal argument")
                continue
            identifier = re.sub(r"^https://orcid\.org/", "", value)
            syntax = bool(re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", identifier, re.ASCII))
            checksum = False
            if syntax:
                digits = identifier.replace("-", "")
                total = 0
                for digit in digits[:15]:
                    total = (total + int(digit)) * 2
                check = (12 - total % 11) % 11
                checksum = digits[-1] == ("X" if check == 10 else str(check))
            item = {
                **document.location(command.start),
                "identifier": value,
                "syntax_valid": syntax,
                "checksum_valid": checksum,
            }
            identifiers.append(item)
            if not syntax or not checksum:
                invalid.append(item)
        results.append(
            _finding(
                "manuscript.orcid",
                "Checked literal ORCID syntax and checksums.",
                document,
                invalid,
                reasons,
                extra={
                    "identifiers": identifiers,
                    "identity": "Checksum validity does not establish ownership or identity.",
                },
            )
        )
    return results


def _float_checks(document: _Document, options: ManuscriptCheckOptions) -> list[Finding]:
    rules = [
        (
            options.require_float_captions,
            "float_captions",
            "Checked literal figure/table captions.",
        ),
        (options.require_float_labels, "float_labels", "Checked literal figure/table labels."),
        (
            options.check_float_label_order,
            "float_label_order",
            "Checked float label/caption source order.",
        ),
        (
            options.require_float_references,
            "float_references",
            "Checked external literal float references.",
        ),
        (
            options.check_float_reference_order,
            "float_reference_order",
            "Checked first-reference source order.",
        ),
        (
            options.require_figure_descriptions,
            "figure_descriptions",
            "Checked literal figure descriptions.",
        ),
    ]
    if not any(enabled for enabled, _, _ in rules):
        return []
    floats, structure_reasons = _regions(document, {"figure", "figure*", "table", "table*"})
    reasons = [*document.uncertainty, *structure_reasons]
    for region in floats:
        reasons.extend(region.uncertainty)
    records = []
    violations: dict[str, list[dict[str, object]]] = {name: [] for _, name, _ in rules}
    extra_reasons: dict[str, list[str]] = {name: [] for _, name, _ in rules}
    labels_seen: set[str] = set()
    all_label_counts: dict[str, int] = {}
    for command in document.commands:
        if command.name == "label" and command.arguments:
            label = command.arguments[0].value.strip()
            all_label_counts[label] = all_label_counts.get(label, 0) + 1
    first_references: dict[str, int] = {}
    float_intervals: list[tuple[int, int]] = []
    for region in floats:
        if float_intervals and region.command.start <= float_intervals[-1][1]:
            float_intervals[-1] = (float_intervals[-1][0], max(float_intervals[-1][1], region.end))
        else:
            float_intervals.append((region.command.start, region.end))
    float_starts = [interval[0] for interval in float_intervals]
    for command in document.commands:
        if command.name not in _REFERENCES:
            continue
        interval_index = bisect_right(float_starts, command.start) - 1
        if interval_index >= 0 and command.start <= float_intervals[interval_index][1]:
            continue
        names = _literal_list(command.arguments[0].value) if command.arguments else None
        if names is None or command.depth:
            for name in ("float_references", "float_reference_order"):
                extra_reasons[name].append("grouped or nonliteral reference")
            continue
        for argument in _items(command.arguments[0], True):
            first_references.setdefault(argument.value, argument.start)
    for region in floats:
        cancellation_point()
        record: dict[str, object] = {
            **document.location(region.command.start),
            "kind": region.kind.rstrip("*"),
            "source_offset": region.command.start,
        }
        captions = [command for command in region.commands if command.name == "caption"]
        labels = [command for command in region.commands if command.name == "label"]
        descriptions = [command for command in region.commands if command.name == "Description"]
        caption_data = []
        for command in captions:
            value, unknown = _value(command)
            extra_reasons["float_captions"].extend(unknown)
            if command.depth:
                extra_reasons["float_captions"].append("caption inside a group or macro argument")
            caption_data.append({**document.location(command.start), "value": value})
        if len(captions) > 1:
            extra_reasons["float_label_order"].append("multiple captions within one float")
        first_caption_end = min((_stop(command) for command in captions), default=None)
        if not any(item["value"] for item in caption_data):
            violations["float_captions"].append(record)
        literal_labels = []
        for command in labels:
            names = _literal_list(command.arguments[0].value) if command.arguments else None
            if command.arguments and not command.arguments[0].value.strip():
                continue
            if names is None or len(names) != 1 or command.depth:
                for name in (
                    "float_labels",
                    "float_label_order",
                    "float_references",
                    "float_reference_order",
                ):
                    extra_reasons[name].append("grouped, empty or nonliteral float label")
                continue
            label = names[0]
            literal_labels.append(label)
            if label in labels_seen or all_label_counts.get(label, 0) > 1:
                for name in ("float_references", "float_reference_order"):
                    extra_reasons[name].append(
                        "duplicate float labels have ambiguous reference targets"
                    )
            labels_seen.add(label)
            if first_caption_end is None or first_caption_end > command.start:
                violations["float_label_order"].append(
                    {**document.location(command.start), "label": label, "kind": record["kind"]}
                )
        if not literal_labels:
            violations["float_labels"].append(record)
        first = min(
            (first_references[label] for label in literal_labels if label in first_references),
            default=None,
        )
        if first is None:
            violations["float_references"].append({**record, "labels": literal_labels})
            extra_reasons["float_reference_order"].append(
                "a float lacks a label or external first reference"
            )
        if region.kind.startswith("figure"):
            valid_descriptions = []
            if len(descriptions) > 1:
                extra_reasons["figure_descriptions"].append(
                    "multiple descriptions within one figure"
                )
            for command in descriptions:
                value, unknown = _value(command)
                extra_reasons["figure_descriptions"].extend(unknown)
                if command.depth:
                    extra_reasons["figure_descriptions"].append(
                        "description inside a group or macro argument"
                    )
                valid_descriptions.append(bool(value) and not _placeholder(value))
            if not any(valid_descriptions):
                violations["figure_descriptions"].append(
                    {**record, "reason": "missing, empty or placeholder Description"}
                )
        record.update(
            {"captions": caption_data, "labels": literal_labels, "first_reference_offset": first}
        )
        records.append(record)
    for kind in ("figure", "table"):
        previous = -1
        for record in records:
            if record["kind"] != kind or record["first_reference_offset"] is None:
                continue
            position = record["first_reference_offset"]
            if not isinstance(position, int):
                continue
            if position < previous:
                violations["float_reference_order"].append(record)
            previous = max(previous, position)
    return [
        _finding(
            "manuscript." + name,
            message,
            document,
            violations[name],
            [*reasons, *extra_reasons[name]],
            extra={
                "floats": records,
                "order": "Source appearance and source first reference only; "
                "rendered float order is not measured.",
                "description_quality": "Presence and obvious placeholders only; "
                "human review is needed for adequacy.",
            },
        )
        for enabled, name, message in rules
        if enabled
    ]


_DEFAULT_OPTIONS = ManuscriptCheckOptions()


def check_manuscript_details(
    root: Path,
    main: str | None,
    options: ManuscriptCheckOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Run selected source checks; defaults enable no additional manuscript policy."""
    if options == ManuscriptCheckOptions():
        return []
    document = _read(root, main)
    results = _package_checks(document, options)
    if options.check_layout_manipulation:
        results.append(
            _finding(
                "manuscript.layout_manipulation",
                "Checked layout override candidates.",
                document,
                _layout(document),
                document.uncertainty,
                advisory=True,
            )
        )
    results.extend(_abstract_checks(document, options))
    results.extend(_structure_checks(document, options))
    results.extend(_metadata_checks(document, options))
    results.extend(_float_checks(document, options))
    return results
