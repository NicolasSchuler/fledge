"""Explicit tex-fmt adapter; computes patches without modifying the input."""

from __future__ import annotations

import difflib
import fnmatch
import re
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Change, Finding, PreparationError, has_blockers
from .runtime import ToolRunner
from .scheduler import ResourceBudget, bounded_map
from .source_transform import protected_spans

if TYPE_CHECKING:
    from .config import Settings

_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_FORMATTER_MEMORY_MB = 128


@dataclass(frozen=True)
class FormattingOptions:
    exclude: tuple[str, ...] = ()
    blank_lines: str = "formatter"

    def __post_init__(self) -> None:
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


def _blank_line_policy(text: str, *, collapse: bool) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Normalize repeated blank lines outside protected spans, or fingerprint runs.

    Paragraph anchors ignore whitespace so ordinary line wrapping is allowed,
    but moving a blank line across non-whitespace source text is detectable.
    """
    spans, _ = protected_spans(text)
    parts: list[str] = []
    signature: list[tuple[str, int]] = []
    anchor: list[str] = []
    count = offset = 0
    for line in text.splitlines(keepends=True):
        protected = any(start < offset + len(line) and end > offset for start, end in spans)
        blank = not line.strip() and not protected
        if blank:
            count += 1
            if not collapse or count == 1:
                parts.append(line)
        else:
            if count:
                signature.append(("".join(anchor), count))
                anchor.clear()
                count = 0
            anchor.append(re.sub(r"\s+", "", line))
            parts.append(line)
        offset += len(line)
    if count:
        signature.append(("".join(anchor), count))
    return "".join(parts), tuple(signature)


@dataclass
class FormattingResult:
    contents: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tools: dict[str, object] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)


def _format_memory_mb(source_bytes: int, runner: ToolRunner) -> int:
    # Reserve tool headroom plus decoded strings, split lines and diff buffers.
    # This is a conservative admission estimate, not a measured memory peak.
    output_bytes = runner.limits.max_output_bytes
    return _FORMATTER_MEMORY_MB + (16 * (source_bytes + output_bytes) + 1024 * 1024 - 1) // (
        1024 * 1024
    )


async def _format_file(
    path: Path,
    relative: str,
    workspace: Path,
    source_bytes: int,
    executable: str,
    runner: ToolRunner,
    settings: Settings,
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
            Finding(
                "source.format_encoding",
                "Formatting currently requires UTF-8 source",
                "error",
                "inconclusive",
                path=relative,
            )
        )
        return result
    options = settings.formatting_options
    regions, incomplete = protected_spans(text)
    if incomplete:
        result.findings.append(
            Finding(
                "source.format_protected",
                "An unclosed literal or formatter-protected region "
                "prevents a safe formatting proposal.",
                "error",
                "inconclusive",
                path=relative,
            )
        )
        if options.blank_lines != "formatter":
            result.findings.append(
                Finding(
                    "source.format_blank_lines",
                    "Blank-line policy cannot be evaluated across an unclosed protected region.",
                    "error",
                    "inconclusive",
                    path=relative,
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
        str(settings.wrap_length),
        "--tabsize",
        str(settings.tab_size),
        f"./{path.name}",
    ]
    formatted = await runner.run(
        command, cwd=workspace, workspace=workspace, memory_mb=_FORMATTER_MEMORY_MB
    )
    if (
        formatted.returncode
        or formatted.timed_out
        or formatted.output_limited
        or formatted.resource_exceeded
    ):
        raise PreparationError(f"tex-fmt did not complete: {formatted.stderr[:2000]}")
    updated_text = formatted.stdout
    if options.blank_lines == "collapse":
        updated_text, _ = _blank_line_policy(updated_text, collapse=True)
    updated = updated_text.encode("utf-8")
    target.write_bytes(updated)
    repeated = await runner.run(
        command, cwd=workspace, workspace=workspace, memory_mb=_FORMATTER_MEMORY_MB
    )
    if (
        repeated.returncode
        or repeated.timed_out
        or repeated.output_limited
        or repeated.resource_exceeded
    ):
        raise PreparationError("tex-fmt failed its second formatting pass")
    repeated_text = repeated.stdout
    if options.blank_lines == "collapse":
        repeated_text, _ = _blank_line_policy(repeated_text, collapse=True)
    if repeated_text.encode("utf-8") != updated:
        result.findings.append(
            Finding(
                "source.format_idempotence",
                "A second tex-fmt pass produced a different result",
                "error",
                "failed",
                path=relative,
            )
        )
        return result
    proposed_regions, proposed_incomplete = protected_spans(updated_text)
    protected_unchanged = not proposed_incomplete and [
        text[start:end] for start, end in regions
    ] == [updated_text[start:end] for start, end in proposed_regions]
    if regions or proposed_regions or proposed_incomplete:
        result.findings.append(
            Finding(
                "source.format_protected",
                "Protected literal regions, directives and licenses are unchanged."
                if protected_unchanged
                else "Formatter changed a protected literal region, directive "
                "or license block; the proposal was rejected.",
                "info" if protected_unchanged else "error",
                "passed" if protected_unchanged else "failed",
                path=relative,
            )
        )
    if not protected_unchanged:
        return result
    if options.blank_lines != "formatter":
        blank_ok = options.blank_lines == "collapse" or (
            _blank_line_policy(text, collapse=False)[1]
            == _blank_line_policy(updated_text, collapse=False)[1]
        )
        result.findings.append(
            Finding(
                "source.format_blank_lines",
                f"Blank-line policy '{options.blank_lines}' is satisfied."
                if blank_ok
                else "Formatter changed source paragraph separators under the preserve policy.",
                "info" if blank_ok else "error",
                "passed" if blank_ok else "failed",
                path=relative,
            )
        )
        if not blank_ok:
            return result
    if original != updated:
        result.contents[relative] = updated
        result.originals[relative] = original
        result.findings.append(
            Finding(
                "source.formatting",
                "File differs from the configured tex-fmt style.",
                "warning",
                "failed",
                path=relative,
            )
        )
        result.changes.append(
            Change(
                relative,
                "format",
                "Apply explicitly configured tex-fmt formatting",
                diff="".join(
                    difflib.unified_diff(
                        text.splitlines(keepends=True),
                        updated_text.splitlines(keepends=True),
                        fromfile=relative,
                        tofile=relative,
                    )
                ),
            )
        )
    return result


async def format_project(
    source: Path,
    work: Path,
    runner: ToolRunner,
    settings: Settings,
    *,
    budget: ResourceBudget | None = None,
) -> FormattingResult:
    result = FormattingResult()
    options = settings.formatting_options
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
                "info" if not unmatched else "warning",
                "passed" if not unmatched else "failed",
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
                "error",
                "inconclusive",
                details={"linked": linked},
            )
        )
        return result
    if not paths:
        result.findings.append(
            Finding(
                "source.formatting",
                "No source files are selected after formatting exclusions.",
                "info",
                "skipped",
            )
        )
        return result
    executable = shutil.which("tex-fmt")
    if executable is None:
        result.findings.append(
            Finding(
                "source.formatter_available",
                "tex-fmt is not installed",
                "error",
                "inconclusive",
                suggestion="Install tex-fmt separately or disable formatting.",
            )
        )
        return result
    work.mkdir(parents=True, exist_ok=False)
    version_workspace = work / "version"
    version_workspace.mkdir()
    try:
        result.tools["tex-fmt"] = await runner.tool_version(
            [executable, "--version"], workspace=version_workspace
        )
    except PreparationError as error:
        result.findings.append(
            Finding("source.format_execution", str(error), "error", "inconclusive")
        )
        return result

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
                            "error",
                            "inconclusive",
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
                    path, relative, work / "files" / relative, size, executable, runner, settings
                )
        except (OSError, PreparationError) as error:
            return FormattingResult(
                findings=[
                    Finding(
                        "source.format_execution",
                        str(error),
                        "error",
                        "inconclusive",
                        path=relative,
                    )
                ]
            )

    completed = (
        await bounded_map(paths, worker, limit=budget.jobs)
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
                "error" if incomplete else "info",
                "inconclusive" if incomplete else "passed",
            )
        )
    if incomplete:
        result.contents.clear()
        result.originals.clear()
        result.changes.clear()
    result.findings.sort(key=lambda item: (item.path or "", item.rule, item.message))
    return result
