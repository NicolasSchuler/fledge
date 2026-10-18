"""Literal declarations collected from the selected source graph, and the counts made of them.

``check_manuscript`` decides its constraints from what this module reads: the document
class and package declarations, the headings, and the abstract and keyword blocks of
every source that contributes manuscript content. The scan is lexical and never executes
TeX. Anything it cannot interpret becomes a reason attached to the declaration it
affects, or a reason for the graph as a whole (``graph_reasons``); no reason ever
becomes a verdict here. Counting follows the convention stated in ``COUNT_METHOD``.
This module owns the evidence, not the constraints applied to it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from .expanded_document import (
    CONTENT_INPUT_COMMANDS,
    commands_before_end,
    unresolved_non_content_file,
)
from .literal_text import (
    HEADING_DEPTHS,
    KEY_ARGUMENT_COMMANDS,
    KEYWORD_ENVIRONMENTS,
    PACKAGE_COMMANDS,
    Declaration,
    first_argument_names,
    literal_list,
    plain_text,
    scan_literal_text,
)
from .source_graph import INCOMPLETE_GRAPH_RULES, SourceGraph
from .source_loading import ParsedSource
from .tex_commands import MATH_MARKER
from .tex_lexer import CONTROL_SEQUENCE, Command, conditional_spans, in_conditional
from .uncertainty_reasons import context_reasons

COUNT_METHOD = (
    "Unicode alphanumeric tokens; internal hyphens and apostrophes stay in one word. "
    "Comments, common literal/code regions, math, and citation/reference/label arguments "
    "count as zero. Common text-formatting wrappers retain their text. Bibliography, "
    "layout and other known non-content commands count as zero without an interpretation "
    "gap. Each unresolved macro widens the reported range by one word, so only a range "
    "that lies wholly inside or outside the limits is decisive. This is a source count, "
    "not a rendered word count."
)
KEYWORD_COUNT_METHOD = (
    "Nonempty phrases separated by commas, semicolons, \\sep, or \\and in \\keywords{} "
    "or keywords/keyword/IEEEkeywords environments."
)
_WORD = re.compile(r"[^\W_]+(?:['’\-‑][^\W_]+)*")
_KEYWORD_SEPARATOR = re.compile(r"[,;]|\\(?:sep|and)\b")
_CITATION_AND_REFERENCE_COMMANDS = KEY_ARGUMENT_COMMANDS - {"label", "nocite"}
_SYNTAX_CHANGING_COMMANDS = {"csname", "catcode", "scantokens"}

_BlockKind = Literal["abstract", "keywords"]


@dataclass(frozen=True, slots=True)
class LiteralHeading:
    """One literal sectioning command with its resolved title text."""

    path: str
    line: int
    heading: str
    command: str
    # False when the title itself holds unresolved text, so it could carry any name.
    title_resolved: bool
    uncertainty: list[str]

    def as_dict(self) -> dict[str, object]:
        """The heading as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "heading": self.heading,
            "command": self.command,
            "title_resolved": self.title_resolved,
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True, slots=True)
class AbstractCount:
    """The source word count of one abstract, as a range, with what is unclear about it."""

    path: str
    line: int
    words: int
    # Each unresolved macro contributes at least zero and, without expanding
    # TeX, at most one further countable word.
    word_count_range: tuple[int, int]
    unresolved_macros: int
    nonempty_literal_text: bool
    content_uncertainty: list[str]
    macro_uncertainty: list[str]
    uncertainty: list[str]

    @property
    def gaps(self) -> list[str]:
        """Reasons the abstract's presence or content cannot be established."""
        return [*self.uncertainty, *self.content_uncertainty]

    @property
    def reasons(self) -> list[str]:
        """The gaps plus unresolved macros, which only widen the counted range."""
        return [*self.gaps, *self.macro_uncertainty]

    def as_dict(self) -> dict[str, object]:
        """The count as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "words": self.words,
            "word_count_range": list(self.word_count_range),
            "unresolved_macros": self.unresolved_macros,
            "nonempty_literal_text": self.nonempty_literal_text,
            "content_uncertainty": self.content_uncertainty,
            "macro_uncertainty": self.macro_uncertainty,
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True, slots=True)
class KeywordCount:
    """The number of keyword phrases in one keyword block."""

    path: str
    line: int
    count: int
    uncertainty: list[str]

    def as_dict(self) -> dict[str, object]:
        """The count as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "count": self.count,
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True)
class Block:
    """The raw text of one abstract or keyword declaration."""

    path: str
    line: int
    value: str
    uncertain: tuple[str, ...] = ()


class _BlockReader:
    """Abstract or keyword declarations of one source: commands and local environments."""

    def __init__(
        self, name: str, source: ParsedSource, kind: _BlockKind, conditionals: list[tuple[int, int]]
    ) -> None:
        self.name = name
        self.source = source
        self.kind = kind
        self.conditionals = conditionals
        self.environments = {"abstract"} if kind == "abstract" else KEYWORD_ENVIRONMENTS
        self.result: list[Block] = []
        self.pending: dict[str, list[Command]] = {}
        # Starts of the environment openers that overlap another one of the same kind.
        self.nested: set[int] = set()
        self.active_count = 0

    def read(self) -> list[Block]:
        """The blocks in source order; an opener without a local closer is an unclear block."""
        for command in commands_before_end(self.source):
            if command.name == self.kind:
                self._command_block(command)
            elif command.name in {"begin", "end"} and command.arguments:
                environment = command.arguments[0].value.strip()
                if environment in self.environments:
                    if command.name == "begin":
                        self._open(command, environment)
                    else:
                        self._close(command, environment)
        for stack in self.pending.values():
            for command in stack:
                reasons = (*self._reasons(command), "environment has no local closing delimiter")
                self.result.append(Block(self.name, command.line, "", reasons))
        return sorted(self.result, key=lambda item: item.line)

    def _reasons(self, command: Command) -> tuple[str, ...]:
        found = context_reasons(command, self.conditionals, "declaration")
        if command.start in self.nested:
            found.append("nested manuscript environments")
        return tuple(found)

    def _command_block(self, command: Command) -> None:
        value = command.arguments[0].value if command.arguments else ""
        uncertainty = self._reasons(command)
        if not command.arguments:
            uncertainty += ("missing literal argument",)
        self.result.append(Block(self.name, command.line, value, uncertainty))

    def _open(self, command: Command, environment: str) -> None:
        if self.active_count:
            if self.active_count == 1:
                self.nested.add(next(stack[0].start for stack in self.pending.values() if stack))
            self.nested.add(command.start)
        self.pending.setdefault(environment, []).append(command)
        self.active_count += 1

    def _close(self, command: Command, environment: str) -> None:
        if not self.pending.get(environment):
            return
        opening = self.pending[environment].pop()
        self.active_count -= 1
        # Do not copy overlapping ambiguous bodies: even maliciously
        # nested input keeps memory proportional to the bounded source.
        value = (
            ""
            if opening.start in self.nested
            else self.source.masked[opening.arguments[0].end + 1 : command.start]
        )
        self.result.append(Block(self.name, opening.line, value, self._reasons(opening)))


def graph_reasons(graph: SourceGraph, selected: set[str], content: set[str]) -> list[str]:
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
        and item.rule in INCOMPLETE_GRAPH_RULES
        and item.status != "passed"
        and not unresolved_non_content_file(item)
    )
    return reasons


@dataclass
class ManuscriptScan:
    """Literal declarations collected source by source from the selected graph."""

    graph_reasons: list[str]
    classes: list[Declaration] = field(default_factory=list)
    packages: list[Declaration] = field(default_factory=list)
    headings: list[LiteralHeading] = field(default_factory=list)
    abstracts: list[Block] = field(default_factory=list)
    keywords: list[Block] = field(default_factory=list)

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
                self._declaration(name, command, conditionals)
            if content:
                self._content_command(name, command, conditionals)
        if content:
            self.abstracts.extend(_BlockReader(name, source, "abstract", conditionals).read())
            self.keywords.extend(_BlockReader(name, source, "keywords", conditionals).read())

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

    def _declaration(
        self, name: str, command: Command, conditionals: list[tuple[int, int]]
    ) -> None:
        names = first_argument_names(command)
        options = (
            literal_list(command.options[0].value)
            if command.options and command.options[0].value.strip()
            else []
        )
        uncertainty = context_reasons(command, conditionals, "declaration")
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

    def _content_command(
        self, name: str, command: Command, conditionals: list[tuple[int, int]]
    ) -> None:
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
            unknown.extend(context_reasons(command, conditionals, "heading"))
            self.headings.append(
                LiteralHeading(
                    name, command.line, " ".join(title.split()), command.name, resolved, unknown
                )
            )


def abstract_count(block: Block) -> AbstractCount:
    """The word count range of one abstract block, and why its content may be unclear."""
    text, unknown, macros = scan_literal_text(block.value)
    count = len(_WORD.findall(text))
    nonempty_text = bool(text.strip())
    content_uncertainty = []
    if not nonempty_text and MATH_MARKER.search(block.value):
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
    return AbstractCount(
        path=block.path,
        line=block.line,
        words=count,
        word_count_range=(count, count + macros),
        unresolved_macros=macros,
        nonempty_literal_text=nonempty_text,
        content_uncertainty=content_uncertainty,
        macro_uncertainty=unknown,
        uncertainty=list(block.uncertain),
    )


def keyword_count(block: Block) -> KeywordCount:
    """The number of nonempty keyword phrases in one keyword block."""
    # Semicolons, commas, and common explicit separator macros separate phrases.
    count = 0
    uncertainty = list(block.uncertain)
    for part in _KEYWORD_SEPARATOR.split(block.value):
        text, unknown = plain_text(part)
        count += bool(text.strip())
        uncertainty.extend(unknown)
    return KeywordCount(block.path, block.line, count, sorted(set(uncertainty)))
