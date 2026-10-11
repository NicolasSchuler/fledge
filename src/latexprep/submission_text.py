"""Bounded, source-only submission fields; unresolved content remains visible."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict, cast

from .manuscript import _TEXT_WRAPPERS
from .manuscript_checks import _DEFINITIONS, _definition_spans, _read, _regions, _within
from .source import MAX_ARGUMENT_CHARS, _group, in_conditional

_TOKEN = re.compile(r"\\([A-Za-z]+|.)", re.S)
_ACCENTS = {
    "'": "\u0301",
    "`": "\u0300",
    "^": "\u0302",
    '"': "\u0308",
    "~": "\u0303",
    "=": "\u0304",
    ".": "\u0307",
    "c": "\u0327",
    "v": "\u030c",
    "u": "\u0306",
    "H": "\u030b",
    "r": "\u030a",
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


@dataclass
class SubmissionField:
    status: str
    draft: str = ""
    locations: list[dict[str, object]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


class SubmissionFieldResult(TypedDict):
    status: str
    draft: str
    locations: list[dict[str, object]]
    issues: list[str]
    candidates: list[str]


def safe_display(value: str) -> str:
    """Show controls as visible escapes rather than sending them to a terminal."""
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in value
    )


def convert_text(
    value: str,
    *,
    depth: int = 0,
    keyword_separators: bool = False,
    redefined_commands: frozenset[str] = frozenset(),
    literal_punctuation: bool = False,
) -> tuple[str, list[str]]:
    """Convert a deliberately small grammar without executing or dropping unknown TeX."""
    if depth > 32 or len(value) > MAX_ARGUMENT_CHARS:
        return safe_display(value), ["Text exceeds bounded conversion limits"]
    out: list[str] = []
    issues: list[str] = []
    if any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\r\t" for char in value):
        issues.append("Unsupported control or directional character needs manual review")
    # Braces inside comments do not delimit groups. Keep offsets aligned with
    # the original input so payloads retain their comments for conversion.
    masked = list(value)
    cursor = 0
    while cursor < len(value):
        if value[cursor] == "\\":
            token = _TOKEN.match(value, cursor)
            if token and token.group(1) in {"url", "nolinkurl", "path"}:
                opening = token.end()
                while opening < len(value) and value[opening].isspace():
                    opening += 1
                literal = _group(value, opening)
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
    grouped_text = "".join(masked)

    def group(start: int, opening: str = "{", closing: str = "}"):
        argument = _group(grouped_text, start, opening, closing)
        if argument is not None:
            return argument.__class__(
                argument.start, argument.end, value[argument.start : argument.end]
            )
        return None

    pos = 0
    while pos < len(value):
        char = value[pos]
        if char == "%":
            end = value.find("\n", pos)
            pos = len(value) if end < 0 else end + 1
            while pos < len(value) and value[pos] in " \t":
                pos += 1
            continue
        if char == "$":
            end = pos + 1
            while end < len(value) and (value[end] != "$" or value[end - 1] == "\\"):
                end += 1
            fragment = value[pos : min(end + 1, len(value))]
            math = value[pos + 1 : end]
            for name, symbol in _GREEK.items():
                if name not in redefined_commands:
                    math = re.sub(r"\\" + name + r"\b", symbol, math)
            # No scripts, fractions, macros, displays, or implicit symbol substitutions.
            if (
                end < len(value)
                and math.strip()
                and re.fullmatch(r"[A-Za-z0-9α-ω\s=+−.,()<>/\-]+", math)
            ):
                out.append(math)
            else:
                out.append(fragment)
                issues.append("Mathematics needs manual conversion: " + fragment)
            pos = min(end + 1, len(value))
            continue
        if char == "\\":
            token = _TOKEN.match(value, pos)
            if token is None:
                out.append(char)
                issues.append("Trailing backslash")
                pos += 1
                continue
            name = token.group(1)
            stop = token.end()
            cursor = stop
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
            argument = group(cursor)
            if name in redefined_commands:
                end = argument.end + 1 if argument is not None else stop
                if name == "href" and argument is not None:
                    cursor = end
                    while cursor < len(value) and value[cursor].isspace():
                        cursor += 1
                    second = group(cursor)
                    if second is not None:
                        end = second.end + 1
                out.append(value[pos:end])
                issues.append("Locally defined command needs manual review: \\" + name)
                pos = end
                continue
            if name in _TEXT_WRAPPERS or name in {"text", "href"}:
                if name == "href" and argument is not None:
                    cursor = argument.end + 1
                    while cursor < len(value) and value[cursor].isspace():
                        cursor += 1
                    argument = group(cursor)
                if argument is not None:
                    text, unknown = convert_text(
                        argument.value,
                        depth=depth + 1,
                        keyword_separators=keyword_separators,
                        redefined_commands=redefined_commands,
                        literal_punctuation=False
                        if name in {"textrm", "textsf", "textnormal"}
                        else literal_punctuation or name == "texttt",
                    )
                    out.append(text)
                    issues.extend(unknown)
                    pos = argument.end + 1
                    continue
            elif name in {"url", "nolinkurl", "path"} and argument is not None:
                out.append(argument.value)
                pos = argument.end + 1
                continue
            elif name in _ACCENTS:
                letter = argument.value if argument is not None else value[cursor : cursor + 1]
                if letter in {r"\i", r"\j"} and letter[1:] not in redefined_commands:
                    letter = letter[1:]
                if len(letter) == 1 and letter.isalpha():
                    out.append(unicodedata.normalize("NFC", letter + _ACCENTS[name]))
                    pos = argument.end + 1 if argument is not None else cursor + 1
                    continue
            elif name in _SYMBOLS:
                out.append(_SYMBOLS[name])
                pos = cursor
                continue
            elif name in {"%", "&", "#", "_", "{", "}", "$"}:
                out.append(name)
                pos = stop
                continue
            elif name in {"par", "newline", "linebreak", "\\", " ", ",", ";", "sep", "and"}:
                if cursor < len(value) and value[cursor] == "[":
                    option = group(cursor, "[", "]")
                    if option is not None:
                        cursor = option.end + 1
                out.append("; " if keyword_separators and name in {"sep", "and"} else " ")
                pos = cursor
                continue
            elif name in {"em", "itshape", "bfseries", "normalfont", "protect"}:
                if name == "normalfont":
                    # Recursive group conversion keeps this font reset local to
                    # its enclosing group, just like the typewriter wrapper.
                    literal_punctuation = False
                pos = cursor
                continue
            # Preserve an unknown fragment whole so draft text never invents its meaning.
            end = argument.end + 1 if argument is not None else stop
            out.append(value[pos:end])
            issues.append("Unsupported command: \\" + name)
            pos = end
            continue
        if char == "{":
            argument = group(pos)
            if argument is not None:
                text, unknown = convert_text(
                    argument.value,
                    depth=depth + 1,
                    keyword_separators=keyword_separators,
                    redefined_commands=redefined_commands,
                    literal_punctuation=literal_punctuation,
                )
                out.append(text)
                issues.extend(unknown)
                pos = argument.end + 1
                continue
            issues.append("Unclosed group")
        elif char == "}":
            issues.append("Unmatched closing brace")
        if char == "~":
            out.append(" ")
        elif not literal_punctuation and value.startswith("---", pos):
            out.append("—")
            pos += 3
            continue
        elif not literal_punctuation and value.startswith("--", pos):
            out.append("–")
            pos += 2
            continue
        elif not literal_punctuation and (
            value.startswith("``", pos) or value.startswith("''", pos)
        ):
            out.append("“" if char == "`" else "”")
            pos += 2
            continue
        else:
            out.append(char)
        pos += 1
    draft = unicodedata.normalize("NFC", " ".join("".join(out).split()))
    return safe_display(draft), sorted({safe_display(issue) for issue in issues})


def read_submission_text(root: Path, main: str | None = None) -> dict[str, SubmissionFieldResult]:
    """Read unique declarations, preserving raw source behind the shared masked lexer."""
    document = _read(root, main)
    # A supported spelling does not establish meaning when the local project
    # defines it. Do not infer conditional or order-dependent activation.
    redefined: set[str] = set()
    active_characters: set[str] = set()
    for path in document.selected:
        source = document.inspection.sources.get(path)
        if source is None:
            continue
        for command in source.commands:
            if command.name not in _DEFINITIONS:
                continue
            if command.arguments and command.name not in {
                "def",
                "gdef",
                "edef",
                "xdef",
                "let",
                "futurelet",
            }:
                target = command.arguments[0].value.strip()
            else:
                target = source.masked[command.start + len(command.name) + 1 :].lstrip()
            token = _TOKEN.match(target)
            if token is not None:
                redefined.add(token.group(1))
            elif target and command.name in {"def", "gdef", "edef", "xdef", "let", "futurelet"}:
                active_characters.add(target[0])
            elif "environment" in command.name.casefold() and re.fullmatch(r"[A-Za-z]+", target):
                redefined.add(target)
    blocked_commands = frozenset(redefined)
    raw: list[str] = []
    graph_issues = list(document.presence_uncertainty)
    for segment in document.segments:
        source = document.inspection.sources[segment.path]
        line_start = 0
        for _ in range(segment.line - 1):
            line_start = source.text.find("\n", line_start) + 1
        chunk = document.text[segment.start : segment.end]
        offset = source.masked.find(chunk, line_start)
        line_end = source.text.find("\n", line_start)
        if offset < 0 or (line_end >= 0 and offset > line_end):
            graph_issues.append("Could not recover original source text")
            raw.append(chunk)
        else:
            raw.append(source.text[offset : offset + len(chunk)])
    original = "".join(raw)
    declarations: dict[str, list[tuple[int, int, int, list[str]]]] = {
        name: [] for name in ("title", "abstract", "keywords")
    }
    definitions = _definition_spans(
        document.text,
        [command for command in document.commands if command.name not in {"let", "futurelet"}],
    )
    # The general checker's definition span can conservatively consume a later
    # group on the same line after a primitive assignment. For field extraction,
    # delimit literal let assignments so following declarations remain visible.
    for command in document.commands:
        if command.name not in {"let", "futurelet"}:
            continue
        cursor = command.start + len(command.name) + 1
        for _ in range(3 if command.name == "futurelet" else 2):
            while cursor < len(document.text) and document.text[cursor].isspace():
                cursor += 1
            if document.text[cursor : cursor + 1] == "=":
                cursor += 1
                while cursor < len(document.text) and document.text[cursor].isspace():
                    cursor += 1
            token = _TOKEN.match(document.text, cursor)
            cursor = token.end() if token is not None else cursor + 1
        definitions.append((command.start, cursor))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(definitions):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    definitions = merged
    for command in document.commands:
        if command.name not in declarations or _within(definitions, command.start):
            continue
        reasons = []
        if command.depth:
            reasons.append("Declaration inside a group or macro argument")
        if in_conditional(document.conditionals, command.start):
            reasons.append("Conditional declaration")
        if not command.arguments:
            reasons.append("Missing literal argument")
        arg = command.arguments[0] if command.arguments else None
        declarations[command.name].append(
            (
                command.start,
                arg.start if arg else command.start,
                arg.end if arg else command.start,
                reasons,
            )
        )
    for name, environments in (
        ("abstract", {"abstract"}),
        ("keywords", {"keywords", "keyword", "IEEEkeywords"}),
    ):
        regions, reasons = _regions(document, environments)
        for region in regions:
            if _within(definitions, region.command.start):
                continue
            # The lexer records every following group as a possible argument.
            # An environment name consumes only its first group; preserve any
            # ordinary grouped prose immediately following it.
            start = region.command.arguments[0].end + 1
            scope_issues = [
                issue
                for issue in region.uncertainty
                if not issue.startswith("unexpanded or context-dependent constructs:")
            ]
            declarations[name].append(
                (region.command.start, start, region.end, [*reasons, *scope_issues])
            )
    result = {}
    for name, candidates in declarations.items():
        issues = list(graph_issues)
        texts = []
        locations = []
        for position, start, end, reasons in candidates:
            raw_text = original[start:end]
            used_active = active_characters.intersection(raw_text)
            if used_active:
                # Active characters can change tokenization and even punctuation
                # inside math. Preserve the field instead of guessing where the
                # locally assigned meaning is in force.
                draft = safe_display(" ".join(raw_text.split()))
                unknown = [
                    "Locally defined active character needs manual review: " + char
                    for char in sorted(used_active)
                ]
            else:
                draft, unknown = convert_text(
                    raw_text,
                    keyword_separators=name == "keywords",
                    redefined_commands=blocked_commands,
                )
            if name in blocked_commands:
                issues.append("Locally defined declaration needs manual review: " + name)
            texts.append(draft)
            location = document.location(position)
            if isinstance(location.get("path"), str):
                location["path"] = safe_display(str(location["path"]))
            locations.append(location)
            issues.extend([*reasons, *unknown])
        if len(candidates) > 1:
            issues.append("Multiple declarations require explicit source clarification")
        if not candidates:
            status = "unresolved" if issues else "missing"
        elif len(candidates) != 1 or issues:
            status = "unresolved"
        else:
            status = "ready" if texts[0] else "empty"
        result[name] = cast(
            SubmissionFieldResult,
            asdict(
                SubmissionField(
                    status,
                    texts[0] if len(texts) == 1 else "",
                    locations,
                    sorted({safe_display(issue) for issue in issues}),
                    texts if len(texts) > 1 else [],
                )
            ),
        )
    return result
