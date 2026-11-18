"""Which LaTeX commands read a project file, and how each names its file.

The tables here are the source graph's vocabulary: the commands whose argument is a
file name, the suffixes TeX tries for each, the commands that take a directory first,
the ones TeX may satisfy from its own installation, and the ones the graph follows
into the file they name. Nothing here reads a file or looks at a project; the
modules that resolve and walk references (``source_resolution``, ``source_graph``)
and the ones that judge commands (``source_command_checks``) share these names.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Graphics lookup order of the default pdfTeX graphics rules.
GRAPHICS_SUFFIXES = (
    ".pdf",
    ".png",
    ".jpg",
    ".mps",
    ".jpeg",
    ".jbig2",
    ".jb2",
    ".PDF",
    ".PNG",
    ".JPG",
    ".JPEG",
    ".JBIG2",
    ".JB2",
    ".eps",
    ".ps",
)
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


def candidate_paths(command: str, paths: list[str], suffixes: tuple[str, ...]) -> list[str]:
    r"""Return the lookup candidates for ``paths`` in the order the command tries them.

    LaTeX's ``\include`` and BibTeX's database and style readers complete the name
    with their own suffix, even after another dot. With graphicx, each candidate
    extension is tried across the entire search path before the next extension.
    An explicit supported graphics extension is tried first.
    """
    if command in {"include", "bibliography", "bibliographystyle"}:
        (suffix,) = suffixes
        return [path if path.endswith(suffix) else path + suffix for path in paths]
    if command == "includegraphics" and paths:
        extended = [path + suffix for suffix in suffixes for path in paths]
        if PurePosixPath(paths[0]).suffix.lower() in GRAPHICS_SUFFIXES:
            return [*paths, *extended]
        return [*extended, *paths]
    candidates: list[str] = []
    for path in paths:
        if not PurePosixPath(path).suffix:
            candidates.extend(path + suffix for suffix in suffixes)
        candidates.append(path)
    return candidates


# Commands taking a directory and then a file name.
IMPORT_COMMANDS = frozenset({"import", "subimport", "inputfrom", "subinputfrom"})
SECOND_ARGUMENT_FILE_COMMANDS = frozenset({*IMPORT_COMMANDS, "DTLloaddb", "inputminted"})


def file_argument_index(command_name: str) -> int:
    """Return the index of the argument naming the file: after the directory, if there is one."""
    return 1 if command_name in SECOND_ARGUMENT_FILE_COMMANDS else 0


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
# Commands whose bare file name TeX or BibTeX may also find in the installation, as
# ``\input{glyphtounicode}``, ``\bibliography{IEEEabrv}`` and
# ``\includegraphics{example-image}`` do; ``\include`` always reads a chapter of the project.
INSTALLATION_INPUT_COMMANDS = frozenset({"input", "bibliography", "includegraphics"})
# Commands whose resolved target is read as TeX source in the current path context;
# a subfile is traversed with its own directory as context instead.
TRAVERSED_COMMANDS = frozenset(
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
# Font commands whose font file is followed; the other font commands are unsupported.
SUPPORTED_FONT_COMMANDS = frozenset({"setmainfont", "setsansfont", "setmonofont", "fontspec"})


def is_literal_text(value: str) -> bool:
    """Return whether ``value`` is nonempty and free of TeX syntax that could change its meaning."""
    return bool(value) and not any(char in value for char in "\\{}#$^~&%\n\r\x00")


# Commands that declare a bibliography resource: a problem with one can hide bibliography
# entries, but not manuscript text.
BIBLIOGRAPHY_RESOURCE_COMMANDS = frozenset({"bibliography", "addbibresource"})
# Commands whose file holds nothing that TeX runs as manuscript source: figures and PDF
# pages, plot coordinates, plain verbatim listings, fonts, bibliography styles and the
# graphics rules. What the scan cannot establish about one of them, such as a figure in
# a macro argument, cannot hide manuscript text, a label or a citation. Files whose
# content can run TeX are left out: listings and minted with escapeinside, fancyvrb with
# commandchars, CSV and table cells that are typeset, an SVG's generated .pdf_tex, and
# another document's labels (\externaldocument).
OPAQUE_FILE_COMMANDS = frozenset(
    {
        "includegraphics",
        "includepdf",
        "includepdfmerge",
        "DeclareGraphicsExtensions",
        "DeclareGraphicsRule",
        "graphicspath",
        "pgfplotstableread",
        "addplot",
        "addplot3",
        "verbatiminput",
        "setmainfont",
        "setsansfont",
        "setmonofont",
        "fontspec",
        "newfontfamily",
        "newfontface",
        "bibliographystyle",
    }
)
