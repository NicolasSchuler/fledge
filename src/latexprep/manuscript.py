"""Configurable checks of literal declarations in one selected source graph.

This module does not execute TeX or inventory packages loaded by the toolchain.
Counts use an explicitly documented lexical convention, not rendered PDF words.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from .expanded_document import (
    CONTENT_INPUT_COMMANDS,
    commands_before_end,
    reachable_sources,
    unresolved_non_content_file,
)
from .file_scan import check_status, format_locations, more_suffix, plural
from .literal_text import (
    HEADING_DEPTHS,
    KEY_ARGUMENT_COMMANDS,
    KEYWORD_ENVIRONMENTS,
    MATH_ENVIRONMENT_PATTERN,
    PACKAGE_COMMANDS,
    Declaration,
    first_argument_names,
    literal_list,
    normalize_heading,
    plain_text,
    scan_literal_text,
)
from .models import EvidenceKind, Finding, PreparationError, Status
from .option_config import require_booleans
from .scheduler import cancellation_point
from .source_graph import ParsedSource, SourceGraph
from .tex_lexer import CONTROL_SEQUENCE, Command, conditional_spans, in_conditional


@dataclass(frozen=True)
class ManuscriptOptions:
    expected_document_class: str | None = None
    required_class_options: tuple[str, ...] = ()
    forbidden_class_options: tuple[str, ...] = ()
    forbidden_packages: tuple[str, ...] = ()
    require_abstract: bool = False
    abstract_min_words: int | None = None
    abstract_max_words: int | None = None
    keywords_min_count: int | None = None
    keywords_max_count: int | None = None
    required_sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_booleans(self, "require_abstract")
        if self.expected_document_class is not None and (
            not isinstance(self.expected_document_class, str)
            or literal_list(self.expected_document_class) != [self.expected_document_class]
        ):
            raise PreparationError("expected_document_class must be a single nonempty class name")
        for name in (
            "required_class_options",
            "forbidden_class_options",
            "forbidden_packages",
            "required_sections",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                for value in values
            ):
                raise PreparationError(
                    f"{name} must contain nonempty strings without surrounding spaces"
                )
            repeated = sorted({value for value in values if values.count(value) > 1})
            if repeated:
                raise PreparationError(f"{name} lists {', '.join(map(repr, repeated))} twice")
        for name in ("required_class_options", "forbidden_class_options", "forbidden_packages"):
            for value in getattr(self, name):
                if literal_list(value) != [value]:
                    raise PreparationError(
                        f"{name} entry {value!r} must be a single literal name "
                        "without commas or TeX commands"
                    )
        for lower_name, upper_name in (
            ("abstract_min_words", "abstract_max_words"),
            ("keywords_min_count", "keywords_max_count"),
        ):
            lower, upper = getattr(self, lower_name), getattr(self, upper_name)
            for limit_name, value in ((lower_name, lower), (upper_name, upper)):
                if value is not None and (type(value) is not int or value < 0):
                    raise PreparationError(f"{limit_name} must be a nonnegative integer")
            if lower is not None and upper is not None and lower > upper:
                raise PreparationError(f"{lower_name} must not exceed {upper_name}")
        both = sorted(set(self.required_class_options) & set(self.forbidden_class_options))
        if both:
            raise PreparationError(
                f"Class option {', '.join(map(repr, both))} is in both required_class_options "
                "and forbidden_class_options"
            )


_COUNT_METHOD = (
    "Unicode alphanumeric tokens; internal hyphens and apostrophes stay in one word. "
    "Comments, common literal/code regions, math, and citation/reference/label arguments "
    "count as zero. Common text-formatting wrappers retain their text. Bibliography, "
    "layout and other known non-content commands count as zero without an interpretation "
    "gap. Each unresolved macro widens the reported range by one word, so only a range "
    "that lies wholly inside or outside the limits is decisive. This is a source count, "
    "not a rendered word count."
)
_WORD = re.compile(r"[^\W_]+(?:['’\-‑][^\W_]+)*")
_MATH_ONLY = re.compile(rf"(?<!\\)\$|\\[([]|\\begin\{{(?:{MATH_ENVIRONMENT_PATTERN})\}}")
_CITATION_AND_REFERENCE_COMMANDS = KEY_ARGUMENT_COMMANDS - {"label", "nocite"}
# A resolved source finding records how a reference was interpreted; only an
# unresolved one of these leaves the manuscript interpretation incomplete.
_INCOMPLETE_SOURCE_RULES = {
    "source.encoding",
    "source.size_limit",
    "source.argument_limit",
    "source.command_limit",
    "source.literal_unclosed",
    "source.input_cycle",
    "source.input_depth",
    "source.expansion_limit",
    "source.dynamic_path",
    "source.nested_file_command",
    "source.conditional_dependency",
    "source.path_literal_context",
    "source.external_path",
    "source.ambiguous_dependency",
    "source.case_mismatch",
    "source.custom_path",
    "source.unsupported_command",
    "source.missing_dependency",
    "source.subfile_structure",
    "source.font_options",
}
_SYNTAX_CHANGING_COMMANDS = {"csname", "catcode", "scantokens"}
_KEYWORD_COUNT_METHOD = (
    "Nonempty phrases separated by commas, semicolons, \\sep, or \\and in \\keywords{} "
    "or keywords/keyword/IEEEkeywords environments."
)

_BlockKind = Literal["abstract", "keywords"]


class _LiteralHeading(TypedDict):
    path: str
    line: int
    heading: str
    command: str
    title_resolved: bool
    uncertainty: list[str]


class _AbstractCount(TypedDict):
    path: str
    line: int
    words: int
    # Each unresolved macro contributes at least zero and, without expanding
    # TeX, at most one further countable word.
    word_count_range: list[int]
    unresolved_macros: int
    nonempty_literal_text: bool
    content_uncertainty: list[str]
    macro_uncertainty: list[str]
    uncertainty: list[str]


class _KeywordCount(TypedDict):
    path: str
    line: int
    count: int
    uncertainty: list[str]


@dataclass(frozen=True)
class _Block:
    path: str
    line: int
    value: str
    uncertain: tuple[str, ...] = ()


def _blocks(
    name: str, source: ParsedSource, kind: _BlockKind, conditionals: list[tuple[int, int]]
) -> list[_Block]:
    """Abstract or keyword declarations of one source: commands and local environments."""
    environments = {"abstract"} if kind == "abstract" else KEYWORD_ENVIRONMENTS
    result: list[_Block] = []
    pending: dict[str, list[Command]] = {}
    nested: set[int] = set()
    active_count = 0

    def reasons(command: Command) -> tuple[str, ...]:
        return tuple(
            message
            for enabled, message in (
                (
                    in_conditional(conditionals, command.start),
                    "conditional source control flow",
                ),
                (bool(command.depth), "declaration inside a group or macro argument"),
                (command.start in nested, "nested manuscript environments"),
            )
            if enabled
        )

    for command in commands_before_end(source):
        if command.name == kind:
            value = command.arguments[0].value if command.arguments else ""
            uncertainty = reasons(command)
            if not command.arguments:
                uncertainty += ("missing literal argument",)
            result.append(_Block(name, command.line, value, uncertainty))
        elif command.name in {"begin", "end"} and command.arguments:
            environment = command.arguments[0].value.strip()
            if environment not in environments:
                continue
            if command.name == "begin":
                if active_count:
                    if active_count == 1:
                        nested.add(next(stack[0].start for stack in pending.values() if stack))
                    nested.add(command.start)
                pending.setdefault(environment, []).append(command)
                active_count += 1
            elif pending.get(environment):
                opening = pending[environment].pop()
                active_count -= 1
                # Do not copy overlapping ambiguous bodies: even maliciously
                # nested input keeps memory proportional to the bounded source.
                value = (
                    ""
                    if opening.start in nested
                    else source.masked[opening.arguments[0].end + 1 : command.start]
                )
                result.append(_Block(name, opening.line, value, reasons(opening)))
    for stack in pending.values():
        for command in stack:
            result.append(
                _Block(
                    name,
                    command.line,
                    "",
                    (*reasons(command), "environment has no local closing delimiter"),
                )
            )
    return sorted(result, key=lambda item: item.line)


def _graph_reasons(graph: SourceGraph, selected: set[str], content: set[str]) -> list[str]:
    """Why the selected graph as a whole cannot be read as one literal manuscript."""
    reasons: list[str] = []
    if graph.main not in selected:
        reasons.append("selected main source could not be inspected")
    if graph.subfile_targets:
        reasons.append(
            "subfile body/preamble content interpretation is not supported by this check"
        )
    occurrences = Counter(
        target
        for (name, _, _), (target, kind) in graph.references.items()
        if name in content and kind in CONTENT_INPUT_COMMANDS
    )
    for name, count in occurrences.items():
        if count > 1:
            reasons.append(f"{name}: repeated source inclusion is not expanded for counts")
    scope = (
        selected
        | {graph.main}
        | {target for (name, _, _), (target, _) in graph.references.items() if name in selected}
    )
    reasons.extend(
        f"{item.path}: {item.message}"
        for item in graph.findings
        if item.path in scope
        and item.rule in _INCOMPLETE_SOURCE_RULES
        and item.status != "passed"
        and not unresolved_non_content_file(item)
    )
    return reasons


@dataclass
class _ManuscriptScan:
    """Literal declarations collected source by source from the selected graph."""

    graph_reasons: list[str]
    classes: list[Declaration] = field(default_factory=list)
    packages: list[Declaration] = field(default_factory=list)
    headings: list[_LiteralHeading] = field(default_factory=list)
    abstracts: list[_Block] = field(default_factory=list)
    keywords: list[_Block] = field(default_factory=list)

    def read(self, name: str, source: ParsedSource, *, content: bool) -> None:
        """Collect one source; only content sources supply abstracts, keywords and headings."""
        # Only primitive \if…\fi ranges hide declarations. A \newif-defined name or a
        # branch-taking package conditional executes nothing by itself.
        conditionals = conditional_spans(source.commands)
        for command in commands_before_end(source):
            conditional = in_conditional(conditionals, command.start)
            if command.name == "endinput":
                self._end_of_input(name, source, command, conditional)
            if command.name == "documentclass" or command.name in PACKAGE_COMMANDS:
                self._declaration(name, command, conditional)
            if content:
                self._content_command(name, command, conditional)
        if content:
            self.abstracts.extend(_blocks(name, source, "abstract", conditionals))
            self.keywords.extend(_blocks(name, source, "keywords", conditionals))

    def _end_of_input(
        self, name: str, source: ParsedSource, command: Command, conditional: bool
    ) -> None:
        if command.depth or conditional:
            self.graph_reasons.append(
                f"{name}:{command.line}: grouped, macro-defined, or conditional "
                "\\endinput has an unresolved execution context"
            )
            return
        suffix = source.masked[command.start + len("\\endinput") :].split("\n", 1)[0]
        if suffix.strip():
            self.graph_reasons.append(
                f"{name}:{command.line}: tokens after \\endinput on the same "
                "physical line have unresolved execution semantics"
            )

    def _declaration(self, name: str, command: Command, conditional: bool) -> None:
        names = first_argument_names(command)
        options = (
            literal_list(command.options[0].value)
            if command.options and command.options[0].value.strip()
            else []
        )
        uncertainty = []
        if command.depth or conditional:
            uncertainty.append("macro, grouped, or conditional declaration")
        if names is None or (command.name == "documentclass" and options is None):
            uncertainty.append("nonliteral declaration or options")
        target = self.classes if command.name == "documentclass" else self.packages
        target.append(
            {
                "path": name,
                "line": command.line,
                "names": names,
                "options": options,
                "uncertainty": uncertainty,
            }
        )

    def _content_command(self, name: str, command: Command, conditional: bool) -> None:
        if command.name == "PassOptionsToClass":
            self.graph_reasons.append(
                f"{name}: class options are also passed outside the documentclass declaration"
            )
        elif command.name in _SYNTAX_CHANGING_COMMANDS:
            self.graph_reasons.append(
                f"{name}: \\{command.name} can generate or change source syntax"
            )
        elif command.name in HEADING_DEPTHS:
            title, unknown = (
                plain_text(command.arguments[0].value)
                if command.arguments
                else ("", ["missing heading argument"])
            )
            # A heading whose own title text is unresolved could carry any
            # required name, so it cannot support a "missing" claim either.
            resolved = bool(command.arguments) and not unknown
            if command.depth or conditional:
                unknown.append("macro, grouped, or conditional heading")
            self.headings.append(
                {
                    "path": name,
                    "line": command.line,
                    "heading": " ".join(title.split()),
                    "command": command.name,
                    "title_resolved": resolved,
                    "uncertainty": unknown,
                }
            )


def _abstract_count(block: _Block) -> _AbstractCount:
    text, unknown, macros = scan_literal_text(block.value)
    count = len(_WORD.findall(text))
    nonempty_text = bool(text.strip())
    content_uncertainty = []
    if not nonempty_text and _MATH_ONLY.search(block.value):
        content_uncertainty.append(
            "abstract has no literal text outside mathematics; nonempty rendered "
            "content is not established by the lexical count"
        )
    if not nonempty_text and any(
        match[1] in _CITATION_AND_REFERENCE_COMMANDS
        for match in CONTROL_SEQUENCE.finditer(block.value)
    ):
        content_uncertainty.append(
            "abstract has no literal text outside citation/reference commands"
        )
    if not nonempty_text and macros:
        content_uncertainty.append("abstract has no literal text outside unresolved macros")
    return {
        "path": block.path,
        "line": block.line,
        "words": count,
        "word_count_range": [count, count + macros],
        "unresolved_macros": macros,
        "nonempty_literal_text": nonempty_text,
        "content_uncertainty": content_uncertainty,
        "macro_uncertainty": unknown,
        "uncertainty": list(block.uncertain),
    }


def _keyword_count(block: _Block) -> _KeywordCount:
    # Semicolons, commas, and common explicit separator macros separate phrases.
    count = 0
    uncertainty = list(block.uncertain)
    for part in re.split(r"[,;]|\\(?:sep|and)\b", block.value):
        text, unknown = plain_text(part)
        count += bool(text.strip())
        uncertainty.extend(unknown)
    return {
        "path": block.path,
        "line": block.line,
        "count": count,
        "uncertainty": sorted(set(uncertainty)),
    }


# Offending names spelled out in a message; the details list every one.
_MAX_NAMED = 3


def _named(names: Iterable[str]) -> str:
    """The first few distinct names, ``a, b, c (+2 more)``, or ``none``."""
    unique = list(dict.fromkeys(names))
    if not unique:
        return "none"
    return ", ".join(unique[:_MAX_NAMED]) + more_suffix(len(unique) - _MAX_NAMED)


def _limits(minimum: int | None, maximum: int | None, noun: str) -> str:
    """The configured range in words: ``at most 250 words``, ``3 to 5 keywords``."""
    if minimum is None and maximum is None:
        return "no limit"
    if minimum is None:
        return f"at most {plural(maximum or 0, noun)}"
    if maximum is None:
        return f"at least {plural(minimum, noun)}"
    if minimum == maximum:
        return f"exactly {plural(minimum, noun)}"
    return f"{minimum} to {plural(maximum, noun)}"


@dataclass(frozen=True)
class _Evidence:
    """Everything the configured constraints are decided from."""

    main: str
    graph_reasons: list[str]
    classes: list[Declaration]
    packages: list[Declaration]
    abstracts: list[_AbstractCount]
    keywords: list[_KeywordCount]
    headings: list[_LiteralHeading]

    @property
    def class_uncertain(self) -> bool:
        return bool(
            self.graph_reasons
            or len(self.classes) != 1
            or any(item["uncertainty"] for item in self.classes)
        )

    @property
    def abstract_uncertain(self) -> bool:
        return bool(
            self.graph_reasons
            or len(self.abstracts) > 1
            or any(item["uncertainty"] for item in self.abstracts)
        )

    def reasons(self, items: Iterable[Mapping[str, object]], *keys: str) -> list[str]:
        """Graph reasons first, then each item's reasons under ``keys``, in order."""
        reasons = list(self.graph_reasons)
        for item in items:
            for key in keys:
                values = item.get(key)
                if isinstance(values, list):
                    reasons.extend(str(value) for value in values)
        return reasons

    def incomplete(self, items: Iterable[Mapping[str, object]], *keys: str) -> bool:
        """Whether the graph or a reported item records an interpretation gap under ``keys``."""
        return bool(self.reasons(items, *keys))


def _constraint_finding(
    rule: str,
    message: str,
    status: Status,
    details: dict[str, object],
    evidence: _Evidence,
    *,
    uncertainty: Sequence[str],
    incomplete: bool,
    located: Sequence[Mapping[str, object]] = (),
) -> Finding:
    """Build a constraint finding; a single located item supplies its path and line.

    ``incomplete`` states whether the reported evidence has an interpretation gap.
    """
    if status == "inconclusive":
        reason = uncertainty[0] if uncertainty else "no unique supported literal interpretation"
        message += f" Inconclusive: {reason.rstrip('.')}."
    path: object = evidence.main
    line: object = None
    if len(located) == 1:
        path, line = located[0]["path"], located[0]["line"]
    return Finding(
        rule,
        message,
        severity="info" if status == "passed" else "error",
        status=status,
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) else None,
        evidence_kind=EvidenceKind.DERIVED,
        incomplete=incomplete,
        details=details,
        # Confirmed violations use the registry's code-specific next step.
        suggestion=(
            "Resolve the ambiguous source construct described above, including input "
            "boundaries or macro-generated declarations, and repeat this check."
            if status == "inconclusive"
            else None
        ),
    )


def _inventory_finding(evidence: _Evidence, selected: set[str], content: set[str]) -> Finding:
    return Finding(
        "manuscript.inventory",
        "Collected literal manuscript structures and direct package declarations "
        "in the selected source graph.",
        severity="info",
        status="passed",
        evidence_kind=EvidenceKind.DERIVED,
        details={
            "sources": sorted(selected),
            "content_sources": sorted(content),
            "document_classes": evidence.classes,
            "package_declarations": evidence.packages,
            "abstracts": evidence.abstracts,
            "keywords": evidence.keywords,
            "headings": evidence.headings,
            "uncertainty": evidence.graph_reasons,
            "word_count_method": _COUNT_METHOD,
            "scope": "Literal source declarations only; no TeX execution "
            "or toolchain package inventory.",
        },
    )


def _document_class_finding(evidence: _Evidence, expected: str) -> Finding:
    classes = evidence.classes
    actual = classes[0]["names"] if len(classes) == 1 else None
    if evidence.class_uncertain or actual is None:
        status: Status = "inconclusive"
    else:
        status = "passed" if actual == [expected] else "failed"
    return _constraint_finding(
        "manuscript.document_class",
        f"Declared document class: {', '.join(actual) if actual else 'unavailable'}; "
        f"expected: {expected}.",
        status,
        {"expected": expected, "declarations": classes, "uncertainty": evidence.graph_reasons},
        evidence,
        uncertainty=evidence.reasons(classes, "uncertainty"),
        incomplete=evidence.incomplete(classes, "uncertainty"),
        located=classes,
    )


def _class_options_finding(evidence: _Evidence, options: ManuscriptOptions) -> Finding:
    classes = evidence.classes
    declared = classes[0]["options"] if len(classes) == 1 else None
    actual = set(declared) if isinstance(declared, list) else set()
    missing = sorted(set(options.required_class_options) - actual)
    forbidden = sorted(set(options.forbidden_class_options) & actual)
    if evidence.class_uncertain:
        status: Status = "inconclusive"
    else:
        status = "failed" if missing or forbidden else "passed"
    return _constraint_finding(
        "manuscript.class_options",
        f"Missing required class options: {_named(missing)}; "
        f"forbidden class options present: {_named(forbidden)}.",
        status,
        {
            "required": list(options.required_class_options),
            "forbidden": list(options.forbidden_class_options),
            "missing": missing,
            "present_forbidden": forbidden,
            "declarations": classes,
            "uncertainty": evidence.graph_reasons,
        },
        evidence,
        uncertainty=evidence.reasons(classes, "uncertainty"),
        incomplete=evidence.incomplete(classes, "uncertainty"),
        located=classes,
    )


def _forbidden_packages_finding(evidence: _Evidence, forbidden: tuple[str, ...]) -> Finding:
    packages = evidence.packages
    matches = [
        item
        for item in packages
        if isinstance(item["names"], list)
        and set(item["names"]) & set(forbidden)
        and not item["uncertainty"]
    ]
    uncertain = bool(evidence.graph_reasons or any(item["uncertainty"] for item in packages))
    found = [name for item in matches for name in item["names"] or [] if name in forbidden]
    return _constraint_finding(
        "manuscript.forbidden_packages",
        f"Forbidden packages declared: {_named(found)} ({format_locations(matches)})."
        if matches
        else f"No forbidden package is declared directly (forbidden: {_named(forbidden)}).",
        check_status(failed=bool(matches), incomplete=uncertain),
        {
            "forbidden": list(forbidden),
            "matches": matches,
            "declarations": packages,
            "uncertainty": evidence.graph_reasons,
            "scope": "Does not establish the transitive packages loaded by TeX.",
        },
        evidence,
        uncertainty=evidence.reasons(packages, "uncertainty"),
        incomplete=evidence.incomplete(packages, "uncertainty"),
        located=matches,
    )


_ABSTRACT_GAP_KEYS = ("uncertainty", "content_uncertainty")
# Unresolved macros only widen the counted range: they can explain an inconclusive
# count, but a range outside the limits is complete evidence of a violation.
_ABSTRACT_REASON_KEYS = (*_ABSTRACT_GAP_KEYS, "macro_uncertainty")


def _abstract_required_finding(evidence: _Evidence) -> Finding:
    abstracts = evidence.abstracts
    if evidence.abstract_uncertain or any(item["content_uncertainty"] for item in abstracts):
        status: Status = "inconclusive"
    elif len(abstracts) == 1 and abstracts[0]["nonempty_literal_text"]:
        status = "passed"
    else:
        status = "failed"
    return _constraint_finding(
        "manuscript.abstract_required",
        f"Found {plural(len(abstracts), 'supported literal abstract declaration')}; "
        "one nonempty abstract is required.",
        status,
        {"abstracts": abstracts, "uncertainty": evidence.graph_reasons},
        evidence,
        uncertainty=evidence.reasons(abstracts, *_ABSTRACT_REASON_KEYS),
        incomplete=evidence.incomplete(abstracts, *_ABSTRACT_GAP_KEYS),
        located=abstracts,
    )


def _abstract_words_finding(
    evidence: _Evidence, minimum: int | None, maximum: int | None
) -> Finding:
    abstracts = evidence.abstracts
    # Unresolved macros bound the count instead of discarding it: a range that
    # lies wholly inside or wholly outside the configured limits is decisive.
    bounds: tuple[int, int] | None = None
    if not abstracts:
        bounds = (0, 0)
    elif len(abstracts) == 1:
        lower, upper = abstracts[0]["word_count_range"]
        bounds = (lower, upper)
    status: Status = "inconclusive"
    if not evidence.abstract_uncertain and bounds is not None:
        lower, upper = bounds
        inside = (minimum is None or lower >= minimum) and (maximum is None or upper <= maximum)
        outside = (minimum is not None and upper < minimum) or (
            maximum is not None and lower > maximum
        )
        if inside:
            status = "passed"
        elif outside:
            status = "failed"
    if bounds is None:
        reported = "unavailable"
    elif bounds[0] == bounds[1]:
        reported = str(bounds[0])
    else:
        reported = f"{bounds[0]} to {bounds[1]}"
    return _constraint_finding(
        "manuscript.abstract_words",
        f"Abstract source word count: {reported}; allowed: {_limits(minimum, maximum, 'word')}.",
        status,
        {
            "minimum": minimum,
            "maximum": maximum,
            "word_count_range": list(bounds) if bounds is not None else None,
            "abstracts": abstracts,
            "counting_method": _COUNT_METHOD,
            "uncertainty": evidence.graph_reasons,
        },
        evidence,
        uncertainty=evidence.reasons(abstracts, *_ABSTRACT_REASON_KEYS),
        incomplete=evidence.incomplete(abstracts, *_ABSTRACT_GAP_KEYS),
        located=abstracts,
    )


def _keyword_count_finding(
    evidence: _Evidence, minimum: int | None, maximum: int | None
) -> Finding:
    keywords = evidence.keywords
    count = keywords[0]["count"] if len(keywords) == 1 else 0
    uncertain = bool(
        evidence.graph_reasons or len(keywords) > 1 or any(item["uncertainty"] for item in keywords)
    )
    if uncertain:
        status: Status = "inconclusive"
    elif (minimum is not None and count < minimum) or (maximum is not None and count > maximum):
        status = "failed"
    else:
        status = "passed"
    return _constraint_finding(
        "manuscript.keyword_count",
        f"Literal keyword count: {count}; allowed: {_limits(minimum, maximum, 'keyword')}.",
        status,
        {
            "minimum": minimum,
            "maximum": maximum,
            "count": count,
            "keywords": keywords,
            "counting_method": _KEYWORD_COUNT_METHOD,
            "uncertainty": evidence.graph_reasons,
        },
        evidence,
        uncertainty=evidence.reasons(keywords, "uncertainty"),
        incomplete=evidence.incomplete(keywords, "uncertainty"),
        located=keywords,
    )


def _required_sections_finding(evidence: _Evidence, required: tuple[str, ...]) -> Finding:
    headings = evidence.headings
    present = {normalize_heading(item["heading"]) for item in headings if not item["uncertainty"]}
    # A heading with an unresolved title could carry any required name, so no
    # absence claim survives it. A resolved but conditional heading only
    # prevents a claim about its own name.
    opaque = bool(evidence.graph_reasons) or any(not item["title_resolved"] for item in headings)
    unresolved_names = {
        normalize_heading(item["heading"])
        for item in headings
        if item["uncertainty"] and item["title_resolved"]
    }
    missing: list[str] = []
    unconfirmed: list[str] = []
    for heading in required:
        key = normalize_heading(heading)
        if key in present:
            continue
        if opaque or key in unresolved_names:
            unconfirmed.append(heading)
        else:
            missing.append(heading)
    uncertain = bool(
        unconfirmed or evidence.graph_reasons or any(item["uncertainty"] for item in headings)
    )
    if missing:
        message = f"Missing required literal headings: {', '.join(missing)}"
        if unconfirmed:
            message += f"; could not confirm: {', '.join(unconfirmed)}"
    elif unconfirmed:
        message = f"Could not confirm required literal headings: {', '.join(unconfirmed)}"
    elif uncertain:
        message = "Required literal headings matched an incompletely interpreted source"
    else:
        message = "All required literal headings are present"
    return _constraint_finding(
        "manuscript.required_sections",
        message + "; content adequacy is not assessed.",
        check_status(failed=bool(missing), incomplete=uncertain),
        {
            "required": list(required),
            "missing": missing,
            "unconfirmed": unconfirmed,
            "headings": headings,
            "matching": "Case-insensitive, collapsed whitespace, "
            "common literal formatting wrappers removed.",
            "uncertainty": evidence.graph_reasons,
        },
        evidence,
        uncertainty=evidence.reasons(headings, "uncertainty"),
        incomplete=evidence.incomplete(headings, "uncertainty"),
    )


def check_manuscript(root: Path, main: str, options: ManuscriptOptions) -> list[Finding]:
    """Check explicit constraints; only the selected graph supplies manuscript evidence."""
    if options == ManuscriptOptions():
        return []
    graph = SourceGraph.build(root, main)
    selected = reachable_sources(graph)
    # Content follows input/include/import edges. Definitions inside class/style files are
    # package evidence, not manuscript abstracts, keywords, or section headings.
    content = reachable_sources(graph, CONTENT_INPUT_COMMANDS)
    scan = _ManuscriptScan(_graph_reasons(graph, selected, content))
    for name in sorted(selected):
        cancellation_point()
        scan.read(name, graph.sources[name], content=name in content)
    evidence = _Evidence(
        main=main,
        graph_reasons=sorted(set(scan.graph_reasons)),
        classes=scan.classes,
        packages=scan.packages,
        abstracts=[_abstract_count(block) for block in scan.abstracts],
        keywords=[_keyword_count(block) for block in scan.keywords],
        headings=scan.headings,
    )
    findings = [_inventory_finding(evidence, selected, content)]
    if options.expected_document_class is not None:
        findings.append(_document_class_finding(evidence, options.expected_document_class))
    if options.required_class_options or options.forbidden_class_options:
        findings.append(_class_options_finding(evidence, options))
    if options.forbidden_packages:
        findings.append(_forbidden_packages_finding(evidence, options.forbidden_packages))
    if options.require_abstract:
        findings.append(_abstract_required_finding(evidence))
    if options.abstract_min_words is not None or options.abstract_max_words is not None:
        findings.append(
            _abstract_words_finding(
                evidence, options.abstract_min_words, options.abstract_max_words
            )
        )
    if options.keywords_min_count is not None or options.keywords_max_count is not None:
        findings.append(
            _keyword_count_finding(evidence, options.keywords_min_count, options.keywords_max_count)
        )
    if options.required_sections:
        findings.append(_required_sections_finding(evidence, options.required_sections))
    return findings
