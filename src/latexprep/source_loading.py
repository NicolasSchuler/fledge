"""Reading one project source and lexing it, within the byte and command budgets.

``load_source`` turns a project file into a ``ParsedSource`` or reports, through
``GraphFindings``, why the lexical subset cannot use it: a link, a file over the size
limit, one that cannot be read or decoded, a group the lexer cannot bound, or a
project over the command budget. It owns the budgets one graph spends while loading;
it does not decide which files to load (``project_files``), judge the commands in a
source (``source_command_checks``) or follow references (``source_graph``).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path

from .file_safety import FileTooLargeError, read_regular_file
from .limits import KIB, MIB
from .source_findings import GraphFindings
from .tex_lexer import (
    MAX_ARGUMENT_CHARS,
    MAX_COMMANDS,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimit,
    ParseLimitExceeded,
    blank_regions,
    comment_starts,
    scan_commands,
    source_regions,
)
from .text_edits import with_bom

# What to tell the user for each way an argument can defeat the lexer's bounds.
_ARGUMENT_LIMIT_ADVICE: dict[ParseLimit, tuple[str, str]] = {
    "unclosed_group": (
        "A brace group opened on this line is never closed.",
        "Add the missing closing brace, or remove the extra opening brace.",
    ),
    "oversized_group": (
        f"A brace group on this line is longer than {MAX_ARGUMENT_CHARS // KIB} KiB.",
        "Move the long text out of the command argument, for example into its own file.",
    ),
    "argument_budget": (
        "The command arguments in this file together exceed the supported total size.",
        "Split the file, or shorten long or deeply nested command arguments.",
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedSource:
    """One readable project source and its lexical scan."""

    text: str
    # ``text`` with comments and literal regions blanked; offsets are unchanged.
    masked: str
    commands: list[Command]
    bom: bool = False
    # Offsets of the comments blanked in ``masked``, in order.
    comment_starts: tuple[int, ...] = ()
    # Offsets of the literal regions that have no closing delimiter.
    unclosed_literals: tuple[int, ...] = ()

    def line_at(self, offset: int) -> int:
        """The 1-based line of ``offset`` in ``text``."""
        return self.text.count("\n", 0, offset) + 1

    def encode(self, text: str | None = None) -> bytes:
        """``text`` (default: the source's own) as the file stores it, with its byte-order mark."""
        return with_bom(self.text if text is None else text, self.bom).encode("utf-8")


def is_document_root(commands: list[Command]) -> bool:
    """Whether a source declares a document class or opens the document environment."""
    return any(
        command.name == "documentclass" or command.begins("document") for command in commands
    )


@dataclass(slots=True)
class LoadBudget:
    """What one graph has spent on loading: bytes read and commands scanned so far."""

    source_bytes: int = 0
    commands: int = 0

    @property
    def remaining_bytes(self) -> int:
        """The largest file one more load may read: the per-file limit or what is left."""
        return min(MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES - self.source_bytes)


@dataclass(frozen=True, slots=True)
class _SourceText:
    """The decoded text of a file, and whether it began with a byte-order mark."""

    text: str
    bom: bool


def _read_text(
    name: str, path: Path, budget: LoadBudget, findings: GraphFindings
) -> _SourceText | None:
    """Read and decode ``path`` within the budget, or report why it cannot be used."""
    try:
        raw = read_regular_file(path, budget.remaining_bytes)
    except FileTooLargeError:
        findings.uncertain(
            "source.size_limit",
            f"Source parsing exceeded the {MAX_SOURCE_BYTES // MIB} MiB per-file "
            f"or {MAX_TOTAL_SOURCE_BYTES // MIB} MiB total limit.",
            name,
            suggestion="Reduce the selected source set or inspect this file separately.",
        )
        return None
    except OSError as error:
        # The cause alone: an operating-system message would name the local path.
        cause = (error.strerror or type(error).__name__).lower()
        findings.uncertain(
            "source.encoding",
            f"Source could not be read ({cause}); its contents were not changed.",
            name,
            suggestion="Make the file readable, then prepare it again.",
        )
        return None
    budget.source_bytes += len(raw)
    try:
        return _SourceText(raw.decode("utf-8-sig"), raw.startswith(codecs.BOM_UTF8))
    except UnicodeDecodeError:
        findings.uncertain(
            "source.encoding",
            "Source is not valid UTF-8; its encoding was not changed.",
            name,
            suggestion="Convert an explicit copy to UTF-8 before preparing it.",
        )
        return None


def _report_parse_limit(
    name: str, text: str, error: ParseLimitExceeded, findings: GraphFindings
) -> None:
    """Record which bound of the lexical subset a source exceeded, and where."""
    line = text.count("\n", 0, error.offset) + 1
    if error.reason == "command_count":
        findings.uncertain(
            "source.command_limit",
            f"This file has more than {MAX_COMMANDS:,} TeX commands, more than can be checked.",
            name,
            line,
            "Split the file or reduce the selected source set.",
        )
        return
    message, suggestion = _ARGUMENT_LIMIT_ADVICE[error.reason]
    findings.uncertain("source.argument_limit", message, name, line, suggestion)


def load_source(
    name: str, path: Path, budget: LoadBudget, findings: GraphFindings
) -> ParsedSource | None:
    """Read and lex the project file ``name``; ``None`` when it cannot be used.

    Every refusal is recorded in ``findings`` as an uncertainty, because the commands
    and references of a file that was not read are unknown. A source read adds its
    bytes and commands to ``budget``.
    """
    if path.is_symlink():
        findings.uncertain(
            "source.link",
            "Symbolic links are outside the supported source subset.",
            name,
            suggestion="Supply the actual project file inside the project.",
        )
        return None
    decoded = _read_text(name, path, budget, findings)
    if decoded is None:
        return None
    regions, unclosed = source_regions(decoded.text)
    masked = blank_regions(decoded.text, regions)
    comments = comment_starts(regions)
    try:
        commands = scan_commands(masked, comments)
    except ParseLimitExceeded as error:
        _report_parse_limit(name, decoded.text, error, findings)
        return None
    budget.commands += len(commands)
    if budget.commands > MAX_COMMANDS:
        findings.uncertain(
            "source.command_limit",
            f"The project's sources together have more than {MAX_COMMANDS:,} TeX commands, "
            "more than can be checked.",
            name,
            suggestion="Reduce the selected source set or inspect this file separately.",
        )
        return None
    return ParsedSource(
        decoded.text, masked, commands, decoded.bom, tuple(comments), tuple(unclosed)
    )
