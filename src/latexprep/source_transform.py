"""Opt-in, copy-only source transformations for a bounded literal TeX subset.

Every proposal still requires the workflow's baseline/prepared PDF comparison.
Token expansion, arbitrary catcodes and semantic equivalence are not inferred.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import Change, Finding, PreparationError, Status, has_blockers
from .source import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    _commands,
    _Inspection,
    _items,
    _ParseLimit,
    mask_literals,
    source_regions,
)

_DIRECTIVE = re.compile(
    r"^%\s*(?:!|&|\*|</?|(?:tex|latex|bib|biber|arara|encoding|coding|spell|"
    r"fmt|tex-fmt|fledge|latex-prep|region|endregion)\b)",
    re.I,
)
_LICENSE = re.compile(
    r"\b(?:copyright|license|licence|SPDX|LPPL|public domain|all rights reserved)\b", re.I
)
_PRIVATE = re.compile(r"\b(?:TODO|FIXME|XXX|private|internal|confidential|note to self)\b", re.I)
_FORMAT_MARKER = re.compile(r"^%\s*(?:tex-fmt|fmt|fledge|latex-prep)\s*:\s*(off|on)\b", re.I)
# Comment editing stays inside authored document sources; class, style and
# generated template internals are copied verbatim.
_EDITABLE_SUFFIXES = {".tex", ".ltx", ".latex"}


def _relative(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(character in value for character in "\\\x00\r\n")
        and not re.match(r"^[A-Za-z]:", value)
    )


@dataclass(frozen=True)
class SourceTransformOptions:
    comment_policy: str = "retain"
    merge_inputs: bool = False
    inline_bibliography: str | None = None
    filename_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.comment_policy not in {"retain", "private", "all"}:
            raise PreparationError("comment_policy must be retain, private or all")
        if type(self.merge_inputs) is not bool:
            raise PreparationError("merge_inputs must be a boolean")
        if self.inline_bibliography is not None and (
            not isinstance(self.inline_bibliography, str)
            or not _relative(self.inline_bibliography)
            or PurePosixPath(self.inline_bibliography).suffix != ".bbl"
        ):
            raise PreparationError("inline_bibliography must be a project-relative .bbl path")
        if not isinstance(self.filename_overrides, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) and _relative(value) for value in pair)
            for pair in self.filename_overrides
        ):
            raise PreparationError(
                "filename_overrides must map project paths to relative filenames"
            )
        if len({pair[0] for pair in self.filename_overrides}) != len(self.filename_overrides):
            raise PreparationError("filename_overrides must use unique source paths")


@dataclass
class SourceTransformPlan:
    contents: dict[str, bytes] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    removals: set[str] = field(default_factory=set)
    removal_originals: dict[str, bytes] = field(default_factory=dict)


def protected_spans(text: str) -> tuple[list[tuple[int, int]], bool]:
    """Literal regions, formatter-off ranges and directive/license comment blocks."""
    regions, incomplete = source_regions(text)
    spans = [(start, end) for start, end, kind in regions if kind == "literal"]
    comments = [(start, end) for start, end, kind in regions if kind == "comment"]
    off: int | None = None
    uncertain = bool(incomplete)
    for start, end in comments:
        marker = _FORMAT_MARKER.match(text[start:end])
        if marker:
            if marker[1].lower() == "off":
                if off is not None:
                    uncertain = True
                off = start
            elif off is None:
                uncertain = True
            else:
                spans.append((off, end))
                off = None
        if _DIRECTIVE.match(text[start:end]):
            spans.append((start, end))
    if off is not None:
        spans.append((off, len(text)))
        uncertain = True
    # Keep whole contiguous comment blocks when any line carries licensing text.
    blocks: list[list[tuple[int, int]]] = []
    for comment in comments:
        if blocks and text[blocks[-1][-1][1] : comment[0]].strip() == "":
            blocks[-1].append(comment)
        else:
            blocks.append([comment])
    for block in blocks:
        if any(_LICENSE.search(text[start:end]) for start, end in block):
            spans.append((block[0][0], block[-1][1]))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged, uncertain


def _remove_comments(text: str, policy: str) -> tuple[str, int, bool]:
    regions, incomplete = source_regions(text)
    protected, uncertain = protected_spans(text)
    edits: list[tuple[int, int]] = []
    for start, end, kind in regions:
        if kind != "comment" or any(left <= start < right for left, right in protected):
            continue
        if policy == "private" and not _PRIVATE.search(text[start:end]):
            continue
        # Retain '%' and its original line terminator: removing either may join
        # tokens, introduce a space, or turn a comment-only line into a paragraph.
        if end > start + 1:
            edits.append((start + 1, end))
    for start, end in reversed(edits):
        text = text[:start] + text[end:]
    return text, len(edits), bool(incomplete) or uncertain


def _problem(
    plan: SourceTransformPlan,
    rule: str,
    message: str,
    path: str | None = None,
    *,
    status: Status = "inconclusive",
) -> None:
    plan.findings.append(Finding(rule, message, "error", status, path=path))


def _command_comments(text: str) -> str:
    regions, _ = source_regions(text)
    comments = []
    for start, end, kind in regions:
        if kind == "comment":
            newline = "\r\n" if text[end : end + 2] == "\r\n" else "\n"
            comments.append(text[start:end] + newline)
    return "".join(comments)


def _merge_inputs(
    inspection: _Inspection, texts: dict[str, str], plan: SourceTransformPlan
) -> None:
    rule = "source.merge_inputs"
    if inspection.uncertainties or has_blockers(inspection.findings):
        _problem(
            plan,
            rule,
            "Input merging requires a complete unambiguous literal dependency graph.",
            status="failed" if has_blockers(inspection.findings) else "inconclusive",
        )
        return
    if any(root != inspection.main for root in inspection.roots):
        _problem(plan, rule, "Input merging supports one document root per package.")
        return
    forbidden = {
        "include",
        "import",
        "subimport",
        "inputfrom",
        "subinputfrom",
        "subfile",
        "subfileinclude",
        "endinput",
        "end",
        "jobname",
        "inputlineno",
    }
    expanded_bytes = 0
    merged: set[str] = set()

    def expand(name: str, stack: tuple[str, ...]) -> str:
        nonlocal expanded_bytes
        source = inspection.sources[name]
        text = texts[name]
        if has_blockers(plan.findings):
            return text
        # Comments may already have changed offsets, so parse this proposal.
        commands = _commands(mask_literals(text))
        edits: list[tuple[int, int, str]] = []
        for command in commands:
            if has_blockers(plan.findings):
                break
            if (
                command.name in forbidden
                and not (
                    command.name == "end"
                    and name == inspection.main
                    and command.arguments
                    and command.arguments[0].value == "document"
                )
                and not (
                    command.name == "end"
                    and command.arguments
                    and command.arguments[0].value != "document"
                )
            ):
                _problem(
                    plan,
                    rule,
                    f"\\{command.name} has file or execution semantics "
                    "outside standalone input merging.",
                    name,
                )
                continue
            if command.name != "input":
                continue
            if not command.arguments or command.depth:
                _problem(plan, rule, "Only direct literal input commands can be merged.", name)
                continue
            argument = command.arguments[0]
            original_command = next(
                (c for c in source.commands if c.name == "input" and c.line == command.line), None
            )
            if original_command is None or not original_command.arguments:
                _problem(
                    plan, rule, "Input position could not be reconciled after comment edits.", name
                )
                continue
            original_arg = _items(original_command.arguments[0], False)[0]
            reference = inspection.references.get((name, original_arg.start, original_arg.end))
            if reference is None or reference[0] in stack:
                _problem(plan, rule, "Input target or execution order is unresolved.", name)
                continue
            target = reference[0]
            if target not in texts or not texts[target].endswith(("\n", "\r")):
                _problem(
                    plan, rule, "Merged input files must be UTF-8 and newline-terminated.", target
                )
                continue
            start = text.rfind("\n", 0, command.start) + 1
            end = text.find("\n", argument.end)
            end = len(text) if end < 0 else end + 1
            command_end = argument.end + (text[argument.end : argument.end + 1] == "}")
            if text[start : command.start].strip() or mask_literals(text[command_end:end]).strip():
                _problem(
                    plan,
                    rule,
                    "Only input commands occupying their own source line can be merged.",
                    name,
                )
                continue
            leading_comments = _command_comments(text[start:command_end])
            trailing_comments = _command_comments(text[command_end:end])
            replacement = leading_comments + expand(target, (*stack, name)) + trailing_comments
            expanded_bytes += len(replacement.encode("utf-8"))
            if expanded_bytes > MAX_TOTAL_SOURCE_BYTES:
                _problem(plan, rule, "Merged sources exceed the 32 MiB expansion limit.", name)
                return text
            edits.append((start, end, replacement))
            merged.add(target)
        for start, end, replacement in reversed(edits):
            text = text[:start] + replacement + text[end:]
        return text

    if inspection.main is None:
        return
    texts[inspection.main] = expand(inspection.main, ())
    if any(
        target in merged and command != "input"
        for target, command in inspection.references.values()
    ):
        _problem(plan, rule, "A merged input is also used by a different file-reading command.")
    if not has_blockers(plan.findings):
        plan.removals.update(merged)
        plan.findings.append(
            Finding(
                rule,
                f"Planned recursive merging of {len(merged)} literal input files.",
                "info",
                "passed",
                details={"files": sorted(merged)},
            )
        )


def _inline_bibliography(
    inspection: _Inspection, texts: dict[str, str], path: str, plan: SourceTransformPlan
) -> None:
    rule = "source.inline_bibliography"
    if inspection.uncertainties or has_blockers(inspection.findings):
        _problem(plan, rule, "Bibliography inlining requires an unambiguous literal source graph.")
        return
    if path not in inspection.sources or path not in texts:
        _problem(
            plan,
            rule,
            "The selected generated bibliography is missing or unreadable.",
            path,
            status="failed",
        )
        return
    text = texts[path]
    commands = _commands(mask_literals(text))
    starts = [
        c
        for c in commands
        if c.name == "begin" and c.arguments and c.arguments[0].value == "thebibliography"
    ]
    ends = [
        c
        for c in commands
        if c.name == "end" and c.arguments and c.arguments[0].value == "thebibliography"
    ]
    if (
        len(starts) != 1
        or len(ends) != 1
        or starts[0].start >= ends[0].start
        or any(
            c.name in {"input", "include", "addbibresource", "entry", "refsection", "endinput"}
            for c in commands
        )
    ):
        _problem(
            plan,
            rule,
            "Inlining requires a standalone BibTeX thebibliography environment; "
            "biblatex data and nested file inputs are unsupported.",
            path,
        )
        return
    locations = []
    for name in inspection.visited - plan.removals:
        for command in _commands(mask_literals(texts[name])):
            if command.name in {"addbibresource", "printbibliography"}:
                _problem(plan, rule, "Biblatex bibliography inlining is not supported.", name)
            if command.name == "bibliography":
                locations.append((name, command))
    if len(locations) != 1 or locations[0][1].depth:
        _problem(plan, rule, "Inlining requires exactly one direct literal bibliography command.")
        return
    name, command = locations[0]
    if not command.arguments:
        _problem(plan, rule, "The bibliography command has no literal database argument.", name)
        return
    start = texts[name].rfind("\n", 0, command.start) + 1
    end = texts[name].find("\n", command.arguments[0].end)
    end = len(texts[name]) if end < 0 else end + 1
    command_end = command.arguments[0].end + 1
    if (
        texts[name][start : command.start].strip()
        or mask_literals(texts[name][command_end:end]).strip()
    ):
        _problem(plan, rule, "The bibliography command must occupy its own source line.", name)
        return
    comments = _command_comments(texts[name][start:command_end])
    trailing = _command_comments(texts[name][command_end:end])
    texts[name] = (
        texts[name][:start] + comments + text.rstrip("\r\n") + "\n" + trailing + texts[name][end:]
    )
    plan.findings.append(
        Finding(
            rule,
            "Planned literal generated-bibliography inlining.",
            "info",
            "passed",
            path=name,
            details={"bibliography": path},
        )
    )


def plan_source_transforms(
    root: Path, main: str, options: SourceTransformOptions
) -> SourceTransformPlan:
    """Return proposed bytes/removals/diffs without writing or deleting any file."""
    plan = SourceTransformPlan()
    if (
        options.comment_policy == "retain"
        and not options.merge_inputs
        and not options.inline_bibliography
    ):
        return plan
    inspection = _Inspection(root, main)
    if inspection.main is None:
        plan.findings.extend(inspection.findings)
        return plan
    texts = {name: source.text for name, source in inspection.sources.items()}
    if options.comment_policy != "retain":
        if inspection._unparsed - set(inspection.sources) or any(
            finding.rule == "source-link" for finding in inspection.uncertainties
        ):
            _problem(
                plan, "source.comment_removal", "Some source files could not be parsed safely."
            )
        for name, text in list(texts.items()):
            if PurePosixPath(name).suffix.lower() not in _EDITABLE_SUFFIXES:
                continue
            commands = inspection.sources[name].commands
            if any(c.name in {"catcode", "scantokens", "csname"} for c in commands):
                _problem(
                    plan,
                    "source.comment_removal",
                    "Dynamic lexical behavior prevents comment removal.",
                    name,
                )
                continue
            updated, count, uncertain = _remove_comments(text, options.comment_policy)
            if uncertain:
                _problem(
                    plan,
                    "source.comment_removal",
                    "An unclosed literal or protected region prevents comment removal.",
                    name,
                )
            else:
                texts[name] = updated
                plan.findings.append(
                    Finding(
                        "source.comment_removal",
                        f"Planned removal of {count} comment bodies; percent signs, "
                        "line terminators, directives and license blocks are retained.",
                        "info",
                        "passed",
                        path=name,
                        details={"removed_comments": count},
                    )
                )
    try:
        if options.merge_inputs:
            _merge_inputs(inspection, texts, plan)
        if options.inline_bibliography:
            _inline_bibliography(inspection, texts, options.inline_bibliography, plan)
    except (_ParseLimit, RecursionError):
        _problem(
            plan,
            "source.merge_inputs" if options.merge_inputs else "source.inline_bibliography",
            "Transformation exceeded the bounded literal parsing limits.",
        )
    if has_blockers(plan.findings):
        plan.removals.clear()
        return plan
    for name in sorted(plan.removals):
        source = inspection.sources[name]
        plan.removal_originals[name] = (
            b"\xef\xbb\xbf" if source.bom else b""
        ) + source.text.encode("utf-8")
        plan.changes.append(
            Change(name, "remove", "Input contents were merged into the main source copy.")
        )
    for name, text in texts.items():
        if name in plan.removals or text == inspection.sources[name].text:
            continue
        encoded = (b"\xef\xbb\xbf" if inspection.sources[name].bom else b"") + text.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            _problem(
                plan,
                "source.merge_inputs",
                "Transformed source exceeds the 8 MiB per-file limit.",
                name,
            )
            break
        plan.contents[name] = encoded
        original = inspection.sources[name]
        plan.originals[name] = (b"\xef\xbb\xbf" if original.bom else b"") + original.text.encode(
            "utf-8"
        )
        plan.changes.append(
            Change(
                name,
                "rewrite",
                "Apply explicitly selected source transformations.",
                diff="".join(
                    difflib.unified_diff(
                        inspection.sources[name].text.splitlines(keepends=True),
                        text.splitlines(keepends=True),
                        fromfile=name,
                        tofile=name,
                    )
                ),
            )
        )
    if has_blockers(plan.findings):
        plan.contents.clear()
        plan.originals.clear()
        plan.removals.clear()
        plan.removal_originals.clear()
        plan.changes.clear()
    return plan
