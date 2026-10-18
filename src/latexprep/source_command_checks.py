"""Judging the commands of one parsed source: which ones the literal subset cannot follow.

Every check looks at one command (or at the source as a whole) and records an
uncertainty in ``GraphFindings`` when the command depends on something the scan does
not establish: a path hidden in an argument, a conditional around a file reference,
a command that reads files at run time, a file name that is not plain text. The
checks are independent and run in a fixed order. They read a source but never follow
a reference (``source_graph``) or resolve a path (``source_resolution``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .literal_text import HEADING_DEPTHS, KEY_ARGUMENT_COMMANDS, REFERENCE_COMMANDS, TEXT_WRAPPERS
from .source_file_commands import (
    COMMA_LIST_FILE_COMMANDS,
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    FILE_COMMANDS,
    SUPPORTED_FONT_COMMANDS,
    TOOLCHAIN_FILE_COMMANDS,
    file_argument_index,
    is_literal_text,
)
from .source_findings import NEEDED_FOR_RESTRUCTURING, GraphFindings
from .source_loading import ParsedSource
from .tex_lexer import Command, conditional_spans, in_conditional, split_argument_items

_UNSUPPORTED_COMMANDS = frozenset(
    {
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
        # Font commands that name no file but a family, which lookup would have to guess.
        "newfontfamily",
        "newfontface",
    }
)
# Commands whose arguments hold prose, keys or declarations, never a file the document
# reads: the shared prose tables plus the groups below.
_TEXT_COMMANDS = frozenset(
    {
        *TEXT_WRAPPERS,
        *KEY_ARGUMENT_COMMANDS,
        *REFERENCE_COMMANDS,
        *HEADING_DEPTHS,
        # Links: the address is an argument, but not a project file.
        "url",
        "nolinkurl",
        "path",
        "href",
        "hyperref",
        "hypersetup",
        # Boxes, captions and front matter.
        "text",
        "fbox",
        "caption",
        "captionof",
        "title",
        "author",
        "thanks",
        "footnote",
        # Package, class and file declarations, and their messages.
        "ProvidesPackage",
        "ProvidesClass",
        "ProvidesFile",
        "typeout",
        "PackageWarning",
        "PackageInfo",
        "ClassWarning",
        "ClassInfo",
        # biblatex field access.
        "bibfield",
        "field",
    }
)
# Arguments are control-sequence names, never files: biblatex's generated .bbl
# guards itself with \@ifundefined{ver@biblatex.sty}.
_MACRO_NAME_COMMANDS = frozenset({"@ifundefined", "@namedef", "@nameuse"})
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
_DECLARED_IDENTITY_COMMANDS = frozenset({"ProvidesPackage", "ProvidesClass", "ProvidesFile"})


@dataclass(frozen=True, slots=True)
class SourceChecks:
    """One parsed source being checked: where it is, what it holds, and where to report.

    ``known_paths`` is the project's index of every name suffix and directory prefix
    (``project_files.known_path_index``); the lookup context of a reference is not
    established while a file is checked, so it only rules out names no project file
    could match.
    """

    path: str
    source: ParsedSource
    known_paths: set[str]
    findings: GraphFindings

    def names_project_file(self, command: Command) -> bool:
        """Whether a class/package reference can name a file inside the project."""
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


def _is_file_command(command: Command) -> bool:
    return command.name in FILE_COMMANDS or command.name == "graphicspath"


def _check_conditional_dependencies(checks: SourceChecks) -> None:
    """File references that sit inside a conditional, whose branch is not established."""
    spans = conditional_spans(checks.source.commands)
    for command in checks.source.commands:
        if not _is_file_command(command) or not in_conditional(spans, command.start):
            continue
        # Toolchain classes and packages are covered by recorded build inputs.
        if command.name in TOOLCHAIN_FILE_COMMANDS and not checks.names_project_file(command):
            continue
        checks.findings.uncertain(
            "source.conditional_dependency",
            "Conditional TeX control flow encloses a file reference; "
            "the static scan does not establish which branch executes.",
            checks.path,
            command.line,
            suggestion="Make the dependency unconditional so it can be followed. "
            'With layout = "preserve" the files the build read are shipped anyway.',
        )


def _check_unclosed_literals(checks: SourceChecks) -> None:
    """Literal regions without a closing delimiter, after which commands are unreliable."""
    for offset in checks.source.unclosed_literals:
        checks.findings.uncertain(
            "source.literal_unclosed",
            "A literal region has no closing delimiter; "
            "later commands cannot be inspected reliably.",
            checks.path,
            checks.source.line_at(offset),
        )


def _check_subfiles_v1(checks: SourceChecks, command: Command) -> None:
    """The subfiles package in its ``v1`` mode, which runs preambles in a way not followed."""
    if (
        command.name in {"usepackage", "RequirePackage"}
        and command.arguments
        and "subfiles" in {item.value for item in split_argument_items(command.arguments[0])}
        and any("v1" in option.value.split(",") for option in command.options)
    ):
        checks.findings.uncertain(
            "source.subfile_structure",
            "The subfiles v1 preamble execution mode is unsupported.",
            checks.path,
            command.line,
        )


def _check_provides_identity(checks: SourceChecks, command: Command) -> None:
    """A declared package, class or file name that contains a directory."""
    if (
        command.name in _DECLARED_IDENTITY_COMMANDS
        and command.arguments
        and "/" in command.arguments[0].value
    ):
        checks.findings.uncertain(
            "source.path_identity",
            "The declared package, class, or file identity contains a directory path.",
            checks.path,
            command.line,
            "Flat layout would move this file and change the declared identity; "
            'use layout = "preserve" to keep it.',
        )


def _check_unsupported_or_custom_path(checks: SourceChecks, command: Command) -> None:
    """A command that reads files at run time, or an unknown one handed what looks like a path."""
    if command.name in _UNSUPPORTED_COMMANDS:
        checks.findings.uncertain(
            "source.unsupported_command",
            f"\\{command.name} may depend on paths or runtime lookup "
            "outside the supported literal subset.",
            checks.path,
            command.line,
            "Replace this command with a literal file reference the scan can follow. "
            + NEEDED_FOR_RESTRUCTURING,
            {"command": command.name},
        )
        return
    if (
        command.name in FILE_COMMANDS
        or command.name in _TEXT_COMMANDS
        or command.name in _MACRO_NAME_COMMANDS
        or command.name == "graphicspath"
    ):
        return
    for argument in (*command.options, *command.arguments):
        if is_literal_text(argument.value) and (
            _PATH_LIKE.search(argument.value)
            or ("/" in argument.value and argument.value.strip() in checks.known_paths)
        ):
            checks.findings.uncertain(
                "source.custom_path",
                f"\\{command.name} has a possible file argument "
                "whose lookup semantics are unknown.",
                checks.path,
                command.line,
                "Name the file with a supported command such as \\input or "
                "\\includegraphics. " + NEEDED_FOR_RESTRUCTURING,
                {"command": command.name, "argument": argument.value},
            )
            return


def _check_option_paths(checks: SourceChecks, command: Command) -> None:
    """Options of a file command that look like paths, whose lookup rules are not followed."""
    if not _is_file_command(command) or command.name in SUPPORTED_FONT_COMMANDS:
        return
    subfiles_parent = (
        command.name == "documentclass"
        and command.arguments
        and command.arguments[0].value.strip() == "subfiles"
    )
    if subfiles_parent:
        return
    for option in command.options:
        if is_literal_text(option.value) and _PATH_LIKE.search(option.value):
            checks.findings.uncertain(
                "source.path_option",
                f"\\{command.name} has a file-like option with unsupported lookup semantics.",
                checks.path,
                command.line,
                "Simplify the option so it no longer looks like a file path. "
                + NEEDED_FOR_RESTRUCTURING,
            )


def _check_nested(checks: SourceChecks, command: Command) -> None:
    """A file command inside a group or macro argument, whose execution context is unknown."""
    if _is_file_command(command) and command.depth and command.name != "graphicspath":
        checks.findings.uncertain(
            "source.nested_file_command",
            f"\\{command.name} occurs inside a group or macro argument; "
            "its execution context is not established.",
            checks.path,
            command.line,
            "Use a direct literal reference outside groups and macro arguments. "
            + NEEDED_FOR_RESTRUCTURING,
        )


def _check_filename_argument(checks: SourceChecks, command: Command) -> None:
    """A file command without a filename argument, or one whose argument hides text."""
    if not _is_file_command(command):
        return
    index = file_argument_index(command.name)
    if len(command.arguments) <= index:
        checks.findings.uncertain(
            "source.dynamic_path",
            f"\\{command.name} does not have a supported literal filename argument.",
            checks.path,
            command.line,
        )
        return
    argument = command.arguments[index]
    source = checks.source
    if source.text[argument.start : argument.end] != source.masked[argument.start : argument.end]:
        checks.findings.uncertain(
            "source.path_literal_context",
            f"\\{command.name} contains comments or literal commands inside its path argument.",
            checks.path,
            command.line,
            "Use a plain filename argument before flattening.",
        )


_COMMAND_CHECKS: tuple[Callable[[SourceChecks, Command], None], ...] = (
    _check_subfiles_v1,
    _check_provides_identity,
    _check_unsupported_or_custom_path,
    _check_option_paths,
    _check_nested,
    _check_filename_argument,
)


def check_source(checks: SourceChecks) -> None:
    """Run every check over a freshly parsed source: conditionals, literals, then commands."""
    _check_conditional_dependencies(checks)
    _check_unclosed_literals(checks)
    for command in checks.source.commands:
        for check in _COMMAND_CHECKS:
            check(checks, command)
