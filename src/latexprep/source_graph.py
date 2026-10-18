"""Literal dependency graph of a LaTeX project, built on the bounded lexical subset.

``SourceGraph.build`` selects the main file, then walks it in source order, following
each literal file reference into the file it names. Resolution uses the selected main
file's compilation directory and supported literal import/subfile contexts. Dynamic
paths, unknown file readers, scoped paths and ambiguous lookups are recorded as
findings instead of being guessed.

The graph owns the walk and its results: sources, references, dependencies and
findings. Reading and lexing files is ``source_loading``; judging commands is
``source_command_checks``; turning a name into a file is ``source_resolution``; the
command tables are ``source_file_commands``.

The walk. ``_visit`` reads a file and goes through its commands in order. A file
command is resolved in the current lookup scope, its reference is recorded, and the
file it names is visited in turn with the scope that command establishes (an
``\\import`` or ``\\subfile`` adds a directory). A ``\\graphicspath`` changes the
graphics search path for the rest of the file and for what the file's inputs leave
behind, which is why a visit returns the search path in force at its end. A visit is
skipped when the same file was already walked with the same search path, directory
context and subfiles state.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple, Self

from .cancellation import cancellation_point
from .errors import PreparationError
from .findings import Finding, has_blockers
from .path_safety import has_drive_prefix, lexically_escapes
from .project_files import is_parsed_source, known_path_index, project_files
from .source_command_checks import SourceChecks, check_source
from .source_file_commands import (
    COMMA_LIST_FILE_COMMANDS,
    FILE_COMMANDS,
    IMPORT_COMMANDS,
    SUPPORTED_FONT_COMMANDS,
    TRAVERSED_COMMANDS,
    file_argument_index,
    is_literal_text,
)
from .source_findings import GraphFindings, listed_names
from .source_loading import LoadBudget, ParsedSource, is_document_root, load_source
from .source_resolution import (
    LookupScope,
    ProjectTree,
    Reference,
    SystemReference,
    project_path,
    resolve_reference,
)
from .tex_commands import ROOT_SUFFIXES
from .tex_lexer import (
    MAX_COMMANDS,
    Argument,
    Command,
    read_group,
    skip_space,
    split_argument_items,
)

MAX_INPUT_DEPTH = 128
_FONT_EXTENSIONS = frozenset({".otf", ".ttf", ".ttc"})
_FONT_OPTION_KEYS = frozenset({"Path", "Ligatures", "Scale", "Renderer", "Numbers"})
_FONT_PATH_OPTION = re.compile(r"(?:^|,)\s*Path\s*=\s*(\{[^{}]*\}|[^,]*)")

# Findings about a source file the graph tried to read but could not parse. Every
# command and reference in such a file is unknown, so a reader that needs the file's
# content (a document root, a citation) cannot continue past one.
UNPARSED_SOURCE_RULES = frozenset(
    {
        "source.encoding",
        "source.size_limit",
        "source.argument_limit",
        "source.command_limit",
        "source.link",
    }
)
# Findings that leave the literal graph incomplete: an unparsed file, or a reference
# whose file or path context the scan cannot establish. A finding of one of these rules
# only matters while it is unresolved; a resolved lookup-order or conditional dependency
# is evidence, not a gap. Findings of other rules (for example ``source.path_option``) are
# advisories about how a reference was written and do not make the graph incomplete.
INCOMPLETE_GRAPH_RULES = UNPARSED_SOURCE_RULES | {
    # The traversal stopped or skipped text.
    "source.literal_unclosed",
    "source.input_cycle",
    "source.input_depth",
    "source.expansion_limit",
    # A reference is not a literal path in a known context.
    "source.dynamic_path",
    "source.nested_file_command",
    "source.conditional_dependency",
    "source.path_literal_context",
    "source.custom_path",
    "source.unsupported_command",
    "source.subfile_structure",
    "source.font_options",
    # A literal path does not select exactly one file.
    "source.external_path",
    "source.ambiguous_dependency",
    "source.case_mismatch",
    "source.missing_dependency",
    "source.context_ambiguity",
}


class ArgumentLocation(NamedTuple):
    """Where an argument's text sits in one project file."""

    path: str
    start: int
    end: int


class FileReference(NamedTuple):
    """The project file a literal argument resolved to, and the command that read it."""

    target: str
    command: str


class _Frame(NamedTuple):
    """One visit of a file: the lookup state it starts with.

    ``search`` is the graphics search path in force, ``context`` the directories of the
    files entered so far (innermost first), and ``subfile`` whether the file is read as a
    ``\\subfile`` body, of which only the part between ``\\begin{document}`` and
    ``\\end{document}`` counts.
    """

    path: str
    search: tuple[str, ...]
    context: tuple[str, ...] = ()
    subfile: bool = False


@dataclass(frozen=True, slots=True)
class _Visit:
    """The file being walked: its parsed source, the files above it, and its lookup context."""

    path: str
    source: ParsedSource
    stack: tuple[str, ...]
    context: tuple[str, ...]

    @property
    def inner_stack(self) -> tuple[str, ...]:
        """The files above any file this one reads: those above it, and itself."""
        return (*self.stack, self.path)


# "project" reads every source file (to discover document roots); "selected" reads
# only the sources reached from an explicit main file.
GraphScope = Literal["project", "selected"]


class SourceGraph:
    """Literal source dependencies of one selected main file.

    Construct with :meth:`build`. ``findings`` holds every finding in order;
    ``uncertainties`` is the inconclusive subset outside the supported lexical subset.
    ``references`` maps each literal file argument to the file it names, and
    ``search_arguments`` the arguments a flat layout must rewrite for directory changes.
    """

    def __init__(self, root: Path) -> None:
        self.files = project_files(root)
        self.known_paths = known_path_index(self.files)
        self.sources: dict[str, ParsedSource] = {}
        self.findings: list[Finding] = []
        self.uncertainties: list[Finding] = []
        self.dependencies: set[str] = set()
        self.references: dict[ArgumentLocation, FileReference] = {}
        # Search-path arguments and their flat-layout replacements.
        self.search_arguments: dict[ArgumentLocation, str] = {}
        self.system_references: list[SystemReference] = []
        self.visited: set[str] = set()
        self.subfile_targets: set[str] = set()
        self.subfiles_package_loaded = False
        self.roots: list[str] = []
        self.main: str | None = None
        # Compilation directory of the main file; paths resolve relative to it.
        self.cwd = ""
        self._reporter = GraphFindings(self.findings, self.uncertainties)
        self._cache: dict[tuple[_Frame, bool], tuple[str, ...]] = {}
        self._budget = LoadBudget()
        self._attempted: set[str] = set()
        self._events = 0

    @classmethod
    def build(cls, root: Path, main: str | None, *, scope: GraphScope = "project") -> Self:
        """Read the project's sources and follow literal references from the main file.

        Without an explicit ``main``, the project's single document root is used.
        """
        if scope == "selected" and main is None:
            raise PreparationError("Selected source analysis requires an explicit main file.")
        graph = cls(root)
        if scope == "project":
            graph._load_project_sources()
            # An unspecified main file is chosen among all project roots.
            graph.roots = graph._document_roots()
        graph.main = graph._select_main(main)
        if graph.main:
            graph.cwd = posixpath.dirname(graph.main)
            graph.dependencies.add(graph.main)
            graph._visit(_Frame(graph.main, ()), ())
        if scope == "selected":
            graph.roots = graph._document_roots()
        return graph

    @property
    def unparsed(self) -> set[str]:
        """Files the graph tried to read but could not parse; their references are unknown."""
        return self._attempted.difference(self.sources)

    @property
    def is_complete(self) -> bool:
        """Whether the walk followed everything: no uncertainty and no blocking finding.

        Rewriting sources (merging inputs, inlining a bibliography) needs this; a graph
        with a gap cannot say which files a rewrite would touch.
        """
        return not self.uncertainties and not has_blockers(self.findings)

    def other_roots(self, *, include_subfiles: bool = True) -> list[str]:
        """The document roots other than the main file.

        A subfile body declares its own root; ``include_subfiles=False`` leaves those
        out, since a flat package keeps them under the main file.
        """
        return [
            name
            for name in self.roots
            if name != self.main and (include_subfiles or name not in self.subfile_targets)
        ]

    def _load_project_sources(self) -> None:
        for name, path in self.files.items():
            cancellation_point()
            if is_parsed_source(name, path):
                self._load(name)

    def _document_roots(self) -> list[str]:
        return [
            name
            for name, source in self.sources.items()
            if Path(name).suffix.lower() in ROOT_SUFFIXES and is_document_root(source.commands)
        ]

    def _load(self, name: str) -> ParsedSource | None:
        """The parsed source of ``name``, loading and checking it on first use."""
        if name in self.sources:
            return self.sources[name]
        if name in self._attempted:
            return None
        self._attempted.add(name)
        source = load_source(name, self.files[name], self._budget, self._reporter)
        if source is None:
            return None
        self.sources[name] = source
        check_source(SourceChecks(name, source, self.known_paths, self._reporter))
        return source

    def _select_main(self, main: str | None) -> str | None:
        if main is not None:
            candidate = posixpath.normpath(main)
            escapes = (
                main.startswith("/")
                or "\\" in main
                or has_drive_prefix(main)
                or lexically_escapes(candidate)
            )
            if escapes or candidate not in self.files:
                roots = f" Document roots found: {listed_names(self.roots)}." if self.roots else ""
                self.findings.append(
                    Finding(
                        "source.main_invalid",
                        f"Main file {main!r} must be a path relative to the project folder, "
                        "not absolute or outside it."
                        if escapes
                        else f"Main file {main!r} was not found in the project.{roots}",
                        severity="error",
                        path=main,
                        suggestion="Give the main file relative to the project folder, "
                        "for example --main paper/main.tex.",
                        details={"candidates": list(self.roots)},
                    )
                )
                return None
            if candidate not in self.sources:
                self._load(candidate)
            return candidate
        if len(self.roots) == 1:
            return self.roots[0]
        self.findings.append(
            Finding(
                "source.root_ambiguous" if self.roots else "source.root_missing",
                f"Multiple document roots were found: {listed_names(self.roots)}."
                if self.roots
                else "No literal document root could be selected.",
                severity="error",
                status="inconclusive",
                suggestion="Select the main file explicitly with --main (or the main setting).",
                details={"candidates": self.roots},
            )
        )
        return None

    def _tree(self) -> ProjectTree:
        return ProjectTree(self.files, self.cwd, self._load)

    def _project_path(self, visit: _Visit, command: Command, value: str, base: str) -> str | None:
        reference = Reference(value, visit.path, command)
        return project_path(reference, self._tree(), self._reporter, base=base)

    def _search_paths(
        self, visit: _Visit, command: Command, context: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """The directories a ``\\graphicspath`` declares, or ``None`` when it is not literal.

        The argument is recorded in ``search_arguments`` so a flat layout rewrites it to
        the current directory.
        """
        name = visit.path
        if not command.arguments:
            return None
        argument = command.arguments[0]
        if command.depth:
            self._reporter.uncertain(
                "source.scoped_search_path",
                "A scoped or macro-defined \\graphicspath is outside the supported subset.",
                name,
                command.line,
            )
            return None
        paths: list[str] = []
        pos = 0
        while skip_space(argument.value, pos) < len(argument.value):
            pos = skip_space(argument.value, pos)
            group = read_group(argument.value, pos)
            if group is None or not is_literal_text(group.value.strip()):
                self._reporter.uncertain(
                    "source.dynamic_search_path",
                    "\\graphicspath must contain a literal list such as {{figures/}{plots/}}.",
                    name,
                    command.line,
                )
                return None
            value = group.value.strip()
            if not value.endswith("/"):
                self._reporter.uncertain(
                    "source.search_path_syntax",
                    "A supported graphicspath directory must end with a slash.",
                    name,
                    command.line,
                )
                return None
            if context and self.subfiles_package_loaded:
                value = posixpath.relpath(posixpath.join(context[0], value), self.cwd or ".") + "/"
            if self._project_path(visit, command, value, self.cwd) is None:
                return None
            paths.append(value)
            pos = group.end + 1
        self.search_arguments[ArgumentLocation(name, argument.start, argument.end)] = "{./}"
        return tuple(paths)

    def _record_reference(
        self, name: str, argument: Argument, target: str, command: Command
    ) -> None:
        location = ArgumentLocation(name, argument.start, argument.end)
        previous = self.references.get(location)
        if previous and previous.target != target:
            self._reporter.error(
                "source.context_ambiguity",
                "The same reference selects different files under different path contexts: "
                f"{previous.target} and {target}.",
                name,
                command.line,
                {"candidates": [previous.target, target]},
                suggestion="Give the file a name that resolves to the same file from every "
                "directory that reads it.",
            )
        else:
            self.references[location] = FileReference(target, command.name)

    def _font_directory(self, visit: _Visit, command: Command) -> str | None:
        """The directory a font command's ``Path`` option names, or ``None`` when unsupported.

        Only an explicit local font file and the options in ``_FONT_OPTION_KEYS`` are
        followed. The ``Path`` argument is recorded in ``search_arguments`` so a flat
        layout rewrites it.
        """
        name = visit.path
        source = visit.source
        if not command.arguments or (
            PurePosixPath(command.arguments[0].value.strip()).suffix.lower() not in _FONT_EXTENSIONS
        ):
            self._reporter.uncertain(
                "source.unsupported_command",
                "Only explicit local .otf/.ttf/.ttc font filenames are supported; "
                "font-family lookup is not inferred.",
                name,
                command.line,
            )
            return None
        directory = "./"
        seen_path = False
        for option in command.options:
            if source.text[option.start : option.end] != option.value:
                self._reporter.uncertain(
                    "source.font_options", "Font options contain masked text.", name
                )
                return None
            for part in option.value.split(","):
                key, separator, value = part.partition("=")
                key, value = key.strip(), value.strip()
                if value.startswith("{") and value.endswith("}"):
                    value = value[1:-1]
                if not separator or key not in _FONT_OPTION_KEYS or not is_literal_text(value):
                    self._reporter.uncertain(
                        "source.font_options",
                        "Font options may select additional faces or dynamic filenames; "
                        "only Path, Ligatures, Scale, Renderer and Numbers are supported.",
                        name,
                        command.line,
                    )
                    return None
                if key != "Path":
                    continue
                if seen_path or not value.endswith("/"):
                    self._reporter.uncertain(
                        "source.font_options",
                        "A font Path must be a single literal directory ending in '/'.",
                        name,
                        command.line,
                    )
                    return None
                seen_path = True
                directory = value
                match = _FONT_PATH_OPTION.search(option.value)
                if match:
                    start, end = match.span(1)
                    location = ArgumentLocation(name, option.start + start, option.start + end)
                    self.search_arguments[location] = (
                        "{./}" if option.value[start:end].startswith("{") else "./"
                    )
        return directory

    def _subfile_commands(self, name: str, source: ParsedSource) -> list[Command]:
        """The commands of a subfile body, or none when the file is not a supported subfile.

        A subfile has one ``subfiles`` document class naming the main file as its parent
        and one document environment; only the commands inside that environment are read.
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
            self._reporter.uncertain(
                "source.subfile_structure",
                "A subfile requires one literal subfiles class and document environment.",
                name,
            )
            return []
        declaration = declarations[0]
        if len(declaration.options) != 1 or not is_literal_text(declaration.options[0].value):
            self._reporter.uncertain(
                "source.subfile_structure", "A subfile requires an explicit parent path.", name
            )
            return []
        parent_argument = declaration.options[0].stripped()
        parent = project_path(
            Reference(parent_argument.value, name, declaration),
            self._tree(),
            self._reporter,
            base=posixpath.dirname(name),
        )
        if parent is None or parent != self.main:
            self._reporter.uncertain(
                "source.subfile_structure",
                "The subfile parent must name the selected main file exactly.",
                name,
            )
            return []
        location = ArgumentLocation(name, parent_argument.start, parent_argument.end)
        self.references[location] = FileReference(parent, "input")
        self.subfile_targets.add(name)
        return [
            command
            for command in source.commands
            if starts[0].start < command.start < ends[0].start
        ]

    def _visit(self, frame: _Frame, stack: tuple[str, ...]) -> tuple[str, ...]:
        """Walk the file of ``frame`` and return the graphics search path in force at its end.

        ``stack`` holds the files being read above this one. A file already on it is a
        cycle, and a stack at ``MAX_INPUT_DEPTH`` is too deep; both are reported and the
        visit returns the search path it was given. A file that cannot be loaded is not
        visited at all. The walk stops, with a finding, once the whole graph has
        processed ``MAX_COMMANDS`` commands.
        """
        name = frame.path
        if name in stack:
            self._reporter.uncertain(
                "source.input_cycle",
                "Source input contains a cycle; execution order is not established.",
                name,
                details={"cycle": [*stack, name]},
            )
            return frame.search
        if len(stack) >= MAX_INPUT_DEPTH:
            self._reporter.uncertain(
                "source.input_depth",
                "Source input exceeds the bounded dependency traversal depth.",
                name,
            )
            return frame.search
        key = (frame, self.subfiles_package_loaded)
        if key in self._cache:
            return self._cache[key]
        source = self._load(name)
        if source is None:
            return frame.search
        self.visited.add(name)
        visit = _Visit(name, source, stack, frame.context)
        commands = self._subfile_commands(name, source) if frame.subfile else source.commands
        search = frame.search
        for command in commands:
            self._events += 1
            if self._events > MAX_COMMANDS:
                self._reporter.uncertain(
                    "source.expansion_limit",
                    "Dependency traversal exceeded its bounded command budget.",
                    name,
                    command.line,
                )
                return search
            search = self._visit_command(visit, command, search)
        self._cache[key] = search
        return search

    def _visit_command(
        self, visit: _Visit, command: Command, search: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Follow one command of the visited file; return the graphics search path afterwards."""
        if command.name == "graphicspath":
            paths = self._search_paths(visit, command, visit.context)
            return search if paths is None else paths
        if command.name not in FILE_COMMANDS or not command.arguments:
            return search
        if command.name in {"usepackage", "RequirePackage"} and any(
            item.value == "subfiles" for item in split_argument_items(command.arguments[0])
        ):
            self.subfiles_package_loaded = True
        if command.name == "documentclass" and command.arguments[0].value == "subfiles":
            self._reporter.uncertain(
                "source.path_option",
                "Compiling a subfiles class as the main root requires inherited preamble "
                "semantics outside this subset; select the parent document.",
                visit.path,
                command.line,
            )
        elif command.name in IMPORT_COMMANDS:
            self._visit_import(visit, command, search)
        else:
            search = self._visit_file_command(visit, command, search)
        return search

    def _visit_import(self, visit: _Visit, command: Command, search: tuple[str, ...]) -> None:
        """Follow ``\\import`` and its relatives: a directory argument, then a file in it."""
        if len(command.arguments) < 2:
            return
        name, source = visit.path, visit.source
        directory_argument, argument = (item.stripped() for item in command.arguments[:2])
        if not all(
            is_literal_text(item.value) and source.text[item.start : item.end] == item.value
            for item in (directory_argument, argument)
        ):
            self._reporter.uncertain(
                "source.dynamic_path",
                "Import paths must be plain literal arguments.",
                name,
                command.line,
            )
            return
        parent = visit.context[0] if visit.context and command.name.startswith("sub") else self.cwd
        directory = self._project_path(visit, command, directory_argument.value, parent)
        if directory is None:
            return
        scope = LookupScope(base=directory, subfiles_loaded=self.subfiles_package_loaded)
        target = self._resolve(visit, command, argument.value, scope)
        if target is None:
            return
        self._record_reference(name, argument, target, command)
        location = ArgumentLocation(name, directory_argument.start, directory_argument.end)
        self.search_arguments[location] = "./"
        self._visit(
            _Frame(target, search, (directory, *visit.context)),
            visit.inner_stack,
        )

    def _visit_file_command(
        self, visit: _Visit, command: Command, search: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Follow a command whose file argument is a name or a list of names.

        Each literal name is resolved and recorded. A ``\\subfile`` is read with its own
        directory as context; the commands in ``TRAVERSED_COMMANDS`` are read in the
        current context, and what they leave of the search path is returned.
        """
        index = file_argument_index(command.name)
        if len(command.arguments) <= index:
            return search
        font_directory = None
        if command.name in SUPPORTED_FONT_COMMANDS:
            directory = self._font_directory(visit, command)
            if directory is None:
                return search
            font_directory = self._project_path(visit, command, directory, self.cwd)
            if font_directory is None:
                return search
        source = visit.source
        for argument in split_argument_items(
            command.arguments[index], comma_separated=command.name in COMMA_LIST_FILE_COMMANDS
        ):
            if (
                source.text[argument.start : argument.end]
                != source.masked[argument.start : argument.end]
            ):
                continue
            scope = LookupScope(search, visit.context, font_directory, self.subfiles_package_loaded)
            target = self._resolve(visit, command, argument.value, scope)
            if target is None:
                continue
            self._record_reference(visit.path, argument, target, command)
            if command.name == "subfile":
                subfile_context = (posixpath.dirname(target), *visit.context)
                self._visit(_Frame(target, search, subfile_context, True), visit.inner_stack)
            if command.name in TRAVERSED_COMMANDS:
                search = self._visit(_Frame(target, search, visit.context), visit.inner_stack)
        return search

    def _resolve(
        self, visit: _Visit, command: Command, value: str, scope: LookupScope
    ) -> str | None:
        """Resolve one file name; a found file joins the dependencies, a toolchain one is kept."""
        resolution = resolve_reference(
            Reference(value, visit.path, command), scope, self._tree(), self._reporter
        )
        if resolution.target is not None:
            self.dependencies.add(resolution.target)
        if resolution.system is not None:
            self.system_references.append(resolution.system)
        return resolution.target
