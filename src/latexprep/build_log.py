"""Findings from the bounded final-pass log of a LaTeX build."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .file_safety import read_file_prefix
from .findings import Finding, Severity
from .path_safety import contained_relative_name, lexically_escapes


@dataclass(frozen=True, slots=True)
class _LogRule:
    """A log pattern and the finding it produces.

    Attributes:
        rule: The rule name of the finding.
        pattern: The compiled expression that a log message must match.
        severity: The severity of the finding.
    """

    rule: str
    pattern: re.Pattern[str]
    severity: Severity


def _rule(rule: str, pattern: str, severity: Severity) -> _LogRule:
    """Build a log rule whose pattern matches regardless of case.

    Args:
        rule: The rule name of the finding.
        pattern: The regular expression a log message must match.
        severity: The severity of the finding.

    Returns:
        The rule with its pattern compiled.
    """
    return _LogRule(rule, re.compile(pattern, re.IGNORECASE), severity)


_LOG_RULES = (
    _rule(
        "build.undefined_citation",
        r"Citation .+? undefined|There were undefined citations",
        "error",
    ),
    _rule(
        "build.undefined_reference",
        r"Reference .+? undefined|There were undefined references",
        "error",
    ),
    _rule(
        "build.duplicate_label", r"Label .+? multiply defined|multiply-defined labels", "warning"
    ),
    _rule("build.missing_character", r"Missing character:", "warning"),
    _rule("build.font_substitution", r"Font Warning:|font shapes were not available", "warning"),
    _rule("build.overfull_box", r"Overfull \\[hv]box", "warning"),
    _rule("build.underfull_box", r"Underfull \\[hv]box", "warning"),
    _rule(
        "build.rerun_required",
        r"Rerun to get|Label\(s\) may have changed|Please \(?re\)?run|Please rerun|rerun LaTeX",
        "error",
    ),
    _rule(
        "build.tex_error",
        r"^! |^.+?:\d+: .*(?:Error|Undefined control sequence)|Fatal error|Emergency stop",
        "error",
    ),
)
# TeX continues a message on an indented or parenthesized next line.
_CONTINUATION = re.compile(r"^(?:\s+|\([A-Za-z-]+\))")
_INPUT_LINE = re.compile(r"(?:input line|at lines?)\s*(\d+)")
_COMPILER_LOCATION = re.compile(r"^([^:\n]+):(\d+):")
_OVERFLOW = re.compile(r"\(([\d.]+)pt too (wide|high)\)")
_MAX_MESSAGE_CHARS = 2000


def read_bounded(path: Path, limit: int) -> tuple[str, bool]:
    """Read the first ``limit`` bytes of a regular, unlinked log as text.

    Args:
        path: The log file.
        limit: The largest number of bytes to read.

    Returns:
        The text, decoded as UTF-8 with replacement characters, and whether it is the
        whole log. A missing, linked or unreadable log reads as empty and incomplete.
    """
    try:
        data = read_file_prefix(path, limit + 1)
    except OSError:
        return "", False
    return data[:limit].decode("utf-8", errors="replace"), len(data) <= limit


def log_tail(log: str, *, lines: int = 60, max_characters: int = 8000) -> str:
    """Keep the closing log lines that explain a failure; reports redact centrally.

    Args:
        log: The log text.
        lines: The largest number of closing lines to keep.
        max_characters: The largest number of closing characters to keep.

    Returns:
        The last ``lines`` lines, cut to their last ``max_characters`` characters.
    """
    return "\n".join(log.splitlines()[-lines:])[-max_characters:]


def log_findings(
    log: str,
    main: str,
    project: Path | None = None,
    cwd: Path | None = None,
) -> list[Finding]:
    """Report one finding per distinct rule and message; paths only when the log names them.

    Args:
        log: The text of the final-pass log.
        main: The main document, recorded in each finding's details; without ``project``
            and ``cwd``, a relative compiler filename is read relative to its directory.
        project: The project root that a compiler filename must lie under to become the
            finding's path.
        cwd: The directory the compiler ran in, against which relative compiler filenames
            are resolved when ``project`` is given too.

    Returns:
        The findings, in log order.
    """
    if project is not None:
        project = project.resolve()
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for message in _log_messages(log):
        for rule in _LOG_RULES:
            if rule.pattern.search(message) and (rule.rule, message) not in seen:
                seen.add((rule.rule, message))
                findings.append(_log_finding(rule, message, main, project, cwd))
    return findings


def _log_messages(log: str) -> Iterator[str]:
    """Yield every stripped log line, joined with the next line when TeX continues there."""
    lines = log.splitlines()
    for index, line in enumerate(lines):
        message = line.strip()
        if index + 1 < len(lines) and _CONTINUATION.match(lines[index + 1]):
            message += " " + lines[index + 1].strip()
        yield message


def _log_finding(
    rule: _LogRule, message: str, main: str, project: Path | None, cwd: Path | None
) -> Finding:
    """Build the finding for one log message that ``rule`` matched.

    Args:
        rule: The matching rule.
        message: The log message, cut to ``_MAX_MESSAGE_CHARS`` characters in the finding.
        main: The main document, recorded in the details.
        project: The project root, passed to :func:`_source_path`.
        cwd: The directory the compiler ran in, passed to :func:`_source_path`.

    Returns:
        The finding, with the input line, compiler location and box overflow in its
        details when the message names them, and a path and line only when the compiler
        location is a project source.
    """
    details: dict[str, object] = {"document": main}
    source_path: str | None = None
    source_line: int | None = None
    if input_line := _INPUT_LINE.search(message):
        details["input_line"] = int(input_line[1])
    if location := _COMPILER_LOCATION.match(message):
        details["compiler_filename"] = location[1]
        details["compiler_line"] = int(location[2])
        source_path = _source_path(Path(location[1]), main, project, cwd)
        if source_path is not None:
            source_line = int(location[2])
    if overflow := _OVERFLOW.search(message):
        details.update({"overflow_pt": float(overflow[1]), "direction": overflow[2]})
    return Finding(
        rule.rule,
        message[:_MAX_MESSAGE_CHARS],
        severity=rule.severity,
        path=source_path,
        line=source_line,
        details=details,
    )


def _source_path(filename: Path, main: str, project: Path | None, cwd: Path | None) -> str | None:
    """Return the project-relative source a compiler diagnostic names, if it is one.

    Args:
        filename: The file the diagnostic names.
        main: The main document; without ``project`` and ``cwd``, a relative ``filename``
            is read relative to its directory.
        project: The project root.
        cwd: The directory the compiler ran in.

    Returns:
        The POSIX path relative to the project, or ``None`` when the file lies outside it,
        or when ``filename`` is absolute and ``project`` or ``cwd`` is missing.
    """
    if project is not None and cwd is not None:
        return contained_relative_name(
            project, filename if filename.is_absolute() else cwd / filename
        )
    if filename.is_absolute():
        return None
    relative = os.path.normpath(str(Path(main).parent / filename))
    return None if lexically_escapes(relative) else Path(relative).as_posix()
