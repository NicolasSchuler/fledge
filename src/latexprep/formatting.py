"""Explicit tex-fmt adapter; computes patches without modifying the input.

For each selected source file the adapter runs tex-fmt twice on a private copy and
proposes the first result only if the second pass leaves it unchanged, protected
regions (``tex_protected_regions``) survive byte for byte, and the configured
blank-line policy holds. Nothing is written back: the result carries proposed bytes,
the original bytes each proposal was computed from, reviewable changes and findings.
The tex-fmt process runs through a ``CommandRunner``; this module owns what to ask of
it and how to judge the answer, not how it is confined.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple

from .cancellation import bounded_map
from .command_runner import CommandResult, CommandRunner, RuntimeLimits
from .errors import PreparationError, SettingsError
from .findings import Finding, has_blockers
from .limits import MIB
from .option_config import Bounds, require_unique_strings, validate_scalars
from .path_safety import is_relative_posix_path
from .report import Change, ChangeKind
from .resource_budget import ResourceBudget
from .tex_commands import ROOT_SUFFIXES
from .tex_lexer import MAX_SOURCE_BYTES
from .tex_protected_regions import protected_spans
from .text_edits import unified_diff_text

_FORMATTER_MEMORY_MB = 128
# Memory reserved per byte of source and of formatter output: decoded strings, split
# lines and diff buffers. A conservative admission estimate, not a measured peak.
_MEMORY_BYTES_PER_BYTE = 16
MIN_WRAP_LENGTH = 20
MAX_TAB_SIZE = 16
BlankLines = Literal["formatter", "preserve", "collapse"]
_WHITESPACE = re.compile(r"\s+")
# Longest piece of tex-fmt's own message quoted in a finding.
_MAX_FORMATTER_MESSAGE_CHARS = 200
# An absolute POSIX path, which would reveal the local directory layout.
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])/(?:[\w.@+~-]+/)*[\w.@+~-]*")


@dataclass(frozen=True)
class FormattingOptions:
    """How tex-fmt is run and judged: exclusions, blank-line policy, wrap length, tab size.

    Construction raises ``SettingsError`` when a value is invalid, ``exclude`` repeats an
    entry, or an ``exclude`` entry is not a glob pattern relative to the project.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to ``wrap_length`` and
            ``tab_size``.
        exclude: Glob patterns, relative to the project, of sources not to format.
        blank_lines: The blank-line policy: ``formatter`` leaves blank lines to tex-fmt,
            ``preserve`` rejects a proposal that moves a paragraph separator, and
            ``collapse`` keeps one blank line of every unprotected run.
        wrap_length: The line length tex-fmt wraps at (``--wraplen``).
        tab_size: The indentation width tex-fmt uses (``--tabsize``).
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "wrap_length": Bounds(MIN_WRAP_LENGTH),
        "tab_size": Bounds(1, MAX_TAB_SIZE),
    }

    exclude: tuple[str, ...] = ()
    blank_lines: BlankLines = "formatter"
    # Read from the top-level wrap_length and tab_size settings.
    wrap_length: int = 80
    tab_size: int = 2

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings("exclude", self.exclude, allow_surrounding_whitespace=True)
        if any(
            not is_relative_posix_path(pattern, normalized=False, characters="no_line_breaks")
            for pattern in self.exclude
        ):
            raise SettingsError(
                "exclude entries must be glob patterns relative to the project", ("exclude",)
            )


@dataclass(frozen=True, slots=True)
class FormatterContext:
    """What every file's formatting shares: the tool, how to run it, and the options.

    Attributes:
        executable: The tex-fmt executable.
        runner: Runs tex-fmt.
        options: The formatting options.
    """

    executable: str
    runner: CommandRunner
    options: FormattingOptions


@dataclass(frozen=True, slots=True)
class FormatJob:
    """One file to format.

    Attributes:
        path: The file.
        relative: Its name in the project.
        workspace: The private directory for its tex-fmt passes (a file's workspace cannot
            contain another file's).
        size: Its size in bytes when it was admitted.
    """

    path: Path
    relative: str
    workspace: Path
    size: int


class _SourceFile(NamedTuple):
    """A source file selected for formatting.

    Attributes:
        path: The file.
        relative: Its project-relative POSIX name.
    """

    path: Path
    relative: str


def _lines_with_blank_state(text: str) -> Iterator[tuple[str, bool]]:
    """Yield each line, and whether it is blank outside every protected span."""
    spans = protected_spans(text).spans
    offset = 0
    for line in text.splitlines(keepends=True):
        protected = any(start < offset + len(line) and end > offset for start, end in spans)
        yield line, not line.strip() and not protected
        offset += len(line)


def _collapse_blank_lines(text: str) -> str:
    """Keep one blank line of every unprotected run of blank lines."""
    parts: list[str] = []
    previous_blank = False
    for line, blank in _lines_with_blank_state(text):
        if not (blank and previous_blank):
            parts.append(line)
        previous_blank = blank
    return "".join(parts)


def _paragraph_separators(text: str) -> tuple[tuple[str, int], ...]:
    """Fingerprint each unprotected blank-line run by the text before it.

    Paragraph anchors ignore whitespace so ordinary line wrapping is allowed,
    but moving a blank line across non-whitespace source text is detectable.
    """
    signature: list[tuple[str, int]] = []
    anchor: list[str] = []
    count = 0
    for line, blank in _lines_with_blank_state(text):
        if blank:
            count += 1
            continue
        if count:
            signature.append(("".join(anchor), count))
            anchor.clear()
            count = 0
        anchor.append(_WHITESPACE.sub("", line))
    if count:
        signature.append(("".join(anchor), count))
    return tuple(signature)


@dataclass
class FormattingResult:
    """Proposed formatting: new bytes with the originals they came from, changes, findings.

    Attributes:
        contents: The proposed bytes, by project-relative name.
        changes: The reviewable changes.
        findings: The findings of the formatting.
        tools: The tex-fmt version, as reports record it.
        originals: The original bytes each proposal was computed from.
    """

    contents: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tools: dict[str, object] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)


def _format_memory_mb(source_bytes: int, runner: CommandRunner) -> int:
    """Estimate the memory, in MiB, to reserve for formatting a source of ``source_bytes``.

    The estimate covers the formatter and buffers for the source and for the most output
    the runner allows.
    """
    output_bytes = runner.limits.max_output_bytes
    return (
        _FORMATTER_MEMORY_MB
        + (_MEMORY_BYTES_PER_BYTE * (source_bytes + output_bytes) + MIB - 1) // MIB
    )


def _unformattable(rule: str, message: str, relative: str) -> Finding:
    """Build an inconclusive error that keeps the file ``relative`` from being formatted."""
    return Finding(rule, message, severity="error", status="inconclusive", path=relative)


def _formatter_failure(result: CommandResult, subject: str, limits: RuntimeLimits) -> str:
    """Explain why tex-fmt did not complete: its exit status and the first line of its message.

    The message is shortened and local paths in it are replaced, so findings do not
    carry the workspace layout or long source excerpts (``CommandResult.failure_reason``
    quotes stderr as it is). A tool a limit stopped is explained by ``failure_reason``,
    with the value of the limit it exceeded.

    Args:
        result: The result of the tex-fmt command.
        subject: Names the pass in the explanation.
        limits: The limits the command ran under.

    Returns:
        The explanation.
    """
    if result.returncode and result.within_limits:
        first_line = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
        message = _ABSOLUTE_PATH.sub("<path>", first_line)[:_MAX_FORMATTER_MESSAGE_CHARS]
        return f"{subject} failed with exit status {result.returncode}" + (
            f": {message}" if message else ""
        )
    return result.failure_reason(subject, limits) or f"{subject} did not complete"


async def _tex_fmt(
    context: FormatterContext, command: list[str], workspace: Path, subject: str
) -> str:
    """Run one tex-fmt pass over the workspace copy, with the blank-line policy applied.

    Args:
        context: What every file's formatting shares.
        command: The tex-fmt command line.
        workspace: The file's private workspace.
        subject: Names the pass in the error raised when tex-fmt does not complete.

    Returns:
        The formatted text.

    Raises:
        PreparationError: tex-fmt did not complete normally.
    """
    result = await context.runner.run(
        command, cwd=workspace, workspace=workspace, memory_mb=_FORMATTER_MEMORY_MB
    )
    if not result.completed_normally:
        raise PreparationError(_formatter_failure(result, subject, context.runner.limits))
    if context.options.blank_lines == "collapse":
        return _collapse_blank_lines(result.stdout)
    return result.stdout


class _Passes(NamedTuple):
    """The outcome of formatting a file twice.

    Attributes:
        text: The first pass's output, after the blank-line policy.
        stable: Whether a second pass left it unchanged.
    """

    text: str
    stable: bool


async def _run_passes(job: FormatJob, context: FormatterContext, original: bytes) -> _Passes:
    """Format a private copy of the file twice; the second pass checks the first is stable.

    Args:
        job: The file to format.
        context: What every file's formatting shares.
        original: The file's bytes.

    Returns:
        The first pass's text, and whether the second pass left it unchanged.

    Raises:
        PreparationError: A tex-fmt pass did not complete normally.
    """
    job.workspace.mkdir(parents=True, exist_ok=False)
    target = job.workspace / job.path.name
    target.write_bytes(original)
    options = context.options
    command = [
        context.executable,
        "--noconfig",
        "--print",
        "--wraplen",
        str(options.wrap_length),
        "--tabsize",
        str(options.tab_size),
        f"./{job.path.name}",
    ]
    updated_text = await _tex_fmt(context, command, job.workspace, "tex-fmt")
    updated = updated_text.encode("utf-8")
    target.write_bytes(updated)
    repeated_text = await _tex_fmt(context, command, job.workspace, "tex-fmt's second pass")
    return _Passes(updated_text, repeated_text.encode("utf-8") == updated)


def _read_original(job: FormatJob) -> bytes:
    """Read the file's bytes, which must not have grown since it was admitted.

    Returns:
        The file's bytes.

    Raises:
        PreparationError: The file is larger than when it was admitted.
    """
    with job.path.open("rb") as stream:
        original = stream.read(job.size + 1)
    if len(original) > job.size:
        raise PreparationError("Source size changed while formatting was being scheduled")
    return original


def _unprotectable_findings(relative: str, options: FormattingOptions) -> list[Finding]:
    """List the findings for a file whose protected regions cannot be delimited."""
    findings = [
        _unformattable(
            "source.format_protected",
            "An unclosed literal or formatter-protected region "
            "prevents a safe formatting proposal.",
            relative,
        )
    ]
    if options.blank_lines != "formatter":
        findings.append(
            _unformattable(
                "source.format_blank_lines",
                "Blank-line policy cannot be evaluated across an unclosed protected region.",
                relative,
            )
        )
    return findings


async def _format_file(job: FormatJob, context: FormatterContext) -> FormattingResult:
    """Format one file and judge the result.

    A file that is not UTF-8, or whose protected regions cannot be delimited, is not
    formatted. A proposal is kept only when it is stable, leaves protected regions
    unchanged, satisfies the blank-line policy and differs from the original.

    Args:
        job: The file to format.
        context: What every file's formatting shares.

    Returns:
        The file's proposal, if any, and its findings.

    Raises:
        PreparationError: tex-fmt fails, or the file grew after it was admitted.
    """
    result = FormattingResult()
    relative, options = job.relative, context.options
    original = _read_original(job)
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        result.findings.append(
            _unformattable(
                "source.format_encoding", "Formatting currently requires UTF-8 source", relative
            )
        )
        return result
    protected = protected_spans(text)
    if protected.uncertain:
        result.findings.extend(_unprotectable_findings(relative, options))
        return result
    passes = await _run_passes(job, context, original)
    if not passes.stable:
        result.findings.append(
            Finding(
                "source.format_idempotence",
                "A second tex-fmt pass produced a different result",
                severity="error",
                status="failed",
                path=relative,
            )
        )
        return result
    if not _accept_protected_regions(text, protected.spans, passes.text, relative, result):
        return result
    if not _accept_blank_lines(text, passes.text, options, relative, result):
        return result
    updated = passes.text.encode("utf-8")
    if original != updated:
        result.contents[relative] = updated
        result.originals[relative] = original
        result.findings.append(
            Finding(
                "source.formatting",
                "File differs from the configured tex-fmt style.",
                severity="warning",
                status="failed",
                path=relative,
            )
        )
        result.changes.append(
            Change(
                relative,
                kind=ChangeKind.FORMAT,
                reason="Apply explicitly configured tex-fmt formatting",
                diff=unified_diff_text(relative, text, passes.text),
            )
        )
    return result


def _accept_protected_regions(
    text: str,
    regions: list[tuple[int, int]],
    updated_text: str,
    relative: str,
    result: FormattingResult,
) -> bool:
    """Record whether protected literal regions survived formatting unchanged.

    Nothing is recorded when neither text has a protected region and the formatted
    text's regions are not uncertain.

    Args:
        text: The original text.
        regions: The protected spans of ``text``.
        updated_text: The formatted text.
        relative: The file's name in the project.
        result: Receives the finding.

    Returns:
        Whether the regions survived unchanged.
    """
    proposed = protected_spans(updated_text)
    unchanged = not proposed.uncertain and [text[start:end] for start, end in regions] == [
        updated_text[start:end] for start, end in proposed.spans
    ]
    if regions or proposed.spans or proposed.uncertain:
        result.findings.append(
            Finding(
                "source.format_protected",
                "Protected literal regions, directives and licenses are unchanged."
                if unchanged
                else "Formatter changed a protected literal region, directive "
                "or license block; the proposal was rejected.",
                severity="info" if unchanged else "error",
                status="passed" if unchanged else "failed",
                path=relative,
            )
        )
    return unchanged


def _accept_blank_lines(
    text: str,
    updated_text: str,
    options: FormattingOptions,
    relative: str,
    result: FormattingResult,
) -> bool:
    """Record whether the configured blank-line policy holds for the proposal.

    The ``formatter`` policy always holds and records nothing.

    Args:
        text: The original text.
        updated_text: The formatted text.
        options: The formatting options, which name the policy.
        relative: The file's name in the project.
        result: Receives the finding.

    Returns:
        Whether the policy holds.
    """
    if options.blank_lines == "formatter":
        return True
    satisfied = options.blank_lines == "collapse" or (
        _paragraph_separators(text) == _paragraph_separators(updated_text)
    )
    result.findings.append(
        Finding(
            "source.format_blank_lines",
            f"Blank-line policy '{options.blank_lines}' is satisfied."
            if satisfied
            else "Formatter changed source paragraph separators under the preserve policy.",
            severity="info" if satisfied else "error",
            status="passed" if satisfied else "failed",
            path=relative,
        )
    )
    return satisfied


def _exclusion_finding(
    files: list[_SourceFile], excluded: set[str], patterns: tuple[str, ...]
) -> Finding:
    """Report whether every exclusion pattern matched a source file, and which files it removed.

    Args:
        files: Every source file before the exclusions.
        excluded: The names of the files the patterns removed.
        patterns: The exclusion patterns.

    Returns:
        A passed finding, or a warning that names the patterns that match nothing.
    """
    unmatched = [
        pattern
        for pattern in patterns
        if not any(fnmatch.fnmatchcase(file.relative, pattern) for file in files)
    ]
    return Finding(
        "source.format_exclusions",
        "Formatting exclusions matched the selected source files."
        if not unmatched
        else "Some formatting exclusion patterns match no source file.",
        severity="info" if not unmatched else "warning",
        status="passed" if not unmatched else "failed",
        details={"excluded": sorted(excluded), "unmatched_patterns": unmatched},
    )


class _Selection(NamedTuple):
    """The source files to format, and the finding about the exclusions.

    Attributes:
        files: The files no exclusion pattern removes.
        exclusion: The finding about the exclusion patterns, or ``None`` without patterns.
    """

    files: list[_SourceFile]
    exclusion: Finding | None


def _select_sources(source: Path, options: FormattingOptions) -> _Selection:
    """Select the TeX documents under ``source`` that no exclusion pattern removes.

    The exclusion finding is present exactly when the options list patterns.
    """
    everything = [
        _SourceFile(path, path.relative_to(source).as_posix())
        for path in sorted(source.rglob("*"))
        if path.is_file() and path.suffix.lower() in ROOT_SUFFIXES
    ]
    excluded = {
        file.relative
        for file in everything
        if any(fnmatch.fnmatchcase(file.relative, pattern) for pattern in options.exclude)
    }
    exclusion = (
        _exclusion_finding(everything, excluded, options.exclude) if options.exclude else None
    )
    return _Selection([file for file in everything if file.relative not in excluded], exclusion)


def _unusable_selection(files: list[_SourceFile]) -> Finding | None:
    """Report what stops formatting: a linked source, or nothing left to format.

    Returns:
        The finding, or ``None`` when formatting can go ahead.
    """
    linked = [file.relative for file in files if file.path.is_symlink()]
    if linked:
        return Finding(
            "source.format_exclusions",
            "Linked source files cannot be read safely for formatting.",
            severity="error",
            status="inconclusive",
            details={"linked": linked},
        )
    if not files:
        return Finding(
            "source.formatting",
            "No source files are selected after formatting exclusions.",
            severity="info",
            status="skipped",
        )
    return None


def _failure_text(error: OSError | PreparationError) -> str:
    """Describe what went wrong with one file.

    An OS error is described by its reason only, without the temporary workspace it would
    name.
    """
    if isinstance(error, OSError):
        cause = (error.strerror or type(error).__name__).lower()
        return f"The file could not be read or written while formatting ({cause})"
    return str(error)


async def _format_admitted(
    file: _SourceFile, work: Path, context: FormatterContext, budget: ResourceBudget | None
) -> FormattingResult:
    """Format one file within the size limit and, when a budget is set, its memory lease.

    Args:
        file: The file to format.
        work: The directory that holds every file's workspace.
        context: What every file's formatting shares.
        budget: The resource budget that grants the memory lease, or ``None``.

    Returns:
        The file's result; a file too large, or a failure, becomes an inconclusive error.
    """
    try:
        size = file.path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            return FormattingResult(
                findings=[
                    _unformattable(
                        "source.format_size",
                        f"Source exceeds the {MAX_SOURCE_BYTES // MIB} MiB formatting limit",
                        file.relative,
                    )
                ]
            )
        reservation = (
            budget.lease(
                f"format:{file.relative}", memory_mb=_format_memory_mb(size, context.runner)
            )
            if budget is not None
            else nullcontext()
        )
        async with reservation:
            # File paths are leaves of the source tree, so these workspace
            # directories cannot contain another file worker's workspace.
            job = FormatJob(file.path, file.relative, work / "files" / file.relative, size)
            return await _format_file(job, context)
    except (OSError, PreparationError) as error:
        return FormattingResult(
            findings=[
                _unformattable("source.format_execution", _failure_text(error), file.relative)
            ]
        )


async def _format_all(
    files: list[_SourceFile], work: Path, context: FormatterContext, budget: ResourceBudget | None
) -> list[FormattingResult]:
    """Format every file, in parallel up to the budget's CPU slots, else one after another."""

    async def worker(file: _SourceFile) -> FormattingResult:
        """Format one file."""
        return await _format_admitted(file, work, context, budget)

    if budget is None:
        return [await worker(file) for file in files]
    return await bounded_map(files, worker, limit=budget.cpu_slots)


def _summarize(result: FormattingResult, completed: list[FormattingResult]) -> None:
    """Merge the per-file results into ``result``; a file that failed discards every proposal."""
    for item in completed:
        result.contents.update(item.contents)
        result.originals.update(item.originals)
        result.changes.extend(item.changes)
        result.findings.extend(item.findings)
    incomplete = has_blockers(result.findings)
    if incomplete or not result.contents:
        result.findings.append(
            Finding(
                "source.formatting",
                "Formatting check could not complete for all files."
                if incomplete
                else "All inspected source files already match the configured style.",
                severity="error" if incomplete else "info",
                status="inconclusive" if incomplete else "passed",
            )
        )
    if incomplete:
        result.contents.clear()
        result.originals.clear()
        result.changes.clear()
    result.findings.sort(key=lambda item: (item.path or "", item.rule, item.message))


async def format_project(
    source: Path,
    work: Path,
    runner: CommandRunner,
    options: FormattingOptions,
    *,
    budget: ResourceBudget | None = None,
) -> FormattingResult:
    """Propose tex-fmt formatting for the TeX documents under ``source``; write nothing there.

    A tex-fmt that is missing, a linked source, or a failure on any file yields findings
    and no proposals.

    Args:
        source: The project directory.
        work: A directory that must not exist: it holds the tool-version probe and one
            private workspace per file.
        runner: Runs tex-fmt.
        options: The formatting options.
        budget: With a budget, files are formatted in parallel, each holding a memory
            lease; without one they run in turn.

    Returns:
        The proposals with their original bytes, the changes, the findings and the
        tex-fmt version.
    """
    result = FormattingResult()
    selection = _select_sources(source, options)
    if selection.exclusion is not None:
        result.findings.append(selection.exclusion)
    stop = _unusable_selection(selection.files)
    if stop is not None:
        result.findings.append(stop)
        return result
    executable = shutil.which("tex-fmt")
    if executable is None:
        result.findings.append(
            Finding(
                "source.formatter_available",
                "tex-fmt is not installed",
                severity="error",
                status="inconclusive",
                suggestion="Install tex-fmt separately or disable formatting.",
            )
        )
        return result
    work.mkdir(parents=True, exist_ok=False)
    version_workspace = work / "version"
    version_workspace.mkdir()
    try:
        version = await runner.tool_version([executable, "--version"], workspace=version_workspace)
    except PreparationError as error:
        result.findings.append(
            Finding("source.format_execution", str(error), severity="error", status="inconclusive")
        )
        return result
    result.tools["tex-fmt"] = version.to_report()
    context = FormatterContext(executable, runner, options)
    _summarize(result, await _format_all(selection.files, work, context, budget))
    return result
