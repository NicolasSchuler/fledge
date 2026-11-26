"""Reading one project source and lexing it, within the byte and command budgets.

``load_source`` turns a project file into a ``ParsedSource`` or reports, through
``GraphFindings``, why the lexical subset cannot use it: a link, a file over the size
limit, one that cannot be read or decoded, a group the lexer cannot bound, or a
project over the command budget. ``document_roots`` says which loaded sources are
documents of their own, for the graph and for root discovery alike. It owns the budgets
one graph spends while loading; it does not decide which files to load
(``project_files``), judge the commands in a source (``source_command_checks``) or
follow references (``source_graph``).
"""

from __future__ import annotations

import codecs
import posixpath
from bisect import bisect_left
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .limits import KIB, MIB
from .source_file_commands import (
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    candidate_paths,
    file_argument_index,
    is_literal_text,
)
from .source_findings import FOREIGN_ENCODING_RULE, GraphFindings
from .tex_lexer import (
    MAX_ARGUMENT_CHARS,
    MAX_COMMANDS,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Argument,
    Command,
    ParseLimit,
    ParseLimitExceeded,
    blank_regions,
    comment_starts,
    end_of_line,
    scan_commands,
    source_regions,
)
from .tex_spans import conditional_spans, in_conditional, runs_in_place, stops_source
from .text_edits import with_bom

# How a source that is not valid UTF-8 is read: as UTF-8, with every byte that does not
# decode taken as the Windows-1252 character of that value, which covers Latin-1 text; the
# five bytes Windows-1252 leaves unused become U+FFFD. The commands of TeX are ASCII in
# every encoding TeX reads byte by byte, so the structure of the file is exact and Western
# text reads as written; other text is approximate. Python marks each such byte as a lone
# surrogate (``surrogateescape``), and this table replaces the marks, which costs no
# call per byte.
_FOREIGN_BYTE_TEXT = {
    0xDC00 + byte: bytes([byte]).decode("cp1252", "replace") for byte in range(0x80, 0x100)
}
# Encodings in which the second byte of a character may be an ASCII byte, among them the
# characters that structure TeX source. In Shift-JIS the character 表 ends in a backslash:
# read byte by byte, 表\input{x} hides the command. A file that is valid text in one of
# these encodings, and in which such a byte belongs to a character, is not read.
_ASCII_TRAIL_CODECS = ("cp932", "shift_jisx0213", "gb18030", "big5hkscs", "cp950", "johab")
_STRUCTURE_BYTES = tuple(bytes([byte]) for byte in b"\\{}[]")


def _may_hide_structure(raw: bytes) -> bool:
    """Return whether ``raw`` may be double-byte text in which a TeX character is half a character.

    A backslash, brace or bracket that stands alone decodes to one character, the same one
    wherever it stands (not always itself: Shift-JIS X 0213 reads a backslash as a yen
    sign). When a file decodes in one of ``_ASCII_TRAIL_CODECS`` and holds fewer of those
    characters than of the bytes, some of the bytes are the second half of a character.
    Western text almost never decodes in them: an accented letter in front of a blank or
    a full stop is not a character there.

    Args:
        raw: The bytes of a file that is not valid UTF-8.

    Returns:
        ``True`` when a byte-wise reader would see a structural character that such an
        encoding reads as part of another character.
    """
    counts = [raw.count(byte) for byte in _STRUCTURE_BYTES]
    for codec in _ASCII_TRAIL_CODECS:
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        if [text.count(byte.decode(codec)) for byte in _STRUCTURE_BYTES] != counts:
            return True
    return False


# The commands with which a main file reads part of its own document from another file.
_DOCUMENT_INPUT_COMMANDS = frozenset({"input", "include"})

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
    """One readable project source and its lexical scan.

    Attributes:
        text: The decoded text of the file, without its byte-order mark.
        masked: The text the lexer scanned.
        commands: The commands the lexer found in ``masked``.
        bom: Whether the file began with a UTF-8 byte-order mark.
        comment_starts: Where the blanked comments begin.
        unclosed_literals: Where the unterminated literal regions begin.
        foreign: Whether the file is not valid UTF-8. Such a source is read for its
            commands (see ``_read_text``); ``text`` does not encode back to the bytes of
            the file, so the source is never rewritten.
    """

    text: str
    # ``text`` with comments and literal regions blanked; offsets are unchanged.
    masked: str
    commands: list[Command]
    bom: bool = False
    # Offsets of the comments blanked in ``masked``, in order.
    comment_starts: tuple[int, ...] = ()
    # Offsets of the literal regions that have no closing delimiter.
    unclosed_literals: tuple[int, ...] = ()
    foreign: bool = False

    def line_at(self, offset: int) -> int:
        """Return the 1-based line of ``offset`` in ``text``."""
        return self.text.count("\n", 0, offset) + 1

    def has_buffered_endinput_suffix(self, command: Command) -> bool:
        r"""Return whether ``\endinput`` leaves noncomment tokens on its physical line.

        Use the raw text before the comment, since the lexer masks URL and other
        literal commands as well as comments, and those commands are still tokens.
        """
        if command.name != "endinput":
            return False
        start = command.name_end
        end = end_of_line(self.text, start)
        comment = bisect_left(self.comment_starts, start)
        if comment < len(self.comment_starts):
            end = min(end, self.comment_starts[comment])
        return bool(self.text[start:end].strip())

    def encode(self, text: str | None = None) -> bytes:
        """Encode ``text`` as the file stores it: UTF-8, with the source's byte-order mark.

        Args:
            text: The text to encode; ``None`` encodes the source's own text.

        Returns:
            The encoded bytes, starting with a byte-order mark when the source had one.

        Raises:
            PreparationError: The source is not valid UTF-8, so its text does not encode
                back to its bytes. Every transformation leaves such a source out and its
                file is copied as it is, so this is a defect.
        """
        if self.foreign:
            raise PreparationError("A source that is not valid UTF-8 cannot be rewritten")
        return with_bom(self.text if text is None else text, bom=self.bom).encode("utf-8")


def file_command_end(source: ParsedSource, command: Command) -> int:
    r"""Return the end of a file command that has its file argument, braced or bare.

    Later groups belong to the text that follows the file read, not the read itself.
    The lexer, which does not know most command signatures, may record those groups
    as additional arguments.
    """
    argument = command.arguments[file_argument_index(command.name)]
    return argument.end + (source.masked[argument.start - 1] == "{")


class SubfileBody(NamedTuple):
    r"""The lexical wrapper of a file entered with ``\subfile``.

    Attributes:
        declaration: The class declaration that supplies the parent argument.
        parent: The literal parent argument, trimmed to its text.
        start: The offset just after the opening document delimiter.
        end: The offset of the closing document delimiter.
    """

    declaration: Command
    parent: Argument
    start: int
    end: int


def read_subfile_body(
    name: str, source: ParsedSource, findings: GraphFindings
) -> SubfileBody | None:
    """Locate the sole literal document environment and parent declaration of a subfile.

    Unknown wrappers are reported and have no body in the supported subset. The caller
    must still resolve the parent and check that it names the selected main file.

    Args:
        name: The project-relative name of the file.
        source: Its parsed source.
        findings: Receives findings for an unsupported wrapper.

    Returns:
        The subfile's body offsets and declared parent, or ``None`` for an unsupported wrapper.
    """
    declarations = [command for command in source.commands if command.name == "documentclass"]
    starts = [command for command in source.commands if command.begins("document", exact=True)]
    ends = [command for command in source.commands if command.ends("document", exact=True)]
    if (
        len(declarations) != 1
        or len(starts) != 1
        or len(ends) != 1
        or not declarations[0].arguments
        or declarations[0].arguments[0].value != "subfiles"
        or not declarations[0].start < starts[0].start < ends[0].start
    ):
        findings.uncertain(
            "source.subfile_structure",
            "A subfile requires one literal subfiles class and document environment.",
            name,
        )
        return None
    declaration = declarations[0]
    if len(declaration.options) != 1 or not is_literal_text(declaration.options[0].value):
        findings.uncertain(
            "source.subfile_structure", "A subfile requires an explicit parent path.", name
        )
        return None
    return SubfileBody(
        declaration,
        declaration.options[0].stripped(),
        starts[0].arguments[0].end + 1,
        ends[0].start,
    )


def is_document_root(commands: Sequence[Command]) -> bool:
    """Return whether a source declares a document class or opens the document environment."""
    return any(
        command.name == "documentclass" or command.begins("document") for command in commands
    )


def _literal_inputs(name: str, commands: Sequence[Command]) -> Iterator[str]:
    r"""Yield the project paths that the literal ``\input`` and ``\include`` of a file may name.

    The file is read as a main file, so a name resolves against its own directory
    with the command's usual suffix handling. Only a command that runs where it
    stands counts: one inside a conditional, a definition or the argument of a command
    that may drop it, as in ``\iffalse\input{supplement}\fi``, may read nothing.

    Args:
        name: The project-relative path of the reading file.
        commands: The commands of the reading file.

    Yields:
        Each project path a literal argument may name.
    """
    directory = posixpath.dirname(name)
    conditionals = conditional_spans(list(commands))
    for command in commands:
        if stops_source(command, conditionals):
            break
        if (
            command.name not in _DOCUMENT_INPUT_COMMANDS
            or command.named_by
            or not command.arguments
            or in_conditional(conditionals, command.start)
            or not runs_in_place(command, (), frozenset())
        ):
            continue
        value = command.arguments[0].value.strip()
        if is_literal_text(value):
            path = posixpath.normpath(posixpath.join(directory, value))
            yield from candidate_paths(
                command.name, [path], DEFAULT_SUFFIXES_BY_FILE_COMMAND[command.name]
            )


def document_roots(commands_by_name: Mapping[str, Sequence[Command]]) -> list[str]:
    r"""Return the files that are documents of their own, in the order given.

    A candidate declares a document class or opens the document environment. A candidate
    that another candidate reads with a literal ``\input`` or ``\include`` that runs where
    it stands is part of that document, as a ``body.tex`` that holds ``\begin{document}``
    or a ``header.tex`` that holds ``\documentclass``, and is not a root. When that would
    leave no root, as for two candidates that read each other, every candidate is
    returned, so the choice is left to the user instead of being made here.

    Args:
        commands_by_name: The commands of each source that may be a main file, by its
            project-relative path.

    Returns:
        The project-relative paths of the roots.
    """
    candidates = {
        name: commands for name, commands in commands_by_name.items() if is_document_root(commands)
    }
    read_by_others = {
        path
        for name, commands in candidates.items()
        for path in _literal_inputs(name, commands)
        if path != name
    }
    roots = [name for name in candidates if name not in read_by_others]
    return roots or list(candidates)


@dataclass(slots=True)
class LoadBudget:
    """What one graph has spent on loading: bytes read and commands scanned so far.

    Attributes:
        source_bytes: The bytes read from source files so far.
        commands: The commands scanned in loaded sources so far.
    """

    source_bytes: int = 0
    commands: int = 0

    @property
    def remaining_bytes(self) -> int:
        """The largest file one more load may read: the per-file limit or what is left."""
        return min(MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES - self.source_bytes)


@dataclass(frozen=True, slots=True)
class _SourceText:
    """The decoded text of a file, and whether it began with a byte-order mark.

    Attributes:
        text: The decoded text, without the byte-order mark.
        bom: Whether the file began with a UTF-8 byte-order mark.
        foreign: Whether the bytes of the file are not valid UTF-8.
    """

    text: str
    bom: bool
    foreign: bool = False


def _read_text(
    name: str, path: Path, budget: LoadBudget, findings: GraphFindings
) -> _SourceText | None:
    """Read and decode ``path`` within the budget, or report why it cannot be used.

    A file that was read adds its bytes to ``budget`` even when it then fails to decode.

    Args:
        name: The project-relative name the findings report.
        path: The file to read.
        budget: The loading budget; it limits the read and records its bytes.
        findings: Where a file over the size limit, an unreadable file or one that is not
            valid UTF-8 is recorded as an uncertainty.

    Returns:
        The decoded text, or ``None`` when the file cannot be used. A file that is not
        valid UTF-8 is read as ``_FOREIGN_BYTE_TEXT`` describes, unless it holds a NUL
        byte or may be double-byte text that hides TeX's own characters.
    """
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
    bom = raw.startswith(codecs.BOM_UTF8)
    try:
        return _SourceText(raw.decode("utf-8-sig"), bom)
    except UnicodeDecodeError as error:
        line = raw.count(b"\n", 0, error.start) + 1
    text = raw.decode("utf-8-sig", "surrogateescape").translate(_FOREIGN_BYTE_TEXT)
    # UTF-16, UTF-32 or binary data holds no command a byte-wise reader would find, and
    # in Shift-JIS, GBK or Big5 text a backslash or brace may be half of a character.
    if b"\x00" in raw or _may_hide_structure(raw):
        findings.uncertain(
            "source.encoding",
            "Source is not text in an encoding that can be read; its contents were not changed.",
            name,
            line,
            suggestion="Convert an explicit copy to UTF-8 before preparing it.",
        )
        return None
    findings.uncertain(
        FOREIGN_ENCODING_RULE,
        "Source is not valid UTF-8. Its commands were read, with the other bytes taken as "
        "Windows-1252, and the file is never changed.",
        name,
        line,
        suggestion="Convert an explicit copy to UTF-8 before preparing it, for example with "
        "iconv -f cp1252 -t utf-8, so that its wording is checked as written and the file "
        "can be transformed.",
    )
    return _SourceText(text, bom, foreign=True)


def _report_parse_limit(
    name: str, text: str, error: ParseLimitExceeded, findings: GraphFindings
) -> None:
    """Record which bound of the lexical subset a source exceeded, and where.

    Args:
        name: The project-relative name the finding reports.
        text: The text of the source, used to turn the offset into a line.
        error: The exceeded bound and its offset, as the lexer raised it.
        findings: Where the uncertainty is recorded.
    """
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

    Args:
        name: The project-relative name of the file.
        path: The file on disk; a symbolic link is refused.
        budget: The bytes and commands the graph has spent so far.
        findings: Where every refusal is recorded.

    Returns:
        The parsed source, or ``None`` when the file is a link, cannot be read or decoded,
        exceeds a bound of the lexer, or takes the project over the command budget.
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
        decoded.text,
        masked,
        commands,
        decoded.bom,
        tuple(comments),
        tuple(unclosed),
        decoded.foreign,
    )
