"""Which LaTeX commands read a project file, and how each names its file.

The tables here are the source graph's vocabulary: the commands whose argument is a
file name, the suffixes TeX tries for each, the commands that take a directory first,
the ones TeX may satisfy from its own installation, and the ones the graph follows
into the file they name. Nothing here reads a file or looks at a project; the
modules that resolve and walk references (``source_resolution``, ``source_graph``)
and the ones that judge commands (``source_command_checks``) share these names.
"""

from __future__ import annotations

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


def file_argument_index(command_name: str) -> int:
    """The index of the argument that names the file: after the directory, if there is one."""
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
    """Whether ``value`` is nonempty and free of TeX syntax that could change its meaning."""
    return bool(value) and not any(char in value for char in "\\{}#$^~&%\n\r\x00")
