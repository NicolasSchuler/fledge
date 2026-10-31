"""The latexmk command line of one build and the fixed Perl configuration it carries.

latexmk runs with ``-norc`` and a static, application-owned configuration passed on
the command line, so no project, user or system rc file, rule or environment variable
can change what it does. The configuration also decides which bibliography backend
may run: an unselected backend, or Biber while its preparation is deferred, is
replaced by an internal command that fails without launching anything. Running the
command is ``runtime``'s work and reading its evidence is the other build modules'.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from .build_workspace import BuildLayout
from .setting_choices import BibliographyBackend, Engine

ENGINE_MODES: dict[Engine, str] = {
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
    "lualatex": "-lualatex",
}
# Passes latexmk may make to settle cross-references, citations and the table of
# contents; a document that still changes after this many is reported, not looped on.
MAX_LATEXMK_PASSES = 5


@dataclass(frozen=True, slots=True)
class BiberPlan:
    """Whether this build defers Biber, and the preparation note it reports.

    Attributes:
        deferred: Whether Biber is replaced by a failing internal command for this build.
        note: The preparation note the build reports, or ``None``.
    """

    deferred: bool
    note: str | None


def latexmk_command(engine: Engine, main: str, layout: BuildLayout, rules: str) -> list[str]:
    """Build the argv that compiles ``main`` with ``engine`` into the output folder.

    Args:
        engine: The TeX engine, which selects the latexmk mode.
        main: The main file; only its file name is passed, relative to the working folder.
        layout: The build's working and output folders.
        rules: The configuration from ``controlled_rules``.

    Returns:
        The latexmk argv, which writes everything into the output folder.
    """
    return [
        "latexmk",
        "-norc",
        "-e",
        rules,
        "-use-make-",
        "-view=none",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-no-shell-escape",
        "-recorder",
        ENGINE_MODES[engine],
        f"-outdir={os.path.relpath(layout.output, layout.cwd)}",
        "./" + PurePosixPath(main).name,
    ]


def controlled_rules(backend: BibliographyBackend, biber: BiberPlan, biber_executable: str) -> str:
    """Write the Perl configuration for ``-e``: fixed settings and the bibliography gates.

    Args:
        backend: The selected bibliography backend; any choice but ``auto`` blocks the
            other backend.
        biber: Whether Biber is deferred for this build.
        biber_executable: The application-generated path of the prepared Biber, used when
            Biber may run.

    Returns:
        The configuration as one line of Perl.
    """
    # Static trusted configuration overrides no imported rc, rules, or environment.
    rules = (
        f"$max_repeat={MAX_LATEXMK_PASSES}; $bibtex_use=1; "
        "$use_make_for_missing_files=0; @cus_dep_list=();"
    )
    if backend != "auto":
        # Latexmk's documented internal-command adapter returns failure without
        # launching the unselected backend. All code is application-owned; no
        # filename or user-provided string is interpolated into Perl or a shell.
        disabled = "$biber" if backend == "bibtex" else "$bibtex"
        rules += (
            " sub fledge_backend_blocked { "
            "warn 'Bibliography backend is disabled by the selected strategy'; return 1; } "
            f"{disabled}='internal fledge_backend_blocked';"
        )
    if biber.deferred:
        # Fails like a blocked backend; the fresh .bcf left behind makes
        # build_project discard this build and rebuild with Biber prepared.
        rules += (
            " sub fledge_biber_deferred { "
            "warn 'Biber is deferred until a build shows that it is needed'; return 1; } "
            "$biber='internal fledge_biber_deferred';"
        )
    elif backend != "bibtex":
        # Only an application-generated path reaches this Perl literal, and
        # prepare_biber rejects any path that is not inert in Perl and the shell.
        rules += f" $biber='{biber_executable} --noconf %O %B';"
    return rules
