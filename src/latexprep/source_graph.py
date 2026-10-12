"""Literal dependency graph of a LaTeX project, built on the bounded lexical subset.

Resolution uses the selected main file's compilation directory and supported
literal import/subfile contexts. Dynamic paths, unknown file readers, scoped
paths and ambiguous lookups are recorded as findings instead of being guessed.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple, Self

from .models import EvidenceKind, Finding, PreparationError
from .scheduler import cancellation_point
from .tex_lexer import (
    MAX_COMMANDS,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Argument,
    Command,
    ParseLimitExceeded,
    blank_regions,
    conditional_spans,
    in_conditional,
    read_group,
    scan_commands,
    skip_space,
    source_regions,
    split_argument_items,
)

MAX_INPUT_DEPTH = 128

_SOURCE_SUFFIXES = {
    ".tex",
    ".ltx",
    ".latex",
    ".sty",
    ".cls",
    ".def",
    ".cfg",
    ".tikz",
    ".pgf",
    ".bbl",
}
ROOT_SUFFIXES = {".tex", ".ltx", ".latex"}
# Graphics lookup order of the default pdfTeX graphics rules.
GRAPHICS_SUFFIXES = (".pdf", ".png", ".jpg", ".mps", ".jpeg", ".jbig2", ".jb2", ".eps", ".ps")
# Suffixes TeX tries, in order, before the bare name of a suffixless reference.
DEFAULT_SUFFIXES_BY_FILE_COMMAND: dict[str, tuple[str, ...]] = {
    "input": (".tex",),
    "include": (".tex",),
    "includegraphics": GRAPHICS_SUFFIXES,
    "bibliography": (".bib",),
    "addbibresource": (".bib",),
    "bibliographystyle": (".bst",),
    "documentclass": (".cls",),
    "usepackage": (".sty",),
    "RequirePackage": (".sty",),
    "RequirePackageWithOptions": (".sty",),
    "LoadClass": (".cls",),
    "LoadClassWithOptions": (".cls",),
    "import": (".tex",),
    "subimport": (".tex",),
    "inputfrom": (".tex",),
    "subinputfrom": (".tex",),
    "subfile": (".tex",),
    "pgfplotstableread": (),
    "pgfplotstabletypeset": (),
    "csvreader": (),
    "csvautotabular": (),
    "DTLloaddb": (),
    "lstinputlisting": (),
    "inputminted": (),
    "verbatiminput": (),
    "VerbatimInput": (),
    "includepdf": (".pdf",),
    "setmainfont": (),
    "setsansfont": (),
    "setmonofont": (),
    "fontspec": (),
}
FILE_COMMANDS = frozenset(DEFAULT_SUFFIXES_BY_FILE_COMMAND)
# Commands taking a directory and then a file name.
IMPORT_COMMANDS = frozenset({"import", "subimport", "inputfrom", "subinputfrom"})
SECOND_ARGUMENT_FILE_COMMANDS = frozenset({*IMPORT_COMMANDS, "DTLloaddb", "inputminted"})
# File commands whose argument is a comma-separated list of names.
COMMA_LIST_FILE_COMMANDS = frozenset(
    {"bibliography", "usepackage", "RequirePackage", "RequirePackageWithOptions"}
)
# Class, package and style commands that TeX may also satisfy from the toolchain.
TOOLCHAIN_FILE_COMMANDS = frozenset(
    {
        "bibliographystyle",
        "documentclass",
        "usepackage",
        "RequirePackage",
        "RequirePackageWithOptions",
        "LoadClass",
        "LoadClassWithOptions",
    }
)
_SUPPORTED_FONT_COMMANDS = {"setmainfont", "setsansfont", "setmonofont", "fontspec"}
_UNSUPPORTED_COMMANDS = {
    "includefrom",
    "subincludefrom",
    "subfileinclude",
    "externaldocument",
    "includepdfmerge",
    "addplot",
    "addplot3",
    "csvloop",
    "includesvg",
    "includeinkscape",
    "includestandalone",
    "includeonly",
    "openin",
    "openout",
    "read",
    "IfFileExists",
    "InputIfFileExists",
    "DeclareGraphicsExtensions",
    "DeclareGraphicsRule",
    "newread",
    "newwrite",
    "catcode",
    "scantokens",
    "csname",
    "directlua",
    "luadirect",
    "luaexec",
}
_FONT_COMMANDS = {
    "setmainfont",
    "setsansfont",
    "setmonofont",
    "fontspec",
    "newfontfamily",
    "newfontface",
}
_TEXT_COMMANDS = {
    "url",
    "nolinkurl",
    "path",
    "href",
    "hyperref",
    "label",
    "ref",
    "pageref",
    "eqref",
    "autoref",
    "cref",
    "Cref",
    "nameref",
    "vref",
    "cite",
    "citep",
    "citet",
    "parencite",
    "textcite",
    "nocite",
    "citeauthor",
    "citeyear",
    "texttt",
    "textbf",
    "textit",
    "textrm",
    "textsf",
    "text",
    "emph",
    "mbox",
    "fbox",
    "caption",
    "captionof",
    "title",
    "author",
    "thanks",
    "footnote",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "chapter",
    "part",
    "hypersetup",
    "ProvidesPackage",
    "ProvidesClass",
    "ProvidesFile",
    "typeout",
    "PackageWarning",
    "PackageInfo",
    "ClassWarning",
    "ClassInfo",
    "bibfield",
    "field",
}
# Arguments are control-sequence names, never files: biblatex's generated .bbl
# guards itself with \@ifundefined{ver@biblatex.sty}.
_MACRO_NAME_COMMANDS = {"@ifundefined", "@namedef", "@nameuse"}
_PATH_LIKE = re.compile(
    r"""
    (?: ^ | [\s,=] )      # the value's start or a list/key separator
    [^\s{}]+              # a name without spaces or groups
    \. (?: tex | ltx | bib | bst | sty | cls | pdf | png | jpe?g | eps | svg
         | csv | tsv | dat | txt | json | xml | ttf | otf )
    (?: $ | [\s,] )       # the value's end or a list separator
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Commands whose resolved target is read as TeX source in the current path context;
# a subfile is traversed with its own directory as context instead.
_TRAVERSED_COMMANDS = frozenset(
    {
        "input",
        "include",
        "documentclass",
        "usepackage",
        "RequirePackage",
        "RequirePackageWithOptions",
        "LoadClass",
        "LoadClassWithOptions",
    }
)


def path_comparison_key(value: str) -> str:
    """Key under which paths collide on case-insensitive or normalizing file systems."""
    return unicodedata.normalize("NFC", value).casefold()


def project_files(root: Path) -> dict[str, Path]:
    """Map every project-relative POSIX path (files and links) to its path, in stable order."""
    if not root.is_dir():
        raise PreparationError(f"Source project is not a directory: {root}")
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(
            root.rglob("*"),
            key=lambda path: (path_comparison_key(path.as_posix()), path.as_posix()),
        )
        if path.is_file() or path.is_symlink()
    }


def is_document_root(commands: list[Command]) -> bool:
    """Whether a source declares a document class or opens the document environment."""
    return any(
        command.name == "documentclass" or command.begins("document") for command in commands
    )


def is_literal_text(value: str) -> bool:
    """Whether ``value`` is nonempty and free of TeX syntax that could change its meaning."""
    return bool(value) and not any(char in value for char in "\\{}#$^~&%\n\r\x00")


@dataclass(frozen=True, slots=True)
class ParsedSource:
    """One readable project source and its lexical scan."""

    text: str
    # ``text`` with comments and literal regions blanked; offsets are unchanged.
    masked: str
    commands: list[Command]
    bom: bool = False


class ArgumentLocation(NamedTuple):
    """Where an argument's text sits in one project file."""

    path: str
    start: int
    end: int


class FileReference(NamedTuple):
    """The project file a literal argument resolved to, and the command that read it."""

    target: str
    command: str


class SystemReference(NamedTuple):
    """A class, package or style reference left for the TeX toolchain to resolve."""

    path: str
    command: Command
    reference: str
    basename: str


class _TraversalState(NamedTuple):
    path: str
    search: tuple[str, ...]
    context: tuple[str, ...]
    subfile: bool
    subfiles_package_loaded: bool


# "project" reads every source file (to discover document roots); "selected" reads
# only the sources reached from an explicit main file.
GraphScope = Literal["project", "selected"]


class SourceGraph:
    """Literal source dependencies of one selected main file.

    Construct with :meth:`build`. ``findings`` holds every finding in order;
    ``uncertainties`` is the inconclusive subset outside the supported lexical subset.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.files = project_files(root)
        # Every path suffix and directory prefix, for cheap "could this name a
        # project file anywhere" tests before a lookup context is established.
        self.known_paths: set[str] = set()
        for filename in self.files:
            parts = filename.split("/")
            self.known_paths.update("/".join(parts[start:]) for start in range(len(parts)))
            self.known_paths.update("/".join(parts[:end]) + "/" for end in range(1, len(parts)))
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
        self._cache: dict[_TraversalState, tuple[str, ...]] = {}
        self._source_bytes = 0
        self._parsed_commands = 0
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
            graph._visit(graph.main, (), ())
        if scope == "selected":
            graph.roots = graph._document_roots()
        return graph

    @property
    def unparsed(self) -> set[str]:
        """Files the graph tried to read but could not parse; their references are unknown."""
        return self._attempted.difference(self.sources)

    def _load_project_sources(self) -> None:
        for name, path in self.files.items():
            cancellation_point()
            if (
                path.is_symlink()
                or path.suffix.lower() in _SOURCE_SUFFIXES
                or name.endswith((".pdf_tex", ".eps_tex", ".pstex_t"))
            ):
                self._load(name)

    def _document_roots(self) -> list[str]:
        return [
            name
            for name, source in self.sources.items()
            if Path(name).suffix.lower() in ROOT_SUFFIXES and is_document_root(source.commands)
        ]

    def _uncertain(
        self,
        rule: str,
        message: str,
        path: str,
        line: int | None = None,
        suggestion: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        finding = Finding(
            rule,
            message,
            status="inconclusive",
            path=path,
            line=line,
            suggestion=suggestion,
            details=details or {},
        )
        if finding not in self.uncertainties:
            self.uncertainties.append(finding)
            self.findings.append(finding)

    def _names_project_file(self, command: Command) -> bool:
        """Whether a class/package reference can name a file inside the project.

        Lookup context is not established while a file is loaded, so this only
        rejects names with no matching project file under any directory.
        """
        if not command.arguments:
            return False
        suffixes = DEFAULT_SUFFIXES_BY_FILE_COMMAND[command.name]
        for item in split_argument_items(
            command.arguments[0], comma_separated=command.name in COMMA_LIST_FILE_COMMANDS
        ):
            if not is_literal_text(item.value):
                return True
            if item.value in self.known_paths or any(
                item.value + suffix in self.known_paths for suffix in suffixes
            ):
                return True
        return False

    def _load(self, name: str) -> ParsedSource | None:
        if name in self.sources:
            return self.sources[name]
        if name in self._attempted:
            return None
        self._attempted.add(name)
        path = self.files[name]
        if path.is_symlink():
            self._uncertain(
                "source.link",
                "Symbolic links are outside the supported source subset.",
                name,
                suggestion="Supply the actual project file inside the project.",
            )
            return None
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES or self._source_bytes + size > MAX_TOTAL_SOURCE_BYTES:
            self._uncertain(
                "source.size_limit",
                "Source parsing exceeded the 8 MiB per-file or 32 MiB total limit.",
                name,
                suggestion="Reduce the selected source set or inspect this file separately.",
            )
            return None
        try:
            raw = path.read_bytes()
            self._source_bytes += len(raw)
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            self._uncertain(
                "source.encoding",
                "Source could not be read as UTF-8; its encoding was not changed.",
                name,
                suggestion="Convert an explicit copy to UTF-8 before preparing it.",
            )
            return None
        regions, unclosed = source_regions(text)
        masked = blank_regions(text, regions)
        try:
            commands = scan_commands(masked)
        except ParseLimitExceeded:
            self._uncertain(
                "source.argument_limit",
                "An argument is unclosed, exceeds 64 KiB, or exceeds the nested-argument budget.",
                name,
                suggestion="Simplify the source construct or use preserve layout.",
            )
            return None
        self._parsed_commands += len(commands)
        if self._parsed_commands > MAX_COMMANDS:
            self._uncertain(
                "source.command_limit",
                "The project exceeds the bounded lexical command limit.",
                name,
            )
            return None
        source = ParsedSource(text, masked, commands, raw.startswith(b"\xef\xbb\xbf"))
        spans = conditional_spans(commands)
        for command in commands:
            if command.name != "graphicspath" and command.name not in FILE_COMMANDS:
                continue
            if not in_conditional(spans, command.start):
                continue
            # Toolchain classes and packages are covered by recorded build inputs.
            if command.name in TOOLCHAIN_FILE_COMMANDS and not self._names_project_file(command):
                continue
            self._uncertain(
                "source.conditional_dependency",
                "Conditional TeX control flow encloses a file reference; "
                "the static scan does not establish which branch executes.",
                name,
                command.line,
                suggestion="Make the dependency unconditional, "
                "or rely on the build trace for preserve layout.",
            )
        self.sources[name] = source
        for offset in unclosed:
            self._uncertain(
                "source.literal_unclosed",
                "A literal region has no closing delimiter; "
                "later commands cannot be inspected reliably.",
                name,
                text.count("\n", 0, offset) + 1,
            )
        for command in commands:
            self._check_command(name, source, command)
        return source

    def _select_main(self, main: str | None) -> str | None:
        if main is not None:
            candidate = posixpath.normpath(main)
            if (
                main.startswith(("/", "\\"))
                or re.match(r"^[A-Za-z]:", main)
                or candidate == ".."
                or candidate.startswith("../")
                or "\\" in main
                or candidate not in self.files
            ):
                self.findings.append(
                    Finding(
                        "source.main_invalid",
                        "Selected main file must be an existing relative path inside the project.",
                        severity="error",
                        path=main,
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
                "Multiple document roots were found."
                if self.roots
                else "No literal document root could be selected.",
                severity="error",
                status="inconclusive",
                suggestion="Select the main file explicitly with --main.",
                details={"candidates": self.roots},
            )
        )
        return None

    def _check_command(self, name: str, source: ParsedSource, command: Command) -> None:
        if (
            command.name in {"usepackage", "RequirePackage"}
            and command.arguments
            and "subfiles" in {item.value for item in split_argument_items(command.arguments[0])}
            and any("v1" in option.value.split(",") for option in command.options)
        ):
            self._uncertain(
                "source.subfile_structure",
                "The subfiles v1 preamble execution mode is unsupported.",
                name,
                command.line,
            )
        if command.name in {"ProvidesPackage", "ProvidesClass", "ProvidesFile"}:
            if command.arguments and "/" in command.arguments[0].value:
                self._uncertain(
                    "source.path_identity",
                    "The declared package, class, or file identity contains a directory path.",
                    name,
                    command.line,
                    "Use preserve layout to retain this identity.",
                )
        if command.name in _UNSUPPORTED_COMMANDS or (
            command.name in _FONT_COMMANDS and command.name not in _SUPPORTED_FONT_COMMANDS
        ):
            self._uncertain(
                "source.unsupported_command",
                f"\\{command.name} may depend on paths or runtime lookup "
                "outside the supported literal subset.",
                name,
                command.line,
                "Use preserve layout or replace this command with a supported literal dependency.",
                {"command": command.name},
            )
        elif (
            command.name not in FILE_COMMANDS
            and command.name not in _TEXT_COMMANDS
            and command.name not in _MACRO_NAME_COMMANDS
            and command.name != "graphicspath"
        ):
            for argument in (*command.options, *command.arguments):
                if is_literal_text(argument.value) and (
                    _PATH_LIKE.search(argument.value)
                    or ("/" in argument.value and argument.value.strip() in self.known_paths)
                ):
                    self._uncertain(
                        "source.custom_path",
                        f"\\{command.name} has a possible file argument "
                        "whose lookup semantics are unknown.",
                        name,
                        command.line,
                        "Use preserve layout or provide a supported explicit file reference.",
                        {"command": command.name, "argument": argument.value},
                    )
                    break
        if command.name in FILE_COMMANDS or command.name == "graphicspath":
            for option in command.options:
                subfiles_parent = (
                    command.name == "documentclass"
                    and command.arguments
                    and command.arguments[0].value.strip() == "subfiles"
                )
                if (
                    not subfiles_parent
                    and command.name not in _SUPPORTED_FONT_COMMANDS
                    and is_literal_text(option.value)
                    and _PATH_LIKE.search(option.value)
                ):
                    self._uncertain(
                        "source.path_option",
                        f"\\{command.name} has a file-like option "
                        "with unsupported lookup semantics.",
                        name,
                        command.line,
                        "Use preserve layout or simplify this option.",
                    )
            if command.depth and command.name != "graphicspath":
                self._uncertain(
                    "source.nested_file_command",
                    f"\\{command.name} occurs inside a group or macro argument; "
                    "its execution context is not established.",
                    name,
                    command.line,
                    "Use a direct literal reference or preserve layout.",
                )
            file_index = 1 if command.name in SECOND_ARGUMENT_FILE_COMMANDS else 0
            if len(command.arguments) <= file_index:
                self._uncertain(
                    "source.dynamic_path",
                    f"\\{command.name} does not have a supported literal filename argument.",
                    name,
                    command.line,
                )
            for argument in command.arguments[file_index : file_index + 1]:
                if (
                    source.text[argument.start : argument.end]
                    != source.masked[argument.start : argument.end]
                ):
                    self._uncertain(
                        "source.path_literal_context",
                        f"\\{command.name} contains comments or literal commands "
                        "inside its path argument.",
                        name,
                        command.line,
                        "Use a plain filename argument before flattening.",
                    )

    def _error(
        self,
        rule: str,
        message: str,
        name: str,
        command: Command,
        details: dict[str, object] | None = None,
    ) -> None:
        finding = Finding(
            rule, message, severity="error", path=name, line=command.line, details=details or {}
        )
        if finding not in self.findings:
            self.findings.append(finding)

    def _inside(
        self, value: str, name: str, command: Command, *, base: str | None = None
    ) -> str | None:
        if value.startswith(("/", "\\\\", "~")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            self._error(
                "source.external_path",
                f"Dependency {value!r} uses an absolute, external, or user-specific path.",
                name,
                command,
                {"reference": value},
            )
            return None
        path = posixpath.normpath(posixpath.join(self.cwd if base is None else base, value))
        if path == ".." or path.startswith("../"):
            self._error(
                "source.external_path",
                f"Dependency {value!r} resolves outside the selected project.",
                name,
                command,
                {"reference": value},
            )
            return None
        return path

    def _resolve(
        self,
        value: str,
        name: str,
        command: Command,
        search: tuple[str, ...],
        context: tuple[str, ...] = (),
        *,
        base: str | None = None,
    ) -> str | None:
        if (
            base is None
            and context
            and (
                command.name == "subfile"
                or (command.name == "bibliography" and self.subfiles_package_loaded)
            )
        ):
            base = context[0]
        initial = self._inside(value, name, command, base=base)
        if initial is None:
            return None
        if not is_literal_text(value):
            self._uncertain(
                "source.dynamic_path",
                f"\\{command.name} has a dynamic or unsupported filename expression.",
                name,
                command.line,
                "Use preserve layout or make the filename literal.",
                {"command": command.name},
            )
            return None
        suffixes = DEFAULT_SUFFIXES_BY_FILE_COMMAND[command.name]
        paths = [initial]
        if base is None and command.name in {
            "input",
            "include",
            "includegraphics",
            "subfile",
            *TOOLCHAIN_FILE_COMMANDS,
        }:
            for directory in context:
                path = self._inside(value, name, command, base=directory)
                if path is not None:
                    paths.append(path)
        if command.name == "includegraphics":
            for directory in search:
                path = self._inside(posixpath.join(directory, value), name, command)
                if path is not None:
                    paths.append(path)
        candidates: list[str] = []
        for path in paths:
            if command.name == "include":
                candidates.append(path + ".tex")
            elif PurePosixPath(path).suffix:
                candidates.append(path)
            else:
                # TeX appends each default suffix before trying the bare name.
                candidates.extend(path + suffix for suffix in suffixes)
                candidates.append(path)
        # Candidates keep TeX's lookup order: base paths first, then declared search
        # directories, and the extensions each command tries within one directory.
        found: list[str] = []
        for candidate in candidates:
            if candidate not in self.files or candidate in found:
                continue
            if self.files[candidate].is_symlink():
                self._load(candidate)
                continue
            found.append(candidate)
        if len(found) > 1:
            self.findings.append(
                Finding(
                    "source.ambiguous_dependency",
                    f"Reference {value!r} also matches {', '.join(found[1:])}; "
                    f"TeX's lookup order selects {found[0]}.",
                    severity="warning",
                    status="passed",
                    path=name,
                    line=command.line,
                    evidence_kind=EvidenceKind.HEURISTIC,
                    suggestion="Name the intended file with an explicit extension "
                    "to remove the dependence on lookup order.",
                    details={
                        "reference": value,
                        "selected": found[0],
                        "alternatives": found[1:],
                        "resolved_by": "TeX lookup order",
                    },
                )
            )
        if found:
            self.dependencies.add(found[0])
            return found[0]
        if command.name in TOOLCHAIN_FILE_COMMANDS and "/" not in value:
            (suffix,) = suffixes
            basename = value if PurePosixPath(value).suffix else value + suffix
            self.system_references.append(SystemReference(name, command, value, basename))
            return None
        mismatches = sorted(
            {
                path
                for candidate in candidates
                for path in self.files
                if path_comparison_key(candidate) == path_comparison_key(path)
            }
        )
        self._error(
            "source.case_mismatch" if mismatches else "source.missing_dependency",
            f"Dependency {value!r} differs in case or Unicode normalization "
            f"from: {', '.join(mismatches)}."
            if mismatches
            else f"Literal dependency {value!r} is missing from the project.",
            name,
            command,
            {"reference": value, "candidates": mismatches or candidates},
        )
        return None

    def _search_paths(
        self, name: str, command: Command, context: tuple[str, ...] = ()
    ) -> tuple[str, ...] | None:
        if not command.arguments:
            return None
        argument = command.arguments[0]
        if command.depth:
            self._uncertain(
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
                self._uncertain(
                    "source.dynamic_search_path",
                    "\\graphicspath must contain a literal list such as {{figures/}{plots/}}.",
                    name,
                    command.line,
                )
                return None
            value = group.value.strip()
            if not value.endswith("/"):
                self._uncertain(
                    "source.search_path_syntax",
                    "A supported graphicspath directory must end with a slash.",
                    name,
                    command.line,
                )
                return None
            if context and self.subfiles_package_loaded:
                value = posixpath.relpath(posixpath.join(context[0], value), self.cwd or ".") + "/"
            if self._inside(value, name, command) is None:
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
            self._error(
                "source.context_ambiguity",
                "The same reference selects different files under different path contexts.",
                name,
                command,
                {"candidates": [previous.target, target]},
            )
        else:
            self.references[location] = FileReference(target, command.name)

    def _font_directory(self, name: str, source: ParsedSource, command: Command) -> str | None:
        if not command.arguments or PurePosixPath(
            command.arguments[0].value.strip()
        ).suffix.lower() not in {".otf", ".ttf", ".ttc"}:
            self._uncertain(
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
                self._uncertain("source.font_options", "Font options contain masked text.", name)
                return None
            for part in option.value.split(","):
                key, separator, value = part.partition("=")
                key, value = key.strip(), value.strip()
                if value.startswith("{") and value.endswith("}"):
                    value = value[1:-1]
                if (
                    not separator
                    or key not in {"Path", "Ligatures", "Scale", "Renderer", "Numbers"}
                    or not is_literal_text(value)
                ):
                    self._uncertain(
                        "source.font_options",
                        "Font options may select additional faces or dynamic filenames; "
                        "only Path, Ligatures, Scale, Renderer and Numbers are supported.",
                        name,
                        command.line,
                    )
                    return None
                if key == "Path":
                    if seen_path or not value.endswith("/"):
                        self._uncertain(
                            "source.font_options",
                            "A font Path must be a single literal directory ending in '/'.",
                            name,
                            command.line,
                        )
                        return None
                    seen_path = True
                    directory = value
                    match = re.search(r"(?:^|,)\s*Path\s*=\s*(\{[^{}]*\}|[^,]*)", option.value)
                    if match:
                        start, end = match.span(1)
                        location = ArgumentLocation(name, option.start + start, option.start + end)
                        self.search_arguments[location] = (
                            "{./}" if option.value[start:end].startswith("{") else "./"
                        )
        return directory

    def _subfile_commands(self, name: str, source: ParsedSource) -> list[Command]:
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
            self._uncertain(
                "source.subfile_structure",
                "A subfile requires one literal subfiles class and document environment.",
                name,
            )
            return []
        declaration = declarations[0]
        if len(declaration.options) != 1 or not is_literal_text(declaration.options[0].value):
            self._uncertain(
                "source.subfile_structure", "A subfile requires an explicit parent path.", name
            )
            return []
        parent_argument = declaration.options[0].stripped()
        parent = self._inside(
            parent_argument.value, name, declaration, base=posixpath.dirname(name)
        )
        if parent is None or parent != self.main:
            self._uncertain(
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

    def _visit(
        self,
        name: str,
        search: tuple[str, ...],
        stack: tuple[str, ...],
        context: tuple[str, ...] = (),
        subfile: bool = False,
    ) -> tuple[str, ...]:
        if name in stack:
            self._uncertain(
                "source.input_cycle",
                "Source input contains a cycle; execution order is not established.",
                name,
                details={"cycle": [*stack, name]},
            )
            return search
        if len(stack) >= MAX_INPUT_DEPTH:
            self._uncertain(
                "source.input_depth",
                "Source input exceeds the bounded dependency traversal depth.",
                name,
            )
            return search
        state = _TraversalState(name, search, context, subfile, self.subfiles_package_loaded)
        if state in self._cache:
            return self._cache[state]
        source = self._load(name)
        if source is None:
            return search
        self.visited.add(name)
        commands = self._subfile_commands(name, source) if subfile else source.commands
        for command in commands:
            self._events += 1
            if self._events > MAX_COMMANDS:
                self._uncertain(
                    "source.expansion_limit",
                    "Dependency traversal exceeded its bounded command budget.",
                    name,
                    command.line,
                )
                return search
            if command.name == "graphicspath":
                paths = self._search_paths(name, command, context)
                if paths is not None:
                    search = paths
                continue
            if command.name not in FILE_COMMANDS or not command.arguments:
                continue
            if command.name in {"usepackage", "RequirePackage"} and any(
                item.value == "subfiles" for item in split_argument_items(command.arguments[0])
            ):
                self.subfiles_package_loaded = True
            if command.name == "documentclass" and command.arguments[0].value == "subfiles":
                self._uncertain(
                    "source.path_option",
                    "Compiling a subfiles class as the main root requires inherited preamble "
                    "semantics outside this subset; select the parent document.",
                    name,
                    command.line,
                )
                continue
            if command.name in IMPORT_COMMANDS:
                if len(command.arguments) < 2:
                    continue
                directory_argument, argument = (item.stripped() for item in command.arguments[:2])
                if not all(
                    is_literal_text(item.value) and source.text[item.start : item.end] == item.value
                    for item in (directory_argument, argument)
                ):
                    self._uncertain(
                        "source.dynamic_path",
                        "Import paths must be plain literal arguments.",
                        name,
                        command.line,
                    )
                    continue
                parent = context[0] if context and command.name.startswith("sub") else self.cwd
                directory = self._inside(directory_argument.value, name, command, base=parent)
                if directory is None:
                    continue
                target = self._resolve(argument.value, name, command, (), base=directory)
                if target is not None:
                    self._record_reference(name, argument, target, command)
                    location = ArgumentLocation(
                        name, directory_argument.start, directory_argument.end
                    )
                    self.search_arguments[location] = "./"
                    self._visit(target, search, (*stack, name), (directory, *context))
                continue
            index = 1 if command.name in SECOND_ARGUMENT_FILE_COMMANDS else 0
            if len(command.arguments) <= index:
                continue
            font_directory = None
            if command.name in _SUPPORTED_FONT_COMMANDS:
                directory = self._font_directory(name, source, command)
                if directory is None:
                    continue
                font_directory = self._inside(directory, name, command)
                if font_directory is None:
                    continue
            for argument in split_argument_items(
                command.arguments[index], comma_separated=command.name in COMMA_LIST_FILE_COMMANDS
            ):
                if (
                    source.text[argument.start : argument.end]
                    != source.masked[argument.start : argument.end]
                ):
                    continue
                target = self._resolve(
                    argument.value, name, command, search, context, base=font_directory
                )
                if target is None:
                    continue
                self._record_reference(name, argument, target, command)
                if command.name == "subfile":
                    self._visit(
                        target,
                        search,
                        (*stack, name),
                        (posixpath.dirname(target), *context),
                        True,
                    )
                if command.name in _TRAVERSED_COMMANDS:
                    search = self._visit(target, search, (*stack, name), context)
        self._cache[state] = search
        return search
