"""Opt-in, copy-only source transformations for a bounded literal TeX subset.

Three transformations, each a proposal that is computed without writing anything:
removing comment bodies, merging literal ``\\input`` lines into the main source, and
inlining a generated ``.bbl`` for a ``\\bibliography`` line. Merging and inlining need
a complete dependency graph (``SourceGraph.is_complete``); comment removal needs every
source to be readable. A proposal with any blocking finding yields no contents at all.

Every proposal still requires the workflow's baseline/prepared PDF comparison.
Token expansion, arbitrary catcodes and semantic equivalence are not inferred. What
must stay untouched is defined by ``tex_protected_regions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from .comment_markers import MarkerCategory, marker_pattern
from .errors import SettingsError
from .findings import Finding, Status, has_blockers
from .limits import MIB
from .option_config import require_pairs, validate_scalars
from .path_safety import is_relative_posix_path
from .report import Change, ChangeKind
from .source_analysis import lint_sources
from .source_findings import listed_names
from .source_graph import ArgumentLocation, SourceGraph
from .tex_commands import ROOT_SUFFIXES
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    in_half_open_span,
    mask_literals,
    scan_source_commands,
    source_regions,
)
from .tex_protected_regions import protected_spans
from .text_edits import TextEdit, apply_text_edits, unified_diff_text

_PRIVATE = marker_pattern(MarkerCategory.EDIT_MARKER, MarkerCategory.PRIVACY_KEYWORD)
_COMMENT_RULE = "source.comment_removal"
_MERGE_RULE = "source.merge_inputs"
_INLINE_RULE = "source.inline_bibliography"
# Commands that read files or depend on where they run. Input merging moves text into
# another file, so a command in this set would mean something else after the merge.
# (``\end`` is judged separately: only ``\end{document}`` is a problem.)
_FORBIDDEN_IN_MERGED_INPUT = frozenset(
    {
        "include",
        "import",
        "subimport",
        "inputfrom",
        "subinputfrom",
        "subfile",
        "subfileinclude",
        "endinput",
        "jobname",
        "inputlineno",
    }
)
# Commands that make an inlined bibliography unusable: file reads and biblatex data.
_BIBLIOGRAPHY_BLOCKERS = frozenset(
    {"input", "include", "addbibresource", "entry", "refsection", "endinput"}
)

CommentPolicy = Literal["retain", "private", "all"]


def _require_relative_path(name: str, value: object) -> None:
    if not isinstance(value, str) or not is_relative_posix_path(
        value, normalized=False, characters="no_line_breaks"
    ):
        raise SettingsError(f"{name} must be a path relative to the project", (name,))


@dataclass(frozen=True)
class SourceTransformOptions:
    """Which source transformations to propose; all are off by default."""

    comment_policy: CommentPolicy = "retain"
    merge_inputs: bool = False
    inline_bibliography: str | None = None
    filename_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_scalars(self)
        if self.inline_bibliography is not None:
            _require_relative_path("inline_bibliography", self.inline_bibliography)
            if PurePosixPath(self.inline_bibliography).suffix != ".bbl":
                raise SettingsError(
                    "inline_bibliography must be a project-relative .bbl path",
                    ("inline_bibliography",),
                )
        require_pairs(
            "filename_overrides",
            self.filename_overrides,
            first=_require_relative_path,
            second=_require_relative_path,
        )


@dataclass
class SourceTransformPlan:
    """Proposed bytes, removals and diffs for the selected transformations.

    ``originals`` and ``removal_originals`` hold the bytes each target had when it was
    planned, so a later write can refuse a file that changed in between.
    """

    contents: dict[str, bytes] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    removals: set[str] = field(default_factory=set)
    removal_originals: dict[str, bytes] = field(default_factory=dict)

    def reject(self) -> None:
        """Drop every proposal, keeping the findings that explain why."""
        self.contents.clear()
        self.originals.clear()
        self.changes.clear()
        self.removals.clear()
        self.removal_originals.clear()


def _problem(
    plan: SourceTransformPlan,
    rule: str,
    message: str,
    path: str | None = None,
    *,
    status: Status = "inconclusive",
    suggestion: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    plan.findings.append(
        Finding(
            rule,
            message,
            severity="error",
            status=status,
            path=path,
            suggestion=suggestion,
            details=details or {},
        )
    )


def _require_complete_graph(
    graph: SourceGraph, plan: SourceTransformPlan, rule: str, action: str
) -> bool:
    """Whether ``action`` may proceed: the graph has no gap and no blocking finding.

    A graph with gaps cannot say which files a rewrite would touch. The problem is
    ``failed`` when the graph has an error (a missing file) and ``inconclusive`` when it
    only has constructs the scan could not follow.
    """
    if graph.is_complete:
        return True
    _problem(
        plan,
        rule,
        f"{action} requires a complete unambiguous literal dependency graph.",
        status="failed" if has_blockers(graph.findings) else "inconclusive",
    )
    return False


class _CommentRemoval(NamedTuple):
    text: str
    removed: int
    uncertain: bool


def _remove_comments(text: str, policy: str) -> _CommentRemoval:
    """``text`` without the bodies of the comments ``policy`` selects, outside protected spans."""
    scan = source_regions(text)
    protected = protected_spans(text, scan)
    edits: list[TextEdit] = []
    for start, end, kind in scan.regions:
        if kind != "comment" or in_half_open_span(protected.spans, start):
            continue
        if policy == "private" and not _PRIVATE.search(text[start:end]):
            continue
        # Retain '%' and its original line terminator: removing either may join
        # tokens, introduce a space, or turn a comment-only line into a paragraph.
        if end > start + 1:
            edits.append(TextEdit(start + 1, end, ""))
    return _CommentRemoval(apply_text_edits(text, edits), len(edits), protected.uncertain)


def _command_comments(text: str) -> str:
    comments = []
    for start, end, kind in source_regions(text).regions:
        if kind == "comment":
            newline = "\r\n" if text[end : end + 2] == "\r\n" else "\n"
            comments.append(text[start:end] + newline)
    return "".join(comments)


def _prevents_merging(command: Command, *, in_main: bool) -> bool:
    """Whether this command has file or execution semantics that input merging cannot keep.

    An ``\\end`` closes an environment and is fine, except ``\\end{document}``, which only
    the main file may contain.
    """
    if command.name == "end":
        return not command.arguments or (not in_main and command.ends("document", exact=True))
    return command.name in _FORBIDDEN_IN_MERGED_INPUT


class _OwnLine(NamedTuple):
    """The line holding a command, as offsets into the text."""

    start: int
    # Just after the command's argument and closing brace.
    command_end: int
    # Just after the line break.
    end: int


def _own_line(text: str, command: Command) -> _OwnLine | None:
    """The line of ``command`` when nothing but comments shares it, else ``None``.

    The command's first argument is its file; a bare ``\\input name`` has no closing brace.
    """
    argument = command.arguments[0]
    start = text.rfind("\n", 0, command.start) + 1
    line_break = text.find("\n", argument.end)
    end = len(text) if line_break < 0 else line_break + 1
    command_end = argument.end + (text[argument.end : argument.end + 1] == "}")
    if text[start : command.start].strip() or mask_literals(text[command_end:end]).strip():
        return None
    return _OwnLine(start, command_end, end)


class _Merge(NamedTuple):
    edit: TextEdit
    target: str


class _InputMerger:
    """Splices the files named by literal ``\\input`` lines into the main source, recursively.

    The proposal is built in ``texts``. Each file merged is remembered so the plan can
    remove it from the package. A problem is added to the plan as an error finding and
    stops further merging.
    """

    def __init__(
        self, graph: SourceGraph, texts: dict[str, str], plan: SourceTransformPlan
    ) -> None:
        self.graph = graph
        self.texts = texts
        self.plan = plan
        self.expanded_bytes = 0
        self.merged: set[str] = set()

    def run(self) -> None:
        main = self.graph.main
        if main is None:
            return
        self.texts[main] = self.expand(main, ())
        if any(
            reference.target in self.merged and reference.command != "input"
            for reference in self.graph.references.values()
        ):
            self._problem("A merged input is also used by a different file-reading command.")
        if not has_blockers(self.plan.findings):
            self.plan.removals.update(self.merged)
            self.plan.findings.append(
                Finding(
                    _MERGE_RULE,
                    f"Planned recursive merging of {len(self.merged)} literal input files.",
                    severity="info",
                    status="passed",
                    details={"files": sorted(self.merged)},
                )
            )

    def _problem(self, message: str, path: str | None = None) -> None:
        _problem(self.plan, _MERGE_RULE, message, path)

    def _blocked(self) -> bool:
        return has_blockers(self.plan.findings)

    def expand(self, name: str, stack: tuple[str, ...]) -> str:
        """The proposed text of ``name`` with its ``\\input`` lines replaced by their files."""
        source = self.graph.sources[name]
        text = self.texts[name]
        if self._blocked():
            return text
        # Comments may already have changed offsets, so parse this proposal. Comment
        # removal deletes only bodies the lexer masks anyway, so the proposal has the
        # inspected command sequence; pair each command with its inspected original.
        edits: list[TextEdit] = []
        for index, command in enumerate(scan_source_commands(text)):
            if self._blocked():
                break
            if _prevents_merging(command, in_main=name == self.graph.main):
                self._problem(
                    f"\\{command.name} has file or execution semantics "
                    "outside standalone input merging.",
                    name,
                )
                continue
            if command.name != "input":
                continue
            original = source.commands[index] if index < len(source.commands) else None
            merge = self._input_merge(name, text, command, original, stack)
            if merge is None:
                continue
            self.expanded_bytes += len(merge.edit.replacement.encode("utf-8"))
            if self.expanded_bytes > MAX_TOTAL_SOURCE_BYTES:
                self._problem(
                    "Merged sources exceed the "
                    f"{MAX_TOTAL_SOURCE_BYTES // MIB} MiB expansion limit.",
                    name,
                )
                return text
            edits.append(merge.edit)
            self.merged.add(merge.target)
        return apply_text_edits(text, edits)

    def _input_merge(
        self,
        name: str,
        text: str,
        command: Command,
        original: Command | None,
        stack: tuple[str, ...],
    ) -> _Merge | None:
        """The edit that replaces one ``\\input`` line by its file, or ``None`` after a problem."""
        if not command.arguments or command.depth:
            self._problem("Only direct literal input commands can be merged.", name)
            return None
        if (
            original is None
            or original.name != "input"
            or original.line != command.line
            or not original.arguments
        ):
            self._problem("Input position could not be reconciled after comment edits.", name)
            return None
        located = original.arguments[0].stripped()
        reference = self.graph.references.get(ArgumentLocation(name, located.start, located.end))
        if reference is None or reference.target in stack:
            self._problem("Input target or execution order is unresolved.", name)
            return None
        target = reference.target
        if target not in self.texts or not self.texts[target].endswith(("\n", "\r")):
            self._problem("Merged input files must be UTF-8 and newline-terminated.", target)
            return None
        line = _own_line(text, command)
        if line is None:
            self._problem(
                "Only input commands occupying their own source line can be merged.", name
            )
            return None
        leading = _command_comments(text[line.start : line.command_end])
        trailing = _command_comments(text[line.command_end : line.end])
        replacement = leading + self.expand(target, (*stack, name)) + trailing
        return _Merge(TextEdit(line.start, line.end, replacement), target)


def _merge_inputs(graph: SourceGraph, texts: dict[str, str], plan: SourceTransformPlan) -> None:
    if not _require_complete_graph(graph, plan, _MERGE_RULE, "Input merging"):
        return
    if graph.other_roots():
        _problem(plan, _MERGE_RULE, "Input merging supports one document root per package.")
        return
    _InputMerger(graph, texts, plan).run()


def _is_standalone_bibtex_environment(commands: list[Command]) -> bool:
    """Whether the commands hold one ``thebibliography`` environment and nothing that reads data."""
    starts = [command for command in commands if command.begins("thebibliography", exact=True)]
    ends = [command for command in commands if command.ends("thebibliography", exact=True)]
    return (
        len(starts) == 1
        and len(ends) == 1
        and starts[0].start < ends[0].start
        and not any(command.name in _BIBLIOGRAPHY_BLOCKERS for command in commands)
    )


def _bibliography_commands(
    graph: SourceGraph, texts: dict[str, str], plan: SourceTransformPlan
) -> list[tuple[str, Command]]:
    """Every ``\\bibliography`` command of the files that stay in the package.

    A biblatex command is reported as a problem on the way: inlining supports BibTeX only.
    """
    found = []
    for name in graph.visited - plan.removals:
        for command in scan_source_commands(texts[name]):
            if command.name in {"addbibresource", "printbibliography"}:
                _problem(
                    plan, _INLINE_RULE, "Biblatex bibliography inlining is not supported.", name
                )
            if command.name == "bibliography":
                found.append((name, command))
    return found


def _inline_bibliography(
    graph: SourceGraph, texts: dict[str, str], path: str, plan: SourceTransformPlan
) -> None:
    if not _require_complete_graph(graph, plan, _INLINE_RULE, "Bibliography inlining"):
        return
    if path not in graph.sources or path not in texts:
        _problem(
            plan,
            _INLINE_RULE,
            "The selected generated bibliography is missing or unreadable.",
            path,
            status="failed",
        )
        return
    text = texts[path]
    if not _is_standalone_bibtex_environment(scan_source_commands(text)):
        _problem(
            plan,
            _INLINE_RULE,
            "Inlining requires a standalone BibTeX thebibliography environment; "
            "biblatex data and nested file inputs are unsupported.",
            path,
        )
        return
    locations = _bibliography_commands(graph, texts, plan)
    if len(locations) != 1 or locations[0][1].depth:
        _problem(
            plan,
            _INLINE_RULE,
            "Inlining requires exactly one direct literal bibliography command.",
        )
        return
    name, command = locations[0]
    if not command.arguments:
        _problem(
            plan, _INLINE_RULE, "The bibliography command has no literal database argument.", name
        )
        return
    target = texts[name]
    line = _own_line(target, command)
    if line is None:
        _problem(
            plan, _INLINE_RULE, "The bibliography command must occupy its own source line.", name
        )
        return
    leading = _command_comments(target[line.start : line.command_end])
    trailing = _command_comments(target[line.command_end : line.end])
    texts[name] = (
        target[: line.start] + leading + text.rstrip("\r\n") + "\n" + trailing + target[line.end :]
    )
    plan.findings.append(
        Finding(
            _INLINE_RULE,
            "Planned literal generated-bibliography inlining.",
            severity="info",
            status="passed",
            path=name,
            details={"bibliography": path},
        )
    )


def _remove_file_comments(
    graph: SourceGraph,
    texts: dict[str, str],
    name: str,
    policy: CommentPolicy,
    plan: SourceTransformPlan,
) -> None:
    """Plan the comment removal of one document source, or record why it cannot be done."""
    commands = graph.sources[name].commands
    if any(command.name in {"catcode", "scantokens", "csname"} for command in commands):
        _problem(plan, _COMMENT_RULE, "Dynamic lexical behavior prevents comment removal.", name)
        return
    removal = _remove_comments(texts[name], policy)
    if removal.uncertain:
        _problem(
            plan,
            _COMMENT_RULE,
            "An unclosed literal or protected region prevents comment removal.",
            name,
        )
        return
    texts[name] = removal.text
    plan.findings.append(
        Finding(
            _COMMENT_RULE,
            f"Planned removal of {removal.removed} comment bodies; percent signs, "
            "line terminators, directives and license blocks are retained.",
            severity="info",
            status="passed",
            path=name,
            details={"removed_comments": removal.removed},
        )
    )


def _plan_comment_removal(
    graph: SourceGraph,
    texts: dict[str, str],
    policy: CommentPolicy,
    plan: SourceTransformPlan,
) -> None:
    """Plan comment removal in every document source; the others are copied verbatim."""
    if graph.unparsed:
        unparsed = sorted(graph.unparsed)
        _problem(
            plan,
            _COMMENT_RULE,
            "Some source files could not be parsed safely: "
            f"{listed_names(unparsed)}. Comment removal needs to read every source file.",
            suggestion="Make these files readable UTF-8 text without symbolic links, or leave "
            "them out of the package, then prepare again.",
            details={"files": unparsed},
        )
    for name in list(texts):
        # Comment editing stays inside authored document sources; class, style and
        # generated template internals are copied verbatim.
        if PurePosixPath(name).suffix.lower() in ROOT_SUFFIXES:
            _remove_file_comments(graph, texts, name, policy, plan)


def _materialize(graph: SourceGraph, texts: dict[str, str], plan: SourceTransformPlan) -> None:
    """Turn the proposed texts into bytes, originals, removals and reviewable changes."""
    for name in sorted(plan.removals):
        plan.removal_originals[name] = graph.sources[name].encode()
        plan.changes.append(
            Change(
                name,
                kind=ChangeKind.REMOVE,
                reason="Input contents were merged into the main source copy.",
            )
        )
    for name, text in texts.items():
        source = graph.sources[name]
        if name in plan.removals or text == source.text:
            continue
        encoded = source.encode(text)
        if len(encoded) > MAX_SOURCE_BYTES:
            _problem(
                plan,
                _MERGE_RULE,
                f"Transformed source exceeds the {MAX_SOURCE_BYTES // MIB} MiB per-file limit.",
                name,
            )
            break
        plan.contents[name] = encoded
        plan.originals[name] = source.encode()
        plan.changes.append(
            Change(
                name,
                kind=ChangeKind.REWRITE,
                reason="Apply explicitly selected source transformations.",
                diff=unified_diff_text(name, source.text, text),
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
    graph = SourceGraph.build(root, main)
    if graph.main is None:
        # The same source findings that source analysis reports for an unusable main.
        plan.findings.extend([*graph.findings, *lint_sources(graph)])
        return plan
    texts = {name: source.text for name, source in graph.sources.items()}
    if options.comment_policy != "retain":
        _plan_comment_removal(graph, texts, options.comment_policy, plan)
    try:
        if options.merge_inputs:
            _merge_inputs(graph, texts, plan)
        if options.inline_bibliography:
            _inline_bibliography(graph, texts, options.inline_bibliography, plan)
    except (ParseLimitExceeded, RecursionError):
        _problem(
            plan,
            _MERGE_RULE if options.merge_inputs else _INLINE_RULE,
            "Transformation exceeded the bounded literal parsing limits.",
        )
    if not has_blockers(plan.findings):
        _materialize(graph, texts, plan)
    if has_blockers(plan.findings):
        plan.reject()
    return plan
