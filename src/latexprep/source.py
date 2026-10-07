"""Conservative source inspection and literal, command-aware flat-layout planning.

This is a lexical subset, not a TeX interpreter. Resolution uses the selected
main file's compilation directory and supported literal import/subfile contexts.
Flat plans reject dynamic paths, unknown file readers, scoped paths and ambiguous lookups; callers
must reject plans with error findings before applying any mapping or contents.
"""

from __future__ import annotations

import bisect
import difflib
import posixpath
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import Change, Finding, PreparationError
from .scheduler import cancellation_point

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_COMMANDS = 100_000
MAX_ARGUMENT_CHARS = 64 * 1024
MAX_INPUT_DEPTH = 128


class _ParseLimit(Exception):
    """The lexical subset cannot establish bounded, complete argument spans."""


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
_ROOT_SUFFIXES = {".tex", ".ltx", ".latex"}
# Manuscript text, as opposed to class, style and generated template internals.
_MANUSCRIPT_SUFFIXES = {".tex", ".ltx", ".latex", ".tikz", ".pgf"}
# Graphics lookup order of the default pdfTeX graphics rules.
_GRAPHICS_SUFFIXES = (".pdf", ".png", ".jpg", ".mps", ".jpeg", ".jbig2", ".jb2", ".eps", ".ps")
_LITERAL_ENVS = {
    "verbatim",
    "verbatim*",
    "Verbatim",
    "BVerbatim",
    "LVerbatim",
    "lstlisting",
    "minted",
    "comment",
}
_FILE_COMMANDS = {
    "input": ".tex",
    "include": ".tex",
    "includegraphics": "graphics",
    "bibliography": ".bib",
    "addbibresource": ".bib",
    "bibliographystyle": ".bst",
    "documentclass": ".cls",
    "usepackage": ".sty",
    "RequirePackage": ".sty",
    "RequirePackageWithOptions": ".sty",
    "LoadClass": ".cls",
    "LoadClassWithOptions": ".cls",
    "import": ".tex",
    "subimport": ".tex",
    "inputfrom": ".tex",
    "subinputfrom": ".tex",
    "subfile": ".tex",
    "pgfplotstableread": "",
    "pgfplotstabletypeset": "",
    "csvreader": "",
    "csvautotabular": "",
    "DTLloaddb": "",
    "lstinputlisting": "",
    "inputminted": "",
    "verbatiminput": "",
    "VerbatimInput": "",
    "includepdf": ".pdf",
    "setmainfont": "",
    "setsansfont": "",
    "setmonofont": "",
    "fontspec": "",
}
_IMPORT_COMMANDS = {"import", "subimport", "inputfrom", "subinputfrom"}
_SUPPORTED_FONTS = {"setmainfont", "setsansfont", "setmonofont", "fontspec"}
_SECOND_FILE_ARGUMENT = {*_IMPORT_COMMANDS, "DTLloaddb", "inputminted"}
_SYSTEM_COMMANDS = {
    "bibliographystyle",
    "documentclass",
    "usepackage",
    "RequirePackage",
    "RequirePackageWithOptions",
    "LoadClass",
    "LoadClassWithOptions",
}
_LIST_COMMANDS = {"bibliography", "usepackage", "RequirePackage", "RequirePackageWithOptions"}
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
# Ordinary formatting such as \textcolor is not an editing marker.
_EDIT_COMMANDS = {
    "todo",
    "TODO",
    "hl",
    "added",
    "deleted",
    "replaced",
    "showframe",
    "linenumbers",
    "SetWatermarkText",
    "draftwatermark",
}
_REFERENCE_COMMANDS = {"ref", "pageref", "eqref", "autoref", "cref", "Cref", "nameref", "vref"}
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_TOKEN = re.compile(r"\\([A-Za-z@]+|[^\r\n])")
_PATH_LIKE = re.compile(
    r"(?:^|[\s,=])[^\s{}]+\.(?:tex|ltx|bib|bst|sty|cls|pdf|png|jpe?g|eps|svg|csv|tsv|dat|txt|json|xml|ttf|otf)(?:$|[\s,])",
    re.I,
)


@dataclass
class SourceAnalysis:
    roots: list[str]
    main: str | None
    findings: list[Finding] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)
    # Completeness is bounded by the supported literal subset, not arbitrary TeX execution.
    complete: bool = False


@dataclass
class FlattenPlan:
    mapping: dict[str, str] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


@dataclass(frozen=True)
class _Argument:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class _Command:
    name: str
    start: int
    line: int
    depth: int
    arguments: tuple[_Argument, ...]
    options: tuple[_Argument, ...]


@dataclass
class _Source:
    text: str
    masked: str
    commands: list[_Command]
    bom: bool = False


def _blank(buffer: list[str], start: int, end: int) -> None:
    buffer[start:end] = [char if char in "\r\n" else " " for char in buffer[start:end]]


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _skip_trivia(text: str, pos: int) -> int:
    while True:
        pos = _skip_space(text, pos)
        if pos < len(text) and text[pos] == "%":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
        else:
            return pos


def _group(text: str, pos: int, opening: str = "{", closing: str = "}") -> _Argument | None:
    if pos >= len(text) or text[pos] != opening:
        return None
    depth, cursor = 1, pos + 1
    brace_depth = 0
    while cursor < len(text) and cursor - pos <= MAX_ARGUMENT_CHARS:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if opening == "[":
            if char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
            if brace_depth:
                cursor += 1
                continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return _Argument(pos + 1, cursor, text[pos + 1 : cursor])
        cursor += 1
    return None


def _mask(text: str, *, regions: list[tuple[int, int, str]] | None = None) -> tuple[str, list[int]]:
    buffer = list(text)
    incomplete: list[int] = []
    pos = 0
    while pos < len(text):
        if pos % 4096 == 0:
            cancellation_point()
        if text[pos] == "%":
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            _blank(buffer, pos, end)
            if regions is not None:
                regions.append((pos, end - (text[end - 1 : end] == "\r"), "comment"))
            pos = end
            continue
        if text[pos] != "\\":
            pos += 1
            continue
        token = _TOKEN.match(text, pos)
        if not token:
            pos += 1
            continue
        name, end = token.group(1), token.end()
        if name == "begin":
            arg = _group(text, _skip_trivia(text, end))
            if arg and arg.value.strip() in _LITERAL_ENVS:
                env = arg.value.strip()
                stop = re.search(r"\\end\s*\{\s*" + re.escape(env) + r"\s*\}", text[arg.end + 1 :])
                if stop:
                    end = arg.end + 1 + stop.end()
                else:
                    end = len(text)
                    incomplete.append(pos)
                _blank(buffer, pos, end)
                if regions is not None:
                    regions.append((pos, end, "literal"))
                pos = end
                continue
        if name in {"verb", "lstinline", "mintinline", "url", "nolinkurl", "path"}:
            cursor = end
            if cursor < len(text) and text[cursor] == "*":
                cursor += 1
            if name != "verb":
                cursor = _skip_trivia(text, cursor)
                option = _group(text, cursor, "[", "]")
                if option:
                    cursor = _skip_trivia(text, option.end + 1)
                if name == "mintinline":
                    language = _group(text, cursor)
                    if language:
                        cursor = _skip_trivia(text, language.end + 1)
            if cursor < len(text) and not text[cursor].isspace():
                if text[cursor] == "{" and name != "verb":
                    arg = _group(text, cursor)
                    end = arg.end + 1 if arg else len(text)
                    if not arg:
                        incomplete.append(pos)
                else:
                    delimiter = text[cursor]
                    line_end = text.find("\n", cursor)
                    line_end = len(text) if line_end < 0 else line_end
                    close = text.find(delimiter, cursor + 1, line_end)
                    end = close + 1 if close >= 0 else line_end
                    if close < 0:
                        incomplete.append(pos)
                _blank(buffer, pos, end)
                if regions is not None:
                    regions.append((pos, end, "literal"))
                pos = end
                continue
        pos = end
    return "".join(buffer), incomplete


def mask_literals(text: str) -> str:
    """Blank comments and common literal regions while retaining offsets/newlines."""
    return _mask(text)[0]


def source_regions(text: str) -> tuple[list[tuple[int, int, str]], list[int]]:
    """Return comment/literal spans and unclosed offsets without TeX expansion."""
    regions: list[tuple[int, int, str]] = []
    _, incomplete = _mask(text, regions=regions)
    return regions, incomplete


def _commands(masked: str) -> list[_Command]:
    result: list[_Command] = []
    depth = pos = 0
    line = 1
    argument_chars = 0
    while pos < len(masked):
        if pos % 4096 == 0:
            cancellation_point()
        char = masked[pos]
        if char == "\n":
            line += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "\\":
            token = _TOKEN.match(masked, pos)
            if token:
                cursor = token.end()
                if cursor < len(masked) and masked[cursor] == "*":
                    cursor += 1
                arguments: list[_Argument] = []
                options: list[_Argument] = []
                name = token.group(1)
                for _ in range(6):
                    skipped = _skip_space(masked, cursor)
                    # A paragraph break ends argument scanning: the next group starts
                    # new text instead of completing this command.
                    if masked.count("\n", cursor, skipped) > 1:
                        break
                    cursor = skipped
                    option = _group(masked, cursor, "[", "]")
                    arg = _group(masked, cursor)
                    if option:
                        options.append(option)
                        cursor = option.end + 1
                    elif arg:
                        arguments.append(arg)
                        cursor = arg.end + 1
                    else:
                        # An unbalanced brace leaves every later argument span
                        # unreadable, but an unmatched bracket is ordinary text such
                        # as "[0, 1)" and only ends this command's arguments.
                        if cursor < len(masked) and masked[cursor] == "{":
                            raise _ParseLimit
                        break
                argument_chars += sum(len(arg.value) for arg in (*arguments, *options))
                if argument_chars > min(MAX_SOURCE_BYTES, len(masked) * 4):
                    raise _ParseLimit
                if not arguments and name == "input":
                    start = _skip_space(masked, token.end())
                    match = re.match(r"[^\s\\{}\[\]]+", masked[start:])
                    if match:
                        arguments.append(_Argument(start, start + match.end(), match.group()))
                result.append(_Command(name, pos, line, depth, tuple(arguments), tuple(options)))
                if len(result) > MAX_COMMANDS:
                    return result
                pos = token.end()
                continue
        pos += 1
    return result


# Tokens after these commands name a macro being defined, not a conditional being run.
_DEFINITION_COMMANDS = frozenset(
    {"newif", "def", "gdef", "edef", "xdef", "let", "futurelet", "global", "long", "protected"}
)
# Package conditionals that take braced branches instead of ending with \fi.
_ARGUMENT_CONDITIONALS = frozenset(
    {
        "ifthenelse",
        "iftoggle",
        "ifbool",
        "ifboolexpr",
        "ifdef",
        "ifundef",
        "ifdefempty",
        "ifdefvoid",
        "ifdefstring",
        "ifcsdef",
        "ifcsundef",
        "ifcsvoid",
        "ifstrequal",
        "ifstrempty",
        "ifblank",
        "ifnumcomp",
        "ifdimcomp",
        "ifpackageloaded",
        "ifclassloaded",
    }
)


def conditional_spans(commands: list[_Command]) -> list[tuple[int, int]]:
    """Return offset ranges enclosed by primitive-style ``\\if…`` … ``\\fi`` conditionals.

    A conditional name defined by ``\\newif`` or ``\\def`` is not itself executed,
    and an opener without a matching ``\\fi`` (such as a branch-taking package
    conditional) is ignored rather than extended to the end of the file.
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    previous: _Command | None = None
    for command in commands:
        defined = (
            previous is not None
            and previous.name in _DEFINITION_COMMANDS
            and not previous.arguments
            and command.start == previous.start + len(previous.name) + 1
        )
        if command.name == "fi":
            if stack:
                start = stack.pop()
                spans.append((start, command.start))
        elif (
            command.name.startswith("if")
            and command.name not in _ARGUMENT_CONDITIONALS
            and not defined
        ):
            stack.append(command.start)
        previous = command
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def in_conditional(spans: list[tuple[int, int]], offset: int) -> bool:
    """Whether ``offset`` lies inside one of the merged ``conditional_spans``."""
    index = bisect.bisect_right(spans, (offset, float("inf"))) - 1
    return index >= 0 and spans[index][0] < offset < spans[index][1]


def _key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _inventory(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise PreparationError(f"Source project is not a directory: {root}")
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(
            root.rglob("*"), key=lambda path: (_key(path.as_posix()), path.as_posix())
        )
        if path.is_file() or path.is_symlink()
    }


def discover_roots(root: Path) -> list[str]:
    """Find literal document roots; oversized, linked or non-UTF-8 files are skipped.

    Full analysis reports skipped-source diagnostics. This preflight performs no
    dependency lookup and never chooses between multiple plausible entry points.
    """
    roots: list[str] = []
    for name, path in _inventory(root).items():
        if path.suffix.lower() not in _ROOT_SUFFIXES or path.is_symlink():
            continue
        if path.stat().st_size > MAX_SOURCE_BYTES:
            continue
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            if _is_root(_commands(mask_literals(text))):
                roots.append(name)
        except _ParseLimit:
            continue
    return roots


def _is_root(commands: list[_Command]) -> bool:
    return any(
        command.name == "documentclass"
        or (
            command.name == "begin"
            and command.arguments
            and command.arguments[0].value.strip() == "document"
        )
        for command in commands
    )


def _literal(value: str) -> bool:
    return bool(value) and not any(char in value for char in "\\{}#$^~&%\n\r\x00")


def _trim(arg: _Argument) -> _Argument:
    start = len(arg.value) - len(arg.value.lstrip())
    value = arg.value.strip()
    return _Argument(arg.start + start, arg.start + start + len(value), value)


def _items(arg: _Argument, multiple: bool) -> list[_Argument]:
    if not multiple:
        return [_trim(arg)]
    items: list[_Argument] = []
    cursor = 0
    for value in arg.value.split(","):
        items.append(_trim(_Argument(arg.start + cursor, arg.start + cursor + len(value), value)))
        cursor += len(value) + 1
    return items


class _Inspection:
    def __init__(self, root: Path, main: str | None, *, selected_only: bool = False):
        if selected_only and main is None:
            raise PreparationError("Selected source analysis requires an explicit main file.")
        self.root = root.resolve()
        self.files = _inventory(root)
        self.known_paths: set[str] = set()
        for filename in self.files:
            parts = filename.split("/")
            self.known_paths.update("/".join(parts[start:]) for start in range(len(parts)))
            self.known_paths.update("/".join(parts[:end]) + "/" for end in range(1, len(parts)))
        self.sources: dict[str, _Source] = {}
        self.findings: list[Finding] = []
        self.uncertainties: list[Finding] = []
        self.dependencies: set[str] = set()
        self.references: dict[tuple[str, int, int], tuple[str, str]] = {}
        self.search_arguments: dict[tuple[str, int, int], str] = {}
        self.system_references: list[tuple[str, _Command, str, str]] = []
        self.visited: set[str] = set()
        self.subfile_targets: set[str] = set()
        self.subfiles_package_loaded = False
        self._cache: dict[
            tuple[str, tuple[str, ...], tuple[str, ...], bool, bool], tuple[str, ...]
        ] = {}
        self._source_bytes = 0
        self._parsed_commands = 0
        self._unparsed: set[str] = set()
        self._events = 0
        if not selected_only:
            for name, path in self.files.items():
                cancellation_point()
                if (
                    path.is_symlink()
                    or path.suffix.lower() in _SOURCE_SUFFIXES
                    or name.endswith((".pdf_tex", ".eps_tex", ".pstex_t"))
                ):
                    self._load(name)
        self.roots = [
            name
            for name, source in self.sources.items()
            if Path(name).suffix.lower() in _ROOT_SUFFIXES and _is_root(source.commands)
        ]
        self.main = self._select_main(main)
        if self.main:
            self.cwd = posixpath.dirname(self.main)
            self.dependencies.add(self.main)
            self._visit(self.main, (), ())
        else:
            self.cwd = ""
        if selected_only:
            self.roots = [
                name
                for name, source in self.sources.items()
                if Path(name).suffix.lower() in _ROOT_SUFFIXES and _is_root(source.commands)
            ]
        self._source_checks()

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

    def _names_project_file(self, command: _Command) -> bool:
        """Whether a class/package reference can name a file inside the project.

        Lookup context is not established while a file is loaded, so this only
        rejects names with no matching project file under any directory.
        """
        if not command.arguments:
            return False
        suffix = _FILE_COMMANDS[command.name]
        for item in _items(command.arguments[0], command.name in _LIST_COMMANDS):
            if not _literal(item.value):
                return True
            if item.value in self.known_paths or item.value + suffix in self.known_paths:
                return True
        return False

    def _load(self, name: str) -> _Source | None:
        if name in self.sources:
            return self.sources[name]
        if name in self._unparsed:
            return None
        self._unparsed.add(name)
        path = self.files[name]
        if path.is_symlink():
            self._uncertain(
                "source-link",
                "Symbolic links are outside the supported source subset.",
                name,
                suggestion="Supply the actual project file inside the project.",
            )
            return None
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES or self._source_bytes + size > MAX_TOTAL_SOURCE_BYTES:
            self._uncertain(
                "source-size-limit",
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
                "source-encoding",
                "Source could not be read as UTF-8; its encoding was not changed.",
                name,
                suggestion="Convert an explicit copy to UTF-8 before preparing it.",
            )
            return None
        masked, incomplete = _mask(text)
        try:
            commands = _commands(masked)
        except _ParseLimit:
            self._uncertain(
                "source-argument-limit",
                "An argument is unclosed, exceeds 64 KiB, or exceeds the nested-argument budget.",
                name,
                suggestion="Simplify the source construct or use preserve layout.",
            )
            return None
        self._parsed_commands += len(commands)
        if self._parsed_commands > MAX_COMMANDS:
            self._uncertain(
                "source-command-limit",
                "The project exceeds the bounded lexical command limit.",
                name,
            )
            return None
        source = _Source(text, masked, commands, raw.startswith(b"\xef\xbb\xbf"))
        spans = conditional_spans(commands)
        for command in commands:
            if command.name != "graphicspath" and command.name not in _FILE_COMMANDS:
                continue
            if not in_conditional(spans, command.start):
                continue
            # Toolchain classes and packages are covered by recorded build inputs.
            if command.name in _SYSTEM_COMMANDS and not self._names_project_file(command):
                continue
            self._uncertain(
                "source-conditional-dependency",
                "Conditional TeX control flow encloses a file reference; "
                "the static scan does not establish which branch executes.",
                name,
                command.line,
                suggestion="Make the dependency unconditional, "
                "or rely on the build trace for preserve layout.",
            )
        self.sources[name] = source
        for offset in incomplete:
            self._uncertain(
                "source-literal-unclosed",
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
                        "source-main-invalid",
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
                "source-root-ambiguous" if self.roots else "source-root-missing",
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

    def _check_command(self, name: str, source: _Source, command: _Command) -> None:
        if (
            command.name in {"usepackage", "RequirePackage"}
            and command.arguments
            and "subfiles" in {item.value for item in _items(command.arguments[0], True)}
            and any("v1" in option.value.split(",") for option in command.options)
        ):
            self._uncertain(
                "source-subfile-structure",
                "The subfiles v1 preamble execution mode is unsupported.",
                name,
                command.line,
            )
        if command.name in {"ProvidesPackage", "ProvidesClass", "ProvidesFile"}:
            if command.arguments and "/" in command.arguments[0].value:
                self._uncertain(
                    "source-path-identity",
                    "The declared package, class, or file identity contains a directory path.",
                    name,
                    command.line,
                    "Use preserve layout to retain this identity.",
                )
        if command.name in _UNSUPPORTED_COMMANDS or (
            command.name in _FONT_COMMANDS and command.name not in _SUPPORTED_FONTS
        ):
            self._uncertain(
                "source-unsupported-command",
                f"\\{command.name} may depend on paths or runtime lookup "
                "outside the supported literal subset.",
                name,
                command.line,
                "Use preserve layout or replace this command with a supported literal dependency.",
                {"command": command.name},
            )
        elif (
            command.name not in _FILE_COMMANDS
            and command.name not in _TEXT_COMMANDS
            and command.name != "graphicspath"
        ):
            for argument in (*command.options, *command.arguments):
                if _literal(argument.value) and (
                    _PATH_LIKE.search(argument.value)
                    or ("/" in argument.value and argument.value.strip() in self.known_paths)
                ):
                    self._uncertain(
                        "source-custom-path",
                        f"\\{command.name} has a possible file argument "
                        "whose lookup semantics are unknown.",
                        name,
                        command.line,
                        "Use preserve layout or provide a supported explicit file reference.",
                        {"command": command.name, "argument": argument.value},
                    )
                    break
        if command.name in _FILE_COMMANDS or command.name == "graphicspath":
            for option in command.options:
                subfiles_parent = (
                    command.name == "documentclass"
                    and command.arguments
                    and command.arguments[0].value.strip() == "subfiles"
                )
                if (
                    not subfiles_parent
                    and command.name not in _SUPPORTED_FONTS
                    and _literal(option.value)
                    and _PATH_LIKE.search(option.value)
                ):
                    self._uncertain(
                        "source-path-option",
                        f"\\{command.name} has a file-like option "
                        "with unsupported lookup semantics.",
                        name,
                        command.line,
                        "Use preserve layout or simplify this option.",
                    )
            if command.depth and command.name != "graphicspath":
                self._uncertain(
                    "source-nested-file-command",
                    f"\\{command.name} occurs inside a group or macro argument; "
                    "its execution context is not established.",
                    name,
                    command.line,
                    "Use a direct literal reference or preserve layout.",
                )
            file_index = 1 if command.name in _SECOND_FILE_ARGUMENT else 0
            if len(command.arguments) <= file_index:
                self._uncertain(
                    "source-dynamic-path",
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
                        "source-path-literal-context",
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
        command: _Command,
        details: dict[str, object] | None = None,
    ) -> None:
        finding = Finding(
            rule, message, severity="error", path=name, line=command.line, details=details or {}
        )
        if finding not in self.findings:
            self.findings.append(finding)

    def _inside(
        self, value: str, name: str, command: _Command, *, base: str | None = None
    ) -> str | None:
        if value.startswith(("/", "\\\\", "~")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            self._error(
                "source-external-path",
                f"Dependency {value!r} uses an absolute, external, or user-specific path.",
                name,
                command,
                {"reference": value},
            )
            return None
        path = posixpath.normpath(posixpath.join(self.cwd if base is None else base, value))
        if path == ".." or path.startswith("../"):
            self._error(
                "source-external-path",
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
        command: _Command,
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
        if not _literal(value):
            self._uncertain(
                "source-dynamic-path",
                f"\\{command.name} has a dynamic or unsupported filename expression.",
                name,
                command.line,
                "Use preserve layout or make the filename literal.",
                {"command": command.name},
            )
            return None
        suffix = _FILE_COMMANDS[command.name]
        paths = [initial]
        if base is None and command.name in {
            "input",
            "include",
            "includegraphics",
            "subfile",
            *_SYSTEM_COMMANDS,
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
            elif suffix == "graphics":
                candidates.extend(path + extension for extension in _GRAPHICS_SUFFIXES)
                candidates.append(path)
            elif suffix:
                # TeX appends the default suffix before trying the bare name.
                candidates.extend((path + suffix, path))
            else:
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
                    "source-ambiguous-dependency",
                    f"Reference {value!r} also matches {', '.join(found[1:])}; "
                    f"TeX's lookup order selects {found[0]}.",
                    severity="warning",
                    status="passed",
                    path=name,
                    line=command.line,
                    evidence="heuristic",
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
        if command.name in _SYSTEM_COMMANDS and "/" not in value:
            basename = value if PurePosixPath(value).suffix else value + suffix
            self.system_references.append((name, command, value, basename))
            return None
        mismatches = sorted(
            {
                path
                for candidate in candidates
                for path in self.files
                if _key(candidate) == _key(path)
            }
        )
        self._error(
            "source-case-mismatch" if mismatches else "source-missing-dependency",
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
        self, name: str, command: _Command, context: tuple[str, ...] = ()
    ) -> tuple[str, ...] | None:
        if not command.arguments:
            return None
        argument = command.arguments[0]
        if command.depth:
            self._uncertain(
                "source-scoped-search-path",
                "A scoped or macro-defined \\graphicspath is outside the supported subset.",
                name,
                command.line,
            )
            return None
        paths: list[str] = []
        pos = 0
        while _skip_space(argument.value, pos) < len(argument.value):
            pos = _skip_space(argument.value, pos)
            group = _group(argument.value, pos)
            if group is None or not _literal(group.value.strip()):
                self._uncertain(
                    "source-dynamic-search-path",
                    "\\graphicspath must contain a literal list such as {{figures/}{plots/}}.",
                    name,
                    command.line,
                )
                return None
            value = group.value.strip()
            if not value.endswith("/"):
                self._uncertain(
                    "source-search-path-syntax",
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
        self.search_arguments[(name, argument.start, argument.end)] = "{./}"
        return tuple(paths)

    def _record_reference(self, name: str, argument: _Argument, target: str, command: _Command):
        key = (name, argument.start, argument.end)
        previous = self.references.get(key)
        if previous and previous[0] != target:
            self._error(
                "source-context-ambiguity",
                "The same reference selects different files under different path contexts.",
                name,
                command,
                {"candidates": [previous[0], target]},
            )
        else:
            self.references[key] = (target, command.name)

    def _font_directory(self, name: str, source: _Source, command: _Command) -> str | None:
        if not command.arguments or PurePosixPath(
            command.arguments[0].value.strip()
        ).suffix.lower() not in {".otf", ".ttf", ".ttc"}:
            self._uncertain(
                "source-unsupported-command",
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
                self._uncertain("source-font-options", "Font options contain masked text.", name)
                return None
            for part in option.value.split(","):
                key, separator, value = part.partition("=")
                key, value = key.strip(), value.strip()
                if value.startswith("{") and value.endswith("}"):
                    value = value[1:-1]
                if (
                    not separator
                    or key not in {"Path", "Ligatures", "Scale", "Renderer", "Numbers"}
                    or not _literal(value)
                ):
                    self._uncertain(
                        "source-font-options",
                        "Font options may select additional faces or dynamic filenames; "
                        "only Path, Ligatures, Scale, Renderer and Numbers are supported.",
                        name,
                        command.line,
                    )
                    return None
                if key == "Path":
                    if seen_path or not value.endswith("/"):
                        self._uncertain(
                            "source-font-options",
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
                        self.search_arguments[(name, option.start + start, option.start + end)] = (
                            "{./}" if option.value[start:end].startswith("{") else "./"
                        )
        return directory

    def _subfile_commands(self, name: str, source: _Source) -> list[_Command]:
        declarations = [c for c in source.commands if c.name == "documentclass"]
        starts = [
            c
            for c in source.commands
            if c.name == "begin" and c.arguments and c.arguments[0].value == "document"
        ]
        ends = [
            c
            for c in source.commands
            if c.name == "end" and c.arguments and c.arguments[0].value == "document"
        ]
        if (
            len(declarations) != 1
            or len(starts) != 1
            or len(ends) != 1
            or not declarations[0].arguments
            or declarations[0].arguments[0].value != "subfiles"
            or not declarations[0].start < starts[0].start < ends[0].start
        ):
            self._uncertain(
                "source-subfile-structure",
                "A subfile requires one literal subfiles class and document environment.",
                name,
            )
            return []
        declaration = declarations[0]
        if len(declaration.options) != 1 or not _literal(declaration.options[0].value):
            self._uncertain(
                "source-subfile-structure", "A subfile requires an explicit parent path.", name
            )
            return []
        parent_arg = _trim(declaration.options[0])
        parent = self._inside(parent_arg.value, name, declaration, base=posixpath.dirname(name))
        if parent is None or parent != self.main:
            self._uncertain(
                "source-subfile-structure",
                "The subfile parent must name the selected main file exactly.",
                name,
            )
            return []
        self.references[(name, parent_arg.start, parent_arg.end)] = (parent, "input")
        self.subfile_targets.add(name)
        return [c for c in source.commands if starts[0].start < c.start < ends[0].start]

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
                "source-input-cycle",
                "Source input contains a cycle; execution order is not established.",
                name,
                details={"cycle": [*stack, name]},
            )
            return search
        if len(stack) >= MAX_INPUT_DEPTH:
            self._uncertain(
                "source-input-depth",
                "Source input exceeds the bounded dependency traversal depth.",
                name,
            )
            return search
        key = (name, search, context, subfile, self.subfiles_package_loaded)
        if key in self._cache:
            return self._cache[key]
        source = self._load(name)
        if source is None:
            return search
        self.visited.add(name)
        commands = self._subfile_commands(name, source) if subfile else source.commands
        for command in commands:
            self._events += 1
            if self._events > MAX_COMMANDS:
                self._uncertain(
                    "source-expansion-limit",
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
            if command.name not in _FILE_COMMANDS or not command.arguments:
                continue
            if command.name in {"usepackage", "RequirePackage"} and any(
                item.value == "subfiles" for item in _items(command.arguments[0], True)
            ):
                self.subfiles_package_loaded = True
            if command.name == "documentclass" and command.arguments[0].value == "subfiles":
                self._uncertain(
                    "source-path-option",
                    "Compiling a subfiles class as the main root requires inherited preamble "
                    "semantics outside this subset; select the parent document.",
                    name,
                    command.line,
                )
                continue
            if command.name in _IMPORT_COMMANDS:
                if len(command.arguments) < 2:
                    continue
                directory_arg, argument = map(_trim, command.arguments[:2])
                if not all(
                    _literal(arg.value) and source.text[arg.start : arg.end] == arg.value
                    for arg in (directory_arg, argument)
                ):
                    self._uncertain(
                        "source-dynamic-path",
                        "Import paths must be plain literal arguments.",
                        name,
                        command.line,
                    )
                    continue
                parent = context[0] if context and command.name.startswith("sub") else self.cwd
                directory = self._inside(directory_arg.value, name, command, base=parent)
                if directory is None:
                    continue
                target = self._resolve(argument.value, name, command, (), base=directory)
                if target is not None:
                    self._record_reference(name, argument, target, command)
                    self.search_arguments[(name, directory_arg.start, directory_arg.end)] = "./"
                    self._visit(target, search, (*stack, name), (directory, *context))
                continue
            index = 1 if command.name in _SECOND_FILE_ARGUMENT else 0
            if len(command.arguments) <= index:
                continue
            font_directory = None
            if command.name in _SUPPORTED_FONTS:
                directory = self._font_directory(name, source, command)
                if directory is None:
                    continue
                font_directory = self._inside(directory, name, command)
                if font_directory is None:
                    continue
            for argument in _items(command.arguments[index], command.name in _LIST_COMMANDS):
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
                if command.name in {
                    "input",
                    "include",
                    "documentclass",
                    "usepackage",
                    "RequirePackage",
                    "RequirePackageWithOptions",
                    "LoadClass",
                    "LoadClassWithOptions",
                }:
                    search = self._visit(target, search, (*stack, name), context)
        self._cache[key] = search
        return search

    def _source_checks(self) -> None:
        labels: dict[str, list[tuple[str, int]]] = defaultdict(list)
        references: list[tuple[str, str, int]] = []
        for name, source in self.sources.items():
            if self.main and name not in self.visited:
                continue
            # Template internals are not authored manuscript text.
            if Path(name).suffix.lower() not in _MANUSCRIPT_SUFFIXES:
                continue
            for match in re.finditer(r"\b(?:TODO|FIXME|XXX)\b|\?\?", source.masked):
                self.findings.append(
                    Finding(
                        "source-edit-marker",
                        f"Possible unfinished editing or placeholder text: {match.group()!r}.",
                        path=name,
                        line=source.text.count("\n", 0, match.start()) + 1,
                        evidence="heuristic",
                    )
                )
            for command in source.commands:
                if command.name in _EDIT_COMMANDS:
                    self.findings.append(
                        Finding(
                            "source-edit-command",
                            f"\\{command.name} may produce visible editing markup "
                            "or layout diagnostics.",
                            path=name,
                            line=command.line,
                            evidence="heuristic",
                        )
                    )
                if command.name == "label" and command.arguments:
                    value = command.arguments[0].value.strip()
                    if _literal(value):
                        labels[value].append((name, command.line))
                if command.name in _REFERENCE_COMMANDS and command.arguments:
                    for arg in _items(command.arguments[0], command.name in {"cref", "Cref"}):
                        if _literal(arg.value):
                            references.append((arg.value, name, command.line))
        for label, locations in labels.items():
            if len(locations) > 1:
                for name, line in locations:
                    self.findings.append(
                        Finding(
                            "source-duplicate-label",
                            f"Label {label!r} has multiple definitions in the source graph.",
                            path=name,
                            line=line,
                            evidence="heuristic",
                            details={
                                "label": label,
                                "locations": [
                                    {"path": path, "line": location_line}
                                    for path, location_line in locations
                                ],
                            },
                        )
                    )
        for label, name, line in references:
            if label not in labels:
                self.findings.append(
                    Finding(
                        "source-unresolved-reference",
                        f"Reference {label!r} has no definition in the inspected source graph.",
                        path=name,
                        line=line,
                        evidence="heuristic",
                        suggestion="Confirm with compilation; dynamic labels are not interpreted.",
                        details={"label": label},
                    )
                )


def analyze_sources(root: Path, main: str | None = None) -> SourceAnalysis:
    """Inspect a selected root and report literal dependencies without editing files."""
    inspection = _Inspection(root, main)
    # Every source file is read to discover roots, but a selected document is not
    # described by lexical uncertainties of files outside its own graph.
    outside = {
        id(item)
        for item in inspection.uncertainties
        if inspection.main is not None
        and item.path is not None
        and item.path not in inspection.visited
    }
    findings = [item for item in inspection.findings if id(item) not in outside]
    return SourceAnalysis(
        inspection.roots,
        inspection.main,
        findings,
        inspection.dependencies,
        complete=bool(inspection.main in inspection.visited)
        and all(id(item) in outside for item in inspection.uncertainties)
        and not any(finding.severity == "error" for finding in findings),
    )


def analyze_selected_sources(root: Path, main: str) -> SourceAnalysis:
    """Inspect only sources reached from an explicit main, leaving other sources unread.

    Dependencies and completeness describe the supported literal subset. Runtime
    input evidence and isolated rebuild verification remain necessary for packaging.
    """
    inspection = _Inspection(root, main, selected_only=True)
    return SourceAnalysis(
        inspection.roots,
        inspection.main,
        inspection.findings,
        inspection.dependencies,
        complete=bool(inspection.main in inspection.visited)
        and not inspection.uncertainties
        and not any(finding.severity == "error" for finding in inspection.findings),
    )


def _split_name(value: str) -> tuple[str, str]:
    suffix = PurePosixPath(value).suffix
    # Long apparent extensions are ordinary truncatable name text. Use the
    # same rule after sanitization, which can itself create a long suffix.
    if len(suffix.encode("utf-8")) > 32:
        return value, ""
    return (value[: -len(suffix)], suffix) if suffix else (value, "")


def _fit_name(stem: str, ending: str) -> str:
    available = 200 - len(ending.encode("utf-8"))
    if available < 0:
        raise PreparationError("A filename suffix exceeds the portable filename byte budget.")
    # A single bounded slice replaces trim loops. Ignoring the incomplete
    # final UTF-8 code point removes only a character split by the byte limit.
    return stem.encode("utf-8")[:available].decode("utf-8", errors="ignore") + ending


def _safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[^\w.\-]", "-", value, flags=re.UNICODE).strip(" .") or "file"
    if value.split(".")[0].upper() in _RESERVED_NAMES:
        value = "file-" + value
    return _fit_name(*_split_name(value))


def _with_counter(value: str, count: int) -> str:
    stem, suffix = _split_name(value)
    return _fit_name(stem, f"-{count}" + suffix)


def _mapping(
    files: dict[str, Path],
    main: str,
    findings: list[Finding],
    filename_overrides: tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    groups: dict[str, list[str]] = defaultdict(list)
    fixed = {
        name
        for name in files
        if name == main or Path(name).suffix.lower() in {".sty", ".cls", ".bst", ".bbl"}
    }
    overrides = dict(filename_overrides)
    if len(overrides) != len(filename_overrides):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "A source has more than one filename override.",
                "error",
                "failed",
            )
        )
    for name, output in overrides.items():
        message = None
        status = "failed"
        if name not in files:
            message = "A filename override names a file absent from the project."
        elif output != _safe_name(output) or "/" in output or "\\" in output:
            message = "An override destination must be one portable filename."
        elif PurePosixPath(output).suffix != PurePosixPath(name).suffix:
            message = "A filename override must preserve the file extension."
        elif name in fixed and output != PurePosixPath(name).name:
            message = (
                "Renaming a main, class, style or generated bibliography identity is unsupported."
            )
            status = "inconclusive"
        if message:
            findings.append(
                Finding(
                    "flatten.filename_overrides",
                    message,
                    "error",
                    status,
                    path=name,
                    details={"destination": output},
                )
            )
    for name in files:
        groups[_key(_safe_name(PurePosixPath(name).name))].append(name)
    candidates: list[tuple[int, str, str]] = []
    for name in files:
        basename = PurePosixPath(name).name
        safe = _safe_name(basename)
        if name in overrides:
            candidates.append((0, name, overrides[name]))
        elif name in fixed:
            if safe != basename:
                findings.append(
                    Finding(
                        "flatten-identity-name",
                        "This root, class, style, or generated bibliography filename "
                        "cannot be sanitized without changing its naming contract.",
                        severity="error",
                        path=name,
                        suggestion="Use preserve layout or rename the original project explicitly.",
                    )
                )
            candidates.append((0, name, basename))
        elif len(groups[_key(safe)]) == 1:
            candidates.append((1, name, safe))
        else:
            candidates.append((2, name, _safe_name("-".join(PurePosixPath(name).parts))))
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for priority, name, candidate in sorted(
        candidates, key=lambda item: (item[0], _key(item[1]), item[1])
    ):
        if priority == 0 and _key(candidate) in used:
            findings.append(
                Finding(
                    "flatten.filename_overrides"
                    if name in overrides or used[_key(candidate)] in overrides
                    else "flatten-identity-collision",
                    "Files with required class/style, bibliography, or root identities "
                    "collide after relocation.",
                    severity="error",
                    path=name,
                    details={"other": used[_key(candidate)], "filename": candidate},
                    suggestion="Use preserve layout or separate these files into packages.",
                )
            )
            continue
        original = candidate
        counter = 2
        while _key(candidate) in used:
            candidate = _with_counter(original, counter)
            counter += 1
        used[_key(candidate)] = name
        mapping[name] = candidate
    if overrides and not any(item.rule == "flatten.filename_overrides" for item in findings):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "Explicit filename overrides are portable and unique.",
                "info",
                "passed",
                details={"count": len(overrides)},
            )
        )
    return {name: mapping[name] for name in files if name in mapping}


def _replacement(target: str, command: str, mapping: dict[str, str]) -> str:
    output = mapping[target]
    if command in {"include", "bibliography", *_SYSTEM_COMMANDS}:
        suffix = _FILE_COMMANDS[command]
        if output.lower().endswith(suffix):
            output = output[: -len(suffix)]
    return output


def plan_flatten(
    root: Path,
    main: str,
    *,
    filename_overrides: tuple[tuple[str, str], ...] = (),
) -> FlattenPlan:
    """Return a reviewable flat map and rewritten bytes; never mutate the project.

    ``contents`` keys are original paths. Error findings make the plan unusable;
    partial mappings are diagnostic only. Caller-owned staging and rebuilt-PDF
    verification remain required, including for a plan without error findings.
    """
    inspection = _Inspection(root, main)
    findings = list(inspection.findings)
    plan = FlattenPlan(findings=findings)
    if inspection.main is None:
        return plan
    for item in inspection.uncertainties:
        findings.append(
            Finding(
                "flatten-unsupported-source",
                item.message,
                severity="error",
                status="inconclusive",
                path=item.path,
                line=item.line,
                suggestion=item.suggestion
                or "Use preserve layout until this source construct is resolved.",
                details={"source_rule": item.rule, **item.details},
            )
        )
    other_roots = [
        name
        for name in inspection.roots
        if name != inspection.main and name not in inspection.subfile_targets
    ]
    if other_roots:
        findings.append(
            Finding(
                "flatten-multiple-roots",
                "The initial flat planner supports one document root per selected package.",
                severity="error",
                status="inconclusive",
                details={"other_roots": other_roots},
                suggestion="Select only this document and its dependencies.",
            )
        )
    for name, source in inspection.sources.items():
        if name not in inspection.visited and any(
            command.name in _FILE_COMMANDS or command.name == "graphicspath"
            for command in source.commands
        ):
            findings.append(
                Finding(
                    "flatten-unresolved-context",
                    "An unvisited source has file references "
                    "with no established compilation context.",
                    severity="error",
                    status="inconclusive",
                    path=name,
                    suggestion="Declare a literal input, exclude the file, or preserve layout.",
                )
            )
    plan.mapping = _mapping(inspection.files, inspection.main, findings, filename_overrides)
    for name, command, value, basename in inspection.system_references:
        shadows = [path for path, output in plan.mapping.items() if _key(output) == _key(basename)]
        if shadows:
            findings.append(
                Finding(
                    "flatten-system-shadowing",
                    "Relocation would make a local file shadow "
                    "an unresolved toolchain class or style reference.",
                    severity="error",
                    path=name,
                    line=command.line,
                    details={"reference": value, "files": shadows},
                    suggestion="Use preserve layout or resolve the intended dependency explicitly.",
                )
            )
    for name, output in plan.mapping.items():
        if name.endswith((".bb", ".xbb")):
            stem = name.rsplit(".", 1)[0]
            for extension in _GRAPHICS_SUFFIXES:
                companion = stem + extension
                if (
                    companion in plan.mapping
                    and PurePosixPath(plan.mapping[companion]).stem != PurePosixPath(output).stem
                ):
                    findings.append(
                        Finding(
                            "flatten-companion-identity",
                            "A graphics sidecar and its image would no longer share a basename.",
                            severity="error",
                            path=name,
                            details={"companion": companion},
                            suggestion="Preserve layout or resolve companion filenames together.",
                        )
                    )
    edits: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
    for (name, start, end), (target, command) in inspection.references.items():
        if target in plan.mapping:
            edits[name][(start, end)] = _replacement(target, command, plan.mapping)
    for (name, start, end), replacement in inspection.search_arguments.items():
        edits[name][(start, end)] = replacement
    for name, replacements in edits.items():
        source = inspection.sources[name]
        rewritten = source.text
        for (start, end), replacement in sorted(replacements.items(), reverse=True):
            rewritten = rewritten[:start] + replacement + rewritten[end:]
        if rewritten != source.text:
            plan.contents[name] = (b"\xef\xbb\xbf" if source.bom else b"") + rewritten.encode(
                "utf-8"
            )
            diff = "".join(
                difflib.unified_diff(
                    source.text.splitlines(keepends=True),
                    rewritten.splitlines(keepends=True),
                    fromfile=name,
                    tofile=plan.mapping.get(name, name),
                )
            )
            plan.changes.append(
                Change(
                    name, "rewrite", "Update literal file references for flat layout.", diff=diff
                )
            )
    for name, output in plan.mapping.items():
        if name != output:
            plan.changes.append(
                Change(
                    name,
                    "move",
                    "Move selected file into the flat submission directory.",
                    destination=output,
                )
            )
    return plan
