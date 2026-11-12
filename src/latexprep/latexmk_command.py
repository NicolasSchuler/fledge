"""The latexmk command line of one build and the fixed Perl configuration it carries.

latexmk runs with ``-norc`` and a static, application-owned configuration passed on
the command line, so no project, user or system rc file, rule or environment variable
can change what it does. The configuration also decides which bibliography backend
may run: an unselected backend, or Biber while its preparation is deferred, is
replaced by an internal command that fails without launching anything. It adds the
custom rules that build a glossary, a list of acronyms and a nomenclature with makeindex,
which latexmk runs by itself only for an index. Running the command is ``runtime``'s work
and reading its evidence is the other build modules'.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from .build_workspace import BuildLayout
from .generated_files import BIBLIOGRAPHY_SUFFIX, INDEX_SUFFIXES
from .setting_choices import BibliographyBackend, Engine

ENGINE_MODES: dict[Engine, str] = {
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
    "lualatex": "-lualatex",
}
# Passes latexmk may make to settle cross-references, citations and the table of
# contents; a document that still changes after this many is reported, not looped on.
MAX_LATEXMK_PASSES = 5
# Custom rules for the glossaries and nomencl packages, which leave makeindex input that
# latexmk does not process by itself. Each rule makes exactly its own output, so that the
# rule database shows which rule wrote which file: the glossary (.glo to .gls) and the list
# of acronyms (.acn to .acr) with the style file and order that the glossaries package
# recorded in the .aux file, and the nomenclature (.nlo to .nls) with nomencl's style.
# A recorded style ending in .xdy means that the glossaries are written for xindy, which
# the isolated build does not run and whose input makeindex cannot read. The glossary and
# acronym rules then leave their list empty instead of running makeindex, because latexmk
# fails a rule that makes no output; the build evidence reports such a list as not built
# (``build_dependencies``). makeindex runs in the output folder, because the paranoid
# openout_any setting refuses a path into a parent directory. Only latexmk's base name
# reaches the commands, as a list argument that no shell interprets.
GLOSSARY_RULES = (
    'add_cus_dep("glo", "gls", 0, "fledge_glossary"); '
    'add_cus_dep("acn", "acr", 0, "fledge_acronyms"); '
    'add_cus_dep("nlo", "nls", 0, "fledge_nomenclature"); '
    "sub fledge_makeindex { my ($base, @arguments) = @_; "
    "my ($name, $folder) = File::Basename::fileparse($base); my $here = Cwd::getcwd(); "
    'chdir($folder) or return 1; my $status = system("makeindex", "-q", @arguments); '
    "chdir($here); return $status; } "
    "sub fledge_glossary_style { my ($base, $name) = @_; "
    'my ($style, $order) = ("$name.ist", ""); '
    'if (open(my $aux, "<", "$base.aux")) { while (my $line = <$aux>) { '
    "$style = $1 if $line =~ /\\\\\\@istfilename\\{([^{}]+)\\}/; "
    '$order = "-l" if $line =~ /\\\\\\@glsorder\\{letter\\}/; } close($aux); } '
    "return ($style, $order); } "
    "sub fledge_unbuilt_list { open(my $list, '>', $_[0]) or return 1; close($list); "
    'warn "Fledge: left $_[0] empty: its glossary is written for xindy\\n"; return 0; } '
    "sub fledge_glossary_index { my ($base, $log, $output, $input) = @_; "
    "my ($name) = File::Basename::fileparse($base); "
    "my ($style, $order) = fledge_glossary_style($base, $name); "
    'return fledge_unbuilt_list("$base.$output") if $style =~ /\\.xdy\\z/; '
    'return fledge_makeindex($base, "-s", $style, grep({ $_ ne "" } $order), '
    '"-t", "$name.$log", "-o", "$name.$output", "$name.$input"); } '
    'sub fledge_glossary { return fledge_glossary_index($_[0], "glg", "gls", "glo"); } '
    'sub fledge_acronyms { return fledge_glossary_index($_[0], "alg", "acr", "acn"); } '
    "sub fledge_nomenclature { my ($name) = File::Basename::fileparse($_[0]); "
    'return fledge_makeindex($_[0], "-s", "nomencl.ist", "-t", "$name.nlg", '
    '"-o", "$name.nls", "$name.nlo"); }'
)


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


def controlled_rules(
    backend: BibliographyBackend,
    biber: BiberPlan,
    biber_executable: str,
    *,
    kept_generated: frozenset[str] = frozenset(),
) -> str:
    """Write the Perl configuration for ``-e``: fixed settings and the bibliography gates.

    Args:
        backend: The selected bibliography backend; any choice but ``auto`` blocks the
            other backend.
        biber: Whether Biber is deferred for this build.
        biber_executable: The application-generated path of the prepared Biber, used when
            Biber may run.
        kept_generated: The suffixes of shipped generated files (``generated_files``)
            that the build must read as they are: with ``.bbl``, neither BibTeX nor Biber
            runs, and with an index or glossary suffix, makeindex is replaced by an
            internal command that succeeds without writing anything and the glossary
            and nomenclature rules are left out.

    Returns:
        The configuration as one line of Perl.
    """
    # Static trusted configuration overrides no imported rc, rules, or environment.
    bibtex_use = 0 if BIBLIOGRAPHY_SUFFIX in kept_generated else 1
    rules = (
        f"$max_repeat={MAX_LATEXMK_PASSES}; $bibtex_use={bibtex_use}; "
        "$use_make_for_missing_files=0; @cus_dep_list=();"
    )
    if kept_generated & frozenset(INDEX_SUFFIXES):
        # The build workspace seeds the shipped files into the aux folder, so latexmk finds
        # the destination it expects and TeX reads the shipped file. The glossary and
        # nomenclature rules are left out: they would run makeindex themselves.
        rules += (
            " sub fledge_generated_file_kept { return 0; } "
            "$makeindex='internal fledge_generated_file_kept';"
        )
    else:
        rules += f" {GLOSSARY_RULES}"
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
