"""Findings from the bounded final-pass log of a LaTeX build and its bibliography tool's log.

A message is one log line together with the lines that TeX continues it on, so a package
error that spans six lines is reported whole. A message that Fledge's own restrictions
explain (shell escape, bitmap fonts, the fonts and TeX trees a build can see) carries the
next step that ``build_failure_help`` gives for it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .build_failure_help import explain_build_message
from .file_safety import read_file_prefix
from .findings import Finding, Severity
from .path_safety import contained_relative_name, lexically_escapes


@dataclass(frozen=True, slots=True)
class LogRule:
    """A kind of log message and the finding it produces.

    Every rule decides in time linear in the length of the message, so that a long or
    adversarial log line cannot stall a build check; ``tests/test_build_log.py`` holds a
    guard for each rule.

    Attributes:
        rule: The rule name of the finding.
        matches: Whether a message, given in lower case, is of this kind.
        severity: The severity of the finding.
    """

    rule: str
    matches: Callable[[str], bool]
    severity: Severity


def _contains(*phrases: str) -> Callable[[str], bool]:
    """Match a lower-case message that contains any of ``phrases``."""
    return lambda text: any(phrase in text for phrase in phrases)


def _in_order(first: str, then: str) -> Callable[[str], bool]:
    """Match a lower-case message where ``first`` occurs, followed later by ``then``.

    At least one character separates the two, as in the pattern ``first.+?then``. Comparing
    the first occurrence of ``first`` with the last occurrence of ``then`` decides this in
    linear time, where a lazy regular expression backtracks once for every occurrence of
    ``first``.
    """

    def matches(text: str) -> bool:
        start = text.find(first)
        return start != -1 and text.rfind(then) >= start + len(first) + 1

    return matches


def _either(*matchers: Callable[[str], bool]) -> Callable[[str], bool]:
    """Match a message that any of ``matchers`` matches."""
    return lambda text: any(matches(text) for matches in matchers)


def _tex_error(text: str) -> bool:
    """Match TeX's error lines, located errors, fatal errors and emergency stops.

    A located error is ``file:line: message`` whose message names an error or an undefined
    control sequence; the file name may itself contain colons.
    """
    if text.startswith("! ") or "fatal error" in text or "emergency stop" in text:
        return True
    location = _LOCATION_SEPARATOR.search(text, 1)
    if location is None:
        return False
    rest = text[location.end() :]
    return "error" in rest or "undefined control sequence" in rest


def _obsolete_package(text: str) -> bool:
    """Match a class or package warning that calls something obsolete or deprecated."""
    warning = text.find(" warning: ")
    if warning == -1 or not ("package " in text[:warning] or "class " in text[:warning]):
        return False
    rest = text[warning:]
    return any(word in rest for word in _OBSOLESCENCE_WORDS)


_LOCATION_SEPARATOR = re.compile(r":\d+: ")
# Words with which classes and packages announce that a command, option or package is out of
# date, such as fixltx2e's "is not required with releases after 2015".
_OBSOLESCENCE_WORDS = (
    "obsolete",
    "deprecated",
    "is not required with releases",
    "no longer needed",
    "no longer supported",
    "old latex 2.09 command",
)
_RERUN = re.compile(r"rerun to get|label\(s\) may have changed|please \(?re\)?run|rerun latex")
LOG_RULES = (
    LogRule(
        "build.undefined_citation",
        _either(_in_order("citation ", " undefined"), _contains("there were undefined citations")),
        "error",
    ),
    LogRule(
        "build.undefined_reference",
        _either(
            _in_order("reference ", " undefined"), _contains("there were undefined references")
        ),
        "error",
    ),
    LogRule(
        "build.duplicate_label",
        _either(_in_order("label ", " multiply defined"), _contains("multiply-defined labels")),
        "warning",
    ),
    LogRule("build.missing_character", _contains("missing character:"), "warning"),
    LogRule(
        "build.font_substitution",
        _contains("font warning:", "font shapes were not available"),
        "warning",
    ),
    LogRule("build.overfull_box", _contains("overfull \\hbox", "overfull \\vbox"), "warning"),
    LogRule("build.underfull_box", _contains("underfull \\hbox", "underfull \\vbox"), "warning"),
    LogRule("build.rerun_required", lambda text: _RERUN.search(text) is not None, "error"),
    LogRule("build.tex_error", _tex_error, "error"),
    LogRule(
        "build.unused_global_option", _contains("latex warning: unused global option"), "warning"
    ),
    LogRule(
        "build.float_too_large", _contains("latex warning: float too large for page"), "warning"
    ),
    LogRule(
        "build.pdf_string_token",
        _contains("package hyperref warning: token not allowed in a pdf string"),
        "info",
    ),
    LogRule("build.obsolete_package", _obsolete_package, "info"),
)
_TEX_ERROR_RULE = "build.tex_error"
_RERUN_RULE = "build.rerun_required"
# TeX continues a message on an indented line, or on one that starts with the name of the
# package in parentheses, such as ``(fontspec)``.
_CONTINUATION = re.compile(r"^(?:\s+|\([A-Za-z-]+\))")
_PACKAGE_PREFIX = re.compile(r"^\([A-Za-z-]+\)\s*")
# How many continuation lines and characters one message joins at most; a package error
# rarely has more than six lines, and the finding keeps ``_MAX_MESSAGE_CHARS`` of it.
_MAX_CONTINUATION_LINES = 12
_MAX_JOINED_CHARS = 4000
_INPUT_LINE = re.compile(r"(?:input line|at lines?)\s*(\d+)")
_COMPILER_LOCATION = re.compile(r"^([^:\n]+):(\d+):")
_OVERFLOW = re.compile(r"\(([\d.]+)pt too (wide|high)\)")
_MAX_MESSAGE_CHARS = 2000
# The bibliography tools' own warnings: BibTeX starts them with "Warning--", Biber writes
# "WARN - " after its line prefix. Each becomes one finding; a log with more is summarized.
_BIBTEX_WARNING = "Warning--"
_BIBER_WARNING = re.compile(r"\bWARN - (.{1,1000})")
_MAX_BIBLIOGRAPHY_WARNINGS = 20
_BIBLIOGRAPHY_WARNING_RULE = "build.bibliography_warning"


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

    A request to rerun is not reported once a TeX error stopped the build: the error is the
    cause, and the rerun would only repeat it.

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
    for message in log_messages(log):
        lowered = message.lower()
        for rule in LOG_RULES:
            if (rule.rule, message) not in seen and rule.matches(lowered):
                seen.add((rule.rule, message))
                findings.append(_log_finding(rule, message, main, project, cwd))
    if any(finding.rule == _TEX_ERROR_RULE for finding in findings):
        findings = [finding for finding in findings if finding.rule != _RERUN_RULE]
    return findings


def log_messages(log: str) -> Iterator[str]:
    """Yield every stripped log line, joined with the lines that TeX continues it on.

    A continuation line is indented or starts with a package name in parentheses, such as
    ``(fontspec)``; the package name is dropped from the joined message. At most
    ``_MAX_CONTINUATION_LINES`` lines and about ``_MAX_JOINED_CHARS`` characters are joined,
    so the work stays linear in the length of the log.

    Args:
        log: The log text.

    Yields:
        One message per log line.
    """
    lines = log.splitlines()
    for index, line in enumerate(lines):
        parts = [line.strip()]
        length = len(parts[0])
        for following in lines[index + 1 : index + 1 + _MAX_CONTINUATION_LINES]:
            if length >= _MAX_JOINED_CHARS or not _CONTINUATION.match(following):
                break
            part = _PACKAGE_PREFIX.sub("", following.strip(), count=1)
            parts.append(part[: _MAX_JOINED_CHARS - length])
            length += len(part) + 1
        yield " ".join(part for part in parts if part)


def _log_finding(
    rule: LogRule, message: str, main: str, project: Path | None, cwd: Path | None
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
        details when the message names them, a path and line only when the compiler
        location is a project source, and the next step for a failure that Fledge's
        restrictions explain.
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
        suggestion=explain_build_message(message) if rule.severity == "error" else None,
    )


def bibliography_log_findings(log: str, main: str) -> list[Finding]:
    """Note the warnings that BibTeX or Biber wrote to their log (``.blg``).

    BibTeX starts a warning with ``Warning--`` and may continue it on lines that start with
    ``--``; Biber writes ``WARN - `` after its line prefix. Each distinct warning becomes one
    informational finding, because BibTeX warns about a missing field in nearly every real
    bibliography; beyond ``_MAX_BIBLIOGRAPHY_WARNINGS``, one more finding counts the rest.

    Args:
        log: The text of the bibliography tool's log.
        main: The main document, the path of each finding.

    Returns:
        The findings, in log order.
    """
    warnings: list[str] = []
    lines = log.splitlines()
    for index, line in enumerate(lines):
        text = line.strip()
        if text.startswith(_BIBTEX_WARNING):
            warning = text.removeprefix(_BIBTEX_WARNING)
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if following.startswith("--"):
                warning += " " + following.removeprefix("--")
        elif biber := _BIBER_WARNING.search(text):
            warning = biber[1]
        else:
            continue
        if warning not in warnings:
            warnings.append(warning[:_MAX_MESSAGE_CHARS])
    findings = [
        Finding(
            _BIBLIOGRAPHY_WARNING_RULE,
            f"The bibliography tool warned: {warning}",
            severity="info",
            path=main,
            details={"document": main},
        )
        for warning in warnings[:_MAX_BIBLIOGRAPHY_WARNINGS]
    ]
    if len(warnings) > _MAX_BIBLIOGRAPHY_WARNINGS:
        findings.append(
            Finding(
                _BIBLIOGRAPHY_WARNING_RULE,
                f"The bibliography tool wrote {len(warnings) - _MAX_BIBLIOGRAPHY_WARNINGS} more "
                "warnings; read its .blg log for the rest.",
                severity="info",
                path=main,
                details={"document": main},
            )
        )
    return findings


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
