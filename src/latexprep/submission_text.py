"""Bounded, source-only submission fields; unresolved content remains visible."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple, get_args

from .expanded_document import (
    ExpandedDocument,
    environment_regions,
    expand_document,
)
from .literal_text import KEYWORD_ENVIRONMENTS, TEXT_WRAPPERS
from .tex_commands import MACRO_DEFINITION_COMMANDS, PRIMITIVE_DEFINITION_COMMANDS
from .tex_lexer import (
    MAX_ARGUMENT_CHARS,
    Argument,
    definition_spans,
    in_conditional,
    in_half_open_span,
    merge_spans,
    read_group,
    skip_space,
)
from .text_safety import escape_single_line_text, is_unsafe_character

FieldName = Literal["title", "abstract", "keywords"]
FIELD_NAMES: tuple[FieldName, ...] = get_args(FieldName)
FieldStatus = Literal["ready", "empty", "missing", "unresolved"]

# Document-text grammar, unlike the lexer's package-code CONTROL_SEQUENCE: "@" is
# not a letter, and a control symbol may be any character, including a line break.
_TEXT_CONTROL_SEQUENCE = re.compile(r"\\([A-Za-z]+|.)", re.S)
_ACCENTS = {
    "'": "́",
    "`": "̀",
    "^": "̂",
    '"': "̈",
    "~": "̃",
    "=": "̄",
    ".": "̇",
    "c": "̧",
    "v": "̌",
    "u": "̆",
    "H": "̋",
    "r": "̊",
}
_SYMBOLS = {
    "LaTeX": "LaTeX",
    "TeX": "TeX",
    "ae": "æ",
    "AE": "Æ",
    "oe": "œ",
    "OE": "Œ",
    "aa": "å",
    "AA": "Å",
    "o": "ø",
    "O": "Ø",
    "ss": "ß",
    "i": "ı",
    "j": "ȷ",
    "textendash": "–",
    "textemdash": "—",
}
_GREEK = dict(
    zip(
        "alpha beta gamma delta epsilon theta lambda mu pi rho sigma tau phi psi omega".split(),
        "α β γ δ ε θ λ μ π ρ σ τ φ ψ ω".split(),
        strict=True,
    )
)
# Inline mathematics that reads as plain text: no scripts, fractions or macros.
_PLAIN_MATH = re.compile(r"[A-Za-z0-9α-ω\s=+−.,()<>/\-]+")
_VERBATIM_URL_COMMANDS = frozenset({"url", "nolinkurl", "path"})
# Wrappers that restore ordinary punctuation inside typewriter text.
_ROMAN_WRAPPERS = frozenset({"textrm", "textsf", "textnormal"})
_LIGATURES = (("---", "—"), ("--", "–"), ("``", "“"), ("''", "”"))
# Line breaks take an optional bracketed length or priority; other spacing does not,
# so a bracket after them is ordinary text.
_LINE_BREAK_COMMANDS = frozenset({"\\", "linebreak"})
_MAX_GROUP_DEPTH = 32


@dataclass(frozen=True, slots=True)
class SubmissionField:
    """One field's copy-ready draft, or why it cannot be exported as is."""

    status: FieldStatus
    draft: str = ""
    locations: list[dict[str, object]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ConversionContext:
    """What holds for one field and for the groups nested inside it."""

    keyword_separators: bool = False
    redefined_commands: frozenset[str] = frozenset()
    # Typewriter text keeps "--" and quote pairs as typed.
    literal_punctuation: bool = False
    depth: int = 0

    def nested(self, *, literal_punctuation: bool) -> _ConversionContext:
        return replace(self, depth=self.depth + 1, literal_punctuation=literal_punctuation)


class _ControlWord(NamedTuple):
    """A control sequence in the field and the first group that follows it."""

    start: int
    name: str
    # Offset just after the name, and after any spaces that follow it.
    stop: int
    cursor: int
    argument: Argument | None


def _mask_comments(value: str) -> str:
    """Blank comments so their braces do not delimit groups, keeping every offset."""
    masked = list(value)
    cursor = 0
    while cursor < len(value):
        if value[cursor] == "\\":
            token = _TEXT_CONTROL_SEQUENCE.match(value, cursor)
            if token and token.group(1) in _VERBATIM_URL_COMMANDS:
                literal = read_group(value, skip_space(value, token.end()))
                if literal is not None:
                    cursor = literal.end + 1
                    continue
            cursor += 2
        elif value[cursor] == "%":
            end = value.find("\n", cursor)
            end = len(value) if end < 0 else end
            masked[cursor:end] = " " * (end - cursor)
            cursor = end
        else:
            cursor += 1
    return "".join(masked)


_Handler = Callable[["_Converter", _ControlWord], int]


def _handler_table(*groups: tuple[Iterable[str], _Handler]) -> dict[str, _Handler]:
    """Map every command name to its handler; a name in two groups is a programming error."""
    table: dict[str, _Handler] = {}
    for names, handler in groups:
        for name in names:
            if name in table:
                raise RuntimeError(f"Command \\{name} is assigned to more than one text handler")
            table[name] = handler
    return table


class _Converter:
    """Converts one field or group; unknown constructs are kept verbatim and reported."""

    def __init__(self, value: str, context: _ConversionContext) -> None:
        self.value = value
        self.context = context
        self.out: list[str] = []
        self.issues: list[str] = []
        self._grouped = value

    def convert(self) -> tuple[str, list[str]]:
        value = self.value
        if self.context.depth > _MAX_GROUP_DEPTH or len(value) > MAX_ARGUMENT_CHARS:
            return escape_single_line_text(value), ["Text exceeds bounded conversion limits"]
        # The draft shows these characters as escapes, so the export is never copy-ready.
        if any(is_unsafe_character(char) and char not in "\n\r\t" for char in value):
            self.issues.append(
                "Unsupported control, directional, private-use or unassigned character "
                "needs manual review"
            )
        # Payloads keep their comments for conversion; only group matching skips them.
        self._grouped = _mask_comments(value)
        position = 0
        while position < len(value):
            position = self._step(position)
        draft = unicodedata.normalize("NFC", " ".join("".join(self.out).split()))
        return escape_single_line_text(draft), sorted(
            {escape_single_line_text(issue) for issue in self.issues}
        )

    def _group(self, start: int, opening: str = "{", closing: str = "}") -> Argument | None:
        argument = read_group(self._grouped, start, opening, closing)
        if argument is not None:
            return replace(argument, value=self.value[argument.start : argument.end])
        return None

    def _step(self, position: int) -> int:
        match self.value[position]:
            case "%":
                return self._comment(position)
            case "$":
                return self._math(position)
            case "\\":
                return self._control_sequence(position)
            case "{":
                argument = self._group(position)
                if argument is not None:
                    return self._nested(
                        argument, literal_punctuation=self.context.literal_punctuation
                    )
                self.issues.append("Unclosed group")
            case "}":
                self.issues.append("Unmatched closing brace")
        return self._character(position)

    def _character(self, position: int) -> int:
        char = self.value[position]
        if char == "~":
            self.out.append(" ")
            return position + 1
        if not self.context.literal_punctuation:
            for ligature, replacement in _LIGATURES:
                if self.value.startswith(ligature, position):
                    self.out.append(replacement)
                    return position + len(ligature)
        self.out.append(char)
        return position + 1

    def _comment(self, position: int) -> int:
        value = self.value
        end = value.find("\n", position)
        position = len(value) if end < 0 else end + 1
        while position < len(value) and value[position] in " \t":
            position += 1
        return position

    def _math(self, position: int) -> int:
        value = self.value
        end = position + 1
        while end < len(value) and (value[end] != "$" or value[end - 1] == "\\"):
            end += 1
        fragment = value[position : min(end + 1, len(value))]
        math = value[position + 1 : end]
        for name, symbol in _GREEK.items():
            if name not in self.context.redefined_commands:
                math = re.sub(r"\\" + name + r"\b", symbol, math)
        # No scripts, fractions, macros, displays, or implicit symbol substitutions.
        if end < len(value) and math.strip() and _PLAIN_MATH.fullmatch(math):
            self.out.append(math)
        else:
            self.out.append(fragment)
            self.issues.append("Mathematics needs manual conversion: " + fragment)
        return min(end + 1, len(value))

    def _nested(self, argument: Argument, *, literal_punctuation: bool) -> int:
        context = self.context.nested(literal_punctuation=literal_punctuation)
        text, issues = _Converter(argument.value, context).convert()
        self.out.append(text)
        self.issues.extend(issues)
        return argument.end + 1

    def _control_sequence(self, position: int) -> int:
        token = _TEXT_CONTROL_SEQUENCE.match(self.value, position)
        if token is None:
            self.out.append("\\")
            self.issues.append("Trailing backslash")
            return position + 1
        stop = token.end()
        cursor = skip_space(self.value, stop)
        word = _ControlWord(position, token.group(1), stop, cursor, self._group(cursor))
        if word.name in self.context.redefined_commands:
            return self._redefined(word)
        handler = self._HANDLERS.get(word.name)
        if handler is None:
            return self._unsupported(word, self._after_argument(word))
        return handler(self, word)

    def _after_argument(self, word: _ControlWord) -> int:
        return word.argument.end + 1 if word.argument is not None else word.stop

    def _unsupported(self, word: _ControlWord, end: int) -> int:
        # Preserve an unknown fragment whole so draft text never invents its meaning.
        self.out.append(self.value[word.start : end])
        self.issues.append("Unsupported command: \\" + word.name)
        return end

    def _redefined(self, word: _ControlWord) -> int:
        end = self._after_argument(word)
        if word.name == "href" and word.argument is not None:
            second = self._group(skip_space(self.value, end))
            if second is not None:
                end = second.end + 1
        self.out.append(self.value[word.start : end])
        self.issues.append("Locally defined command needs manual review: \\" + word.name)
        return end

    def _wrapper(self, word: _ControlWord) -> int:
        argument = word.argument
        if word.name == "href" and argument is not None:
            argument = self._group(skip_space(self.value, argument.end + 1))
        if argument is None:
            return self._unsupported(word, word.stop)
        literal_punctuation = word.name not in _ROMAN_WRAPPERS and (
            self.context.literal_punctuation or word.name == "texttt"
        )
        return self._nested(argument, literal_punctuation=literal_punctuation)

    def _url(self, word: _ControlWord) -> int:
        if word.argument is None:
            return self._unsupported(word, word.stop)
        self.out.append(word.argument.value)
        return word.argument.end + 1

    def _accent(self, word: _ControlWord) -> int:
        argument = word.argument
        letter = (
            argument.value if argument is not None else self.value[word.cursor : word.cursor + 1]
        )
        if letter in {r"\i", r"\j"} and letter[1:] not in self.context.redefined_commands:
            letter = letter[1:]
        if len(letter) == 1 and letter.isalpha():
            self.out.append(unicodedata.normalize("NFC", letter + _ACCENTS[word.name]))
            return argument.end + 1 if argument is not None else word.cursor + 1
        return self._unsupported(word, self._after_argument(word))

    def _symbol(self, word: _ControlWord) -> int:
        self.out.append(_SYMBOLS[word.name])
        return word.cursor

    def _escaped_character(self, word: _ControlWord) -> int:
        self.out.append(word.name)
        return word.stop

    def _space(self, word: _ControlWord) -> int:
        cursor = word.cursor
        if word.name in _LINE_BREAK_COMMANDS and self.value[cursor : cursor + 1] == "[":
            option = self._group(cursor, "[", "]")
            if option is not None:
                cursor = option.end + 1
        separator = self.context.keyword_separators and word.name in {"sep", "and"}
        self.out.append("; " if separator else " ")
        return cursor

    def _font_switch(self, word: _ControlWord) -> int:
        if word.name == "normalfont":
            # Recursive group conversion keeps this font reset local to
            # its enclosing group, just like the typewriter wrapper.
            self.context = replace(self.context, literal_punctuation=False)
        return word.cursor

    # The command groups are disjoint, so each name has exactly one handler.
    _HANDLERS: ClassVar[dict[str, _Handler]] = _handler_table(
        (TEXT_WRAPPERS | {"text", "href"}, _wrapper),
        (_VERBATIM_URL_COMMANDS, _url),
        (_ACCENTS, _accent),
        (_SYMBOLS, _symbol),
        ("%&#_{}$", _escaped_character),
        (("par", "newline", "linebreak", "\\", " ", ",", ";", "sep", "and"), _space),
        (("em", "itshape", "bfseries", "normalfont", "protect"), _font_switch),
    )


def convert_text(
    value: str,
    *,
    keyword_separators: bool = False,
    redefined_commands: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Convert a deliberately small grammar without executing or dropping unknown TeX."""
    context = _ConversionContext(keyword_separators, redefined_commands)
    return _Converter(value, context).convert()


class _Declaration(NamedTuple):
    """One source declaration of a field: its command offset and raw text span."""

    position: int
    start: int
    end: int
    reasons: list[str]


def _local_definitions(document: ExpandedDocument) -> tuple[frozenset[str], frozenset[str]]:
    """Commands/environments and active characters that the project itself defines.

    A supported spelling does not establish meaning when the local project defines
    it. Conditional or order-dependent activation is not inferred.
    """
    redefined: set[str] = set()
    active_characters: set[str] = set()
    for path in document.selected:
        source = document.graph.sources.get(path)
        if source is None:
            continue
        for command in source.commands:
            if command.name not in MACRO_DEFINITION_COMMANDS:
                continue
            if command.arguments and command.name not in PRIMITIVE_DEFINITION_COMMANDS:
                target = command.arguments[0].value.strip()
            else:
                target = source.masked[command.name_end :].lstrip()
            token = _TEXT_CONTROL_SEQUENCE.match(target)
            if token is not None:
                redefined.add(token.group(1))
            elif target and command.name in PRIMITIVE_DEFINITION_COMMANDS:
                active_characters.add(target[0])
            elif "environment" in command.name.casefold() and re.fullmatch(r"[A-Za-z]+", target):
                redefined.add(target)
    return frozenset(redefined), frozenset(active_characters)


def _original_text(document: ExpandedDocument) -> str:
    """The raw source behind the masked expanded text, at the same offsets."""
    return "".join(
        document.graph.sources[segment.path].text[
            segment.source_start : segment.source_start + segment.end - segment.start
        ]
        for segment in document.segments
    )


def _field_definition_spans(document: ExpandedDocument) -> list[tuple[int, int]]:
    """Definition bodies, with literal let assignments delimited exactly.

    The general definition span can conservatively consume a later group on the
    same line after a primitive assignment. For field extraction, delimit literal
    let assignments so following declarations remain visible.
    """
    text = document.text
    spans = definition_spans(
        text, [command for command in document.commands if command.name not in {"let", "futurelet"}]
    )
    for command in document.commands:
        if command.name not in {"let", "futurelet"}:
            continue
        cursor = command.name_end
        for _ in range(3 if command.name == "futurelet" else 2):
            cursor = skip_space(text, cursor)
            if text[cursor : cursor + 1] == "=":
                cursor = skip_space(text, cursor + 1)
            token = _TEXT_CONTROL_SEQUENCE.match(text, cursor)
            cursor = token.end() if token is not None else cursor + 1
        spans.append((command.start, cursor))
    return merge_spans(spans)


def _declarations(document: ExpandedDocument) -> dict[str, list[_Declaration]]:
    """Command and environment declarations of each field, outside definition bodies."""
    definitions = _field_definition_spans(document)
    declarations: dict[str, list[_Declaration]] = {name: [] for name in FIELD_NAMES}
    for command in document.commands:
        if command.name not in declarations or in_half_open_span(definitions, command.start):
            continue
        reasons = []
        if command.depth:
            reasons.append("Declaration inside a group or macro argument")
        if in_conditional(document.conditionals, command.start):
            reasons.append("Conditional declaration")
        argument = command.arguments[0] if command.arguments else None
        if argument is None:
            reasons.append("Missing literal argument")
            span = (command.start, command.start)
        else:
            span = (argument.start, argument.end)
        declarations[command.name].append(_Declaration(command.start, *span, reasons))
    for name, environments in (("abstract", {"abstract"}), ("keywords", KEYWORD_ENVIRONMENTS)):
        regions, reasons = environment_regions(document, environments)
        for region in regions:
            if in_half_open_span(definitions, region.command.start):
                continue
            # The lexer records every following group as a possible argument.
            # An environment name consumes only its first group; preserve any
            # ordinary grouped prose immediately following it.
            start = region.command.arguments[0].end + 1
            declarations[name].append(
                _Declaration(
                    region.command.start, start, region.end, [*reasons, *region.uncertainty]
                )
            )
    return declarations


def _field_status(declarations: int, issues: list[str], draft: str) -> FieldStatus:
    if not declarations:
        return "unresolved" if issues else "missing"
    if declarations != 1 or issues:
        return "unresolved"
    return "ready" if draft else "empty"


@dataclass(frozen=True, slots=True)
class _FieldReader:
    """What every field draws on: the document, its raw text and local definitions."""

    document: ExpandedDocument
    original: str
    shared_issues: list[str]
    redefined_commands: frozenset[str]
    active_characters: frozenset[str]

    def convert(self, name: str, declaration: _Declaration) -> tuple[str, list[str]]:
        raw_text = self.original[declaration.start : declaration.end]
        used_active = self.active_characters.intersection(raw_text)
        if used_active:
            # Active characters can change tokenization and even punctuation
            # inside math. Preserve the field instead of guessing where the
            # locally assigned meaning is in force.
            return escape_single_line_text(" ".join(raw_text.split())), [
                "Locally defined active character needs manual review: " + char
                for char in sorted(used_active)
            ]
        return convert_text(
            raw_text,
            keyword_separators=name == "keywords",
            redefined_commands=self.redefined_commands,
        )

    def read_field(self, name: str, declarations: list[_Declaration]) -> SubmissionField:
        issues = list(self.shared_issues)
        drafts = []
        locations: list[dict[str, object]] = []
        for declaration in declarations:
            draft, unknown = self.convert(name, declaration)
            if name in self.redefined_commands:
                issues.append("Locally defined declaration needs manual review: " + name)
            drafts.append(draft)
            location = self.document.location(declaration.position)
            path = escape_single_line_text(location.path) if location.path is not None else None
            locations.append({"path": path, "line": location.line})
            issues.extend([*declaration.reasons, *unknown])
        if len(declarations) > 1:
            issues.append("Multiple declarations require explicit source clarification")
        draft = drafts[0] if len(drafts) == 1 else ""
        return SubmissionField(
            _field_status(len(declarations), issues, draft),
            draft,
            locations,
            sorted({escape_single_line_text(issue) for issue in issues}),
            drafts if len(drafts) > 1 else [],
        )


def read_submission_text(root: Path, main: str | None = None) -> dict[str, SubmissionField]:
    """Read unique declarations, preserving raw source behind the shared masked lexer."""
    document = expand_document(root, main)
    redefined, active_characters = _local_definitions(document)
    reader = _FieldReader(
        document,
        _original_text(document),
        list(document.presence_uncertainty),
        redefined,
        active_characters,
    )
    declarations = _declarations(document)
    return {name: reader.read_field(name, declarations[name]) for name in FIELD_NAMES}
