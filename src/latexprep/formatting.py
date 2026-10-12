"""Explicit tex-fmt adapter; computes patches without modifying the input."""

from __future__ import annotations

import fnmatch
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .file_safety import MIB
from .models import Change, ChangeKind, Finding, PreparationError, has_blockers
from .runtime import CommandResult, CommandRunner
from .scheduler import ResourceBudget, bounded_map
from .source_transform import protected_spans
from .text_edits import unified_diff_text

_MAX_SOURCE_BYTES = 8 * MIB
_FORMATTER_MEMORY_MB = 128
MIN_WRAP_LENGTH = 20
MAX_TAB_SIZE = 16
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FormattingOptions:
    exclude: tuple[str, ...] = ()
    blank_lines: str = "formatter"
    # Read from the top-level wrap_length and tab_size settings.
    wrap_length: int = 80
    tab_size: int = 2

    def __post_init__(self) -> None:
        for name in ("wrap_length", "tab_size"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise PreparationError(f"{name} must be a positive integer")
        if self.wrap_length < MIN_WRAP_LENGTH or self.tab_size > MAX_TAB_SIZE:
            raise PreparationError(
                f"wrap_length must be >={MIN_WRAP_LENGTH} and tab_size must be <={MAX_TAB_SIZE}"
            )
        if not isinstance(self.exclude, tuple) or any(
            not isinstance(pattern, str)
            or not pattern
            or pattern.startswith("/")
            or ".." in pattern.split("/")
            or any(c in pattern for c in "\\\x00\r\n")
            for pattern in self.exclude
        ):
            raise PreparationError("formatting exclusions must be project-relative glob patterns")
        if len(set(self.exclude)) != len(self.exclude):
            raise PreparationError("formatting exclusions must be unique")
        if self.blank_lines not in {"formatter", "preserve", "collapse"}:
            raise PreparationError("blank_lines must be formatter, preserve or collapse")


def _lines_with_blank_state(text: str) -> Iterator[tuple[str, bool]]:
    """Each line, and whether it is blank outside every protected span."""
    spans, _ = protected_spans(text)
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
    contents: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tools: dict[str, object] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)


def _format_memory_mb(source_bytes: int, runner: CommandRunner) -> int:
    # Reserve tool headroom plus decoded strings, split lines and diff buffers.
    # This is a conservative admission estimate, not a measured memory peak.
    output_bytes = runner.limits.max_output_bytes
    return _FORMATTER_MEMORY_MB + (16 * (source_bytes + output_bytes) + MIB - 1) // MIB


def _unformattable(rule: str, message: str, relative: str) -> Finding:
    return Finding(rule, message, severity="error", status="inconclusive", path=relative)


async def _tex_fmt(
    runner: CommandRunner,
    command: list[str],
    workspace: Path,
    options: FormattingOptions,
    incomplete: Callable[[CommandResult], str],
) -> str:
    """One tex-fmt pass over the workspace copy, with the blank-line policy applied."""
    result = await runner.run(
        command, cwd=workspace, workspace=workspace, memory_mb=_FORMATTER_MEMORY_MB
    )
    if not result.completed_normally:
        raise PreparationError(incomplete(result))
    if options.blank_lines == "collapse":
        return _collapse_blank_lines(result.stdout)
    return result.stdout


async def _format_file(
    path: Path,
    relative: str,
    workspace: Path,
    source_bytes: int,
    executable: str,
    runner: CommandRunner,
    options: FormattingOptions,
) -> FormattingResult:
    result = FormattingResult()
    with path.open("rb") as stream:
        original = stream.read(source_bytes + 1)
    if len(original) > source_bytes:
        raise PreparationError("Source size changed while formatting was being scheduled")
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        result.findings.append(
            _unformattable(
                "source.format_encoding", "Formatting currently requires UTF-8 source", relative
            )
        )
        return result
    regions, incomplete = protected_spans(text)
    if incomplete:
        result.findings.append(
            _unformattable(
                "source.format_protected",
                "An unclosed literal or formatter-protected region "
                "prevents a safe formatting proposal.",
                relative,
            )
        )
        if options.blank_lines != "formatter":
            result.findings.append(
                _unformattable(
                    "source.format_blank_lines",
                    "Blank-line policy cannot be evaluated across an unclosed protected region.",
                    relative,
                )
            )
        return result
    workspace.mkdir(parents=True, exist_ok=False)
    target = workspace / path.name
    target.write_bytes(original)
    command = [
        executable,
        "--noconfig",
        "--print",
        "--wraplen",
        str(options.wrap_length),
        "--tabsize",
        str(options.tab_size),
        f"./{path.name}",
    ]
    updated_text = await _tex_fmt(
        runner,
        command,
        workspace,
        options,
        lambda formatted: f"tex-fmt did not complete: {formatted.stderr[:2000]}",
    )
    updated = updated_text.encode("utf-8")
    target.write_bytes(updated)
    repeated_text = await _tex_fmt(
        runner,
        command,
        workspace,
        options,
        lambda _repeated: "tex-fmt failed its second formatting pass",
    )
    if repeated_text.encode("utf-8") != updated:
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
    if not _accept_protected_regions(text, regions, updated_text, relative, result):
        return result
    if not _accept_blank_lines(text, updated_text, options, relative, result):
        return result
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
                diff=unified_diff_text(relative, text, updated_text),
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
    """Record whether protected literal regions survived formatting unchanged."""
    proposed_regions, proposed_incomplete = protected_spans(updated_text)
    unchanged = not proposed_incomplete and [text[start:end] for start, end in regions] == [
        updated_text[start:end] for start, end in proposed_regions
    ]
    if regions or proposed_regions or proposed_incomplete:
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
    """Record whether the configured blank-line policy holds for the proposal."""
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


async def format_project(
    source: Path,
    work: Path,
    runner: CommandRunner,
    options: FormattingOptions,
    *,
    budget: ResourceBudget | None = None,
) -> FormattingResult:
    result = FormattingResult()
    all_paths = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix in {".tex", ".ltx", ".latex"}
    )
    excluded = {
        path.relative_to(source).as_posix()
        for path in all_paths
        if any(
            fnmatch.fnmatchcase(path.relative_to(source).as_posix(), pattern)
            for pattern in options.exclude
        )
    }
    if options.exclude:
        unmatched = [
            pattern
            for pattern in options.exclude
            if not any(
                fnmatch.fnmatchcase(path.relative_to(source).as_posix(), pattern)
                for path in all_paths
            )
        ]
        result.findings.append(
            Finding(
                "source.format_exclusions",
                "Formatting exclusions matched the selected source files."
                if not unmatched
                else "Some formatting exclusion patterns match no source file.",
                severity="info" if not unmatched else "warning",
                status="passed" if not unmatched else "failed",
                details={"excluded": sorted(excluded), "unmatched_patterns": unmatched},
            )
        )
    paths = [path for path in all_paths if path.relative_to(source).as_posix() not in excluded]
    linked = [path.relative_to(source).as_posix() for path in paths if path.is_symlink()]
    if linked:
        result.findings.append(
            Finding(
                "source.format_exclusions",
                "Linked source files cannot be read safely for formatting.",
                severity="error",
                status="inconclusive",
                details={"linked": linked},
            )
        )
        return result
    if not paths:
        result.findings.append(
            Finding(
                "source.formatting",
                "No source files are selected after formatting exclusions.",
                severity="info",
                status="skipped",
            )
        )
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

    async def worker(path: Path) -> FormattingResult:
        relative = path.relative_to(source).as_posix()
        try:
            size = path.stat().st_size
            if size > _MAX_SOURCE_BYTES:
                return FormattingResult(
                    findings=[
                        Finding(
                            "source.format_size",
                            "Source exceeds the 8 MiB formatting limit",
                            severity="error",
                            status="inconclusive",
                            path=relative,
                        )
                    ]
                )
            reservation = (
                budget.lease(f"format:{relative}", memory_mb=_format_memory_mb(size, runner))
                if budget is not None
                else nullcontext()
            )
            async with reservation:
                # File paths are leaves of the source tree, so these workspace
                # directories cannot contain another file worker's workspace.
                return await _format_file(
                    path, relative, work / "files" / relative, size, executable, runner, options
                )
        except (OSError, PreparationError) as error:
            return FormattingResult(
                findings=[
                    Finding(
                        "source.format_execution",
                        str(error),
                        severity="error",
                        status="inconclusive",
                        path=relative,
                    )
                ]
            )

    completed = (
        await bounded_map(paths, worker, limit=budget.cpu_slots)
        if budget is not None
        else [await worker(path) for path in paths]
    )
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
    return result
