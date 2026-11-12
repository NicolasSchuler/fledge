r"""Which TeX engine builds a document, and why; and what a project's latexmkrc would change.

The engine comes from the first of these that names one: the ``engine`` setting (from
``--engine`` or the settings file), a ``% !TEX program = xelatex`` comment among the first
lines of the main file, as TeXShop, TeXstudio, TeXworks and VS Code write it, and otherwise
pdfLaTeX. A project's ``latexmkrc`` is never executed, because latexmk runs with ``-norc``;
it is read only as text, to tell the researcher which of its choices Fledge ignores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .file_safety import read_file_prefix
from .findings import Finding
from .setting_choices import ENGINES, Engine, EngineSetting

EngineSource = Literal["setting", "magic comment", "default"]
ENGINE_LABELS: dict[Engine, str] = {
    "pdflatex": "pdfLaTeX",
    "xelatex": "XeLaTeX",
    "lualatex": "LuaLaTeX",
}
DEFAULT_ENGINE: Engine = "pdflatex"
ENGINE_RULE = "build.engine"
LATEXMK_CONFIGURATION_RULE = "build.latexmk_configuration"
# TeXShop reads magic comments from the first twenty lines; the other editors agree.
_MAGIC_COMMENT_LINES = 20
_MAX_MAGIC_COMMENT_BYTES = 16_384
_MAGIC_COMMENT = re.compile(r"^\s*%\s*!\s*tex\s+(?:ts-)?program\s*=\s*(\S+)", re.IGNORECASE)
# TeXShop names its latexmk-based engine scripts after the engine with "mk" appended.
_ENGINE_SCRIPT_SUFFIX = "mk"
# The names latexmk reads its configuration from in the directory it runs in.
_LATEXMK_CONFIGURATION_NAMES = ("latexmkrc", ".latexmkrc")
_MAX_LATEXMK_CONFIGURATION_BYTES = 65_536
_PDF_MODE = re.compile(r"\$pdf_mode\s*=\s*(\d+)")
# latexmk's $pdf_mode values: 1 pdfLaTeX, 4 LuaLaTeX, 5 XeLaTeX; 2 and 3 make a PDF through
# PostScript or DVI, which Fledge does not build.
_PDF_MODE_ENGINES: dict[str, Engine] = {"1": "pdflatex", "4": "lualatex", "5": "xelatex"}
_CUSTOM_RULE_MARKERS = ("add_cus_dep", "@cus_dep_list")
_SHELL_ESCAPE_MARKERS = ("-shell-escape", "--shell-escape", "-enable-write18")


@dataclass(frozen=True, slots=True)
class EngineComment:
    """A ``% !TEX program`` comment in the main file.

    Attributes:
        program: The program the comment names, as written.
        line: The line of the comment, from 1.
        engine: The engine the program names, or ``None`` when Fledge cannot run it.
    """

    program: str
    line: int
    engine: Engine | None


@dataclass(frozen=True, slots=True)
class EngineChoice:
    """The engine of one build and what chose it.

    Attributes:
        engine: The engine that builds the document.
        source: What chose it: the setting, the magic comment, or the default.
        comment: The main file's ``% !TEX program`` comment, if it has one.
    """

    engine: Engine
    source: EngineSource
    comment: EngineComment | None


@dataclass(frozen=True, slots=True)
class LatexmkConfiguration:
    """What a project's latexmk configuration would change, read as text.

    Attributes:
        name: The configuration file, relative to the project.
        pdf_mode: The ``$pdf_mode`` value it sets, if any.
        custom_rules: Whether it defines custom dependency rules.
        shell_escape: Whether it enables shell escape.
    """

    name: str
    pdf_mode: str | None
    custom_rules: bool
    shell_escape: bool

    @property
    def engine(self) -> Engine | None:
        """The engine its ``$pdf_mode`` selects, or ``None``."""
        return _PDF_MODE_ENGINES.get(self.pdf_mode or "")


def _program_engine(program: str) -> Engine | None:
    """Return the engine a magic comment's program names, or ``None`` for another program."""
    name = program.casefold()
    for engine in ENGINES:
        if name in {engine, engine + _ENGINE_SCRIPT_SUFFIX}:
            return engine
    return None


def read_engine_comment(main_file: Path) -> EngineComment | None:
    """Read the first ``% !TEX program`` comment among the first lines of the main file.

    Args:
        main_file: The main file.

    Returns:
        The comment, or ``None`` when the file has none or cannot be read.
    """
    try:
        data = read_file_prefix(main_file, _MAX_MAGIC_COMMENT_BYTES)
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    for number, line in enumerate(lines[:_MAGIC_COMMENT_LINES], 1):
        if match := _MAGIC_COMMENT.match(line):
            program = match[1]
            return EngineComment(program, number, _program_engine(program))
    return None


def choose_engine(setting: EngineSetting, comment: EngineComment | None) -> EngineChoice:
    """Choose the engine: the setting, then the magic comment, then pdfLaTeX.

    Args:
        setting: The ``engine`` setting; ``auto`` leaves the choice to the main file.
        comment: The main file's ``% !TEX program`` comment, if any.

    Returns:
        The engine and what chose it.
    """
    if setting != "auto":
        return EngineChoice(setting, "setting", comment)
    if comment is not None and comment.engine is not None:
        return EngineChoice(comment.engine, "magic comment", comment)
    return EngineChoice(DEFAULT_ENGINE, "default", comment)


def engine_finding(choice: EngineChoice, main: str) -> Finding:
    """Say which engine builds the document and what chose it.

    A magic comment that names a program Fledge cannot run, and that no setting overrides,
    makes the finding a warning: the document was probably written for that program.

    Args:
        choice: The engine and what chose it.
        main: The main file, the finding's path.

    Returns:
        An informational finding that passed, or a warning about an unusable comment.
    """
    label = ENGINE_LABELS[choice.engine]
    comment = choice.comment
    quoted = (
        f"the `% !TEX program = {comment.program}` comment on line {comment.line} of {main}"
        if comment is not None
        else None
    )
    details: dict[str, object] = {
        "engine": choice.engine,
        "source": choice.source,
        "comment": None
        if comment is None
        else {"program": comment.program, "line": comment.line, "engine": comment.engine},
    }
    if choice.source == "setting":
        message = (
            f"{label} builds the document, as the engine setting asks "
            "(--engine or engine in the settings file)."
        )
        if comment is not None and comment.engine != choice.engine:
            message += f" The setting takes precedence over {quoted}."
    elif choice.source == "magic comment":
        message = f"{label} builds the document, as {quoted} asks."
    elif comment is not None:
        return Finding(
            ENGINE_RULE,
            f"The `% !TEX program = {comment.program}` comment on line {comment.line} of "
            f"{main} names a program that Fledge cannot run, so {label} builds the document.",
            severity="warning",
            path=main,
            line=comment.line,
            suggestion=(
                "Fledge builds with pdflatex, xelatex or lualatex. Change the comment to the "
                "one the document is written for, or choose it with --engine."
            ),
            details=details,
        )
    else:
        message = (
            f"{label} builds the document: neither the engine setting nor a "
            f"`% !TEX program` comment in {main} chooses another engine."
        )
    return Finding(
        ENGINE_RULE,
        message,
        severity="info",
        status="passed",
        path=main,
        line=comment.line if choice.source == "magic comment" and comment is not None else None,
        details=details,
    )


def find_latexmk_configurations(source: Path, main: str) -> list[LatexmkConfiguration]:
    """Read the latexmk configuration files in the main file's folder and the project root.

    The files are read as text with a bounded read; nothing in them is executed.

    Args:
        source: The project folder.
        main: The main file, relative to ``source``.

    Returns:
        What each configuration file would change, in folder order.
    """
    folders = dict.fromkeys((PurePosixPath(main).parent, PurePosixPath(".")))
    configurations = []
    for folder in folders:
        for name in _LATEXMK_CONFIGURATION_NAMES:
            relative = (folder / name).as_posix()
            try:
                data = read_file_prefix(source / relative, _MAX_LATEXMK_CONFIGURATION_BYTES)
            except OSError:
                continue
            text = data.decode("utf-8", errors="replace")
            code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            modes = _PDF_MODE.findall(code)
            configurations.append(
                LatexmkConfiguration(
                    relative,
                    modes[-1] if modes else None,
                    any(marker in code for marker in _CUSTOM_RULE_MARKERS),
                    any(marker in code for marker in _SHELL_ESCAPE_MARKERS),
                )
            )
    return configurations


def latexmk_configuration_finding(
    configuration: LatexmkConfiguration, choice: EngineChoice, main: str
) -> Finding:
    """Say that a project's latexmk configuration is not executed, and what it would change.

    The finding is a warning when the configuration selects another engine than the one
    that builds the document, and informational otherwise.

    Args:
        configuration: What the configuration file would change.
        choice: The engine of the build and what chose it.
        main: The main file, which a suggestion names.

    Returns:
        The finding.
    """
    message = (
        f"{configuration.name} is not executed: Fledge runs latexmk with -norc and its own "
        "fixed configuration, so the settings in this file do not apply."
    )
    suggestion = None
    selected = configuration.engine
    if selected is not None:
        message += f" It selects {ENGINE_LABELS[selected]} ($pdf_mode = {configuration.pdf_mode})."
        if selected != choice.engine:
            suggestion = (
                f"Build with {ENGINE_LABELS[selected]}: pass --engine {selected}, set "
                f'engine = "{selected}" in fledge.toml, or add `% !TEX program = {selected}` '
                f"to the first lines of {main}."
            )
    elif configuration.pdf_mode is not None:
        message += (
            f" Its $pdf_mode = {configuration.pdf_mode} makes the PDF through DVI or "
            "PostScript, which Fledge does not build."
        )
    if configuration.custom_rules:
        message += (
            " Its custom rules do not run either; Fledge's own rules run makeindex, "
            "makeglossaries and the nomencl index."
        )
    if configuration.shell_escape:
        message += " It enables shell escape, which Fledge's builds never allow."
    if suggestion is None:
        suggestion = (
            "Check that the build result matches what the file would do; if the document "
            "needs another engine, choose it with --engine or a % !TEX program comment."
        )
    return Finding(
        LATEXMK_CONFIGURATION_RULE,
        message,
        severity="warning" if selected is not None and selected != choice.engine else "info",
        path=configuration.name,
        suggestion=suggestion,
        details={
            "pdf_mode": configuration.pdf_mode,
            "custom_rules": configuration.custom_rules,
            "shell_escape": configuration.shell_escape,
        },
    )
