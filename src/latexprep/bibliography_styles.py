"""The string macros that BibTeX styles define, so that they are not reported as undefined.

A .bib file may use macros such as ``jan`` or ``jacm`` that no ``@string`` in it defines,
because the bibliography style defines them. This module owns what is known about that:
the month macros every style has, the journal macros of the standard styles, the
``MACRO`` definitions of a .bst file that ships in the project or that the TeX
installation holds, such as ``ACM-Reference-Format.bst``, and the ``@string`` names of a
.bib file that BibTeX reads from the installation, such as IEEE's ``IEEEabrv.bib``. A
style or installation file that is named but unavailable is recorded as unread, so the
caller can report an uncertainty instead of an error. It reads no project .bib file and
builds no finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .cancellation import cancellation_point
from .citation_graph import StyleReference
from .file_safety import read_regular_file
from .limits import MIB
from .source_file_commands import STANDARD_BIBTEX_STYLES
from .source_resolution import installation_file_names, installed_input_path

MONTH_MACROS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
# The standard styles, and the natbib and other styles derived from them, define these
# journal macros besides the months; other styles, such as ACM-Reference-Format, define
# more or fewer.
_STANDARD_STYLE_MACROS = frozenset(
    "acmcs acta cacm ibmjrd ibmsj ieeese ieeetc ieeetcad ipl jacm jcss scp sicomp tocs "
    "tods tog toms toois toplas tcs".split()
)
_MAX_STYLE_BYTES = 2 * MIB
_STYLE_MACRO = re.compile(r"^[ \t]*MACRO[ \t]*\{[ \t]*([^\s{}]+)[ \t]*\}", re.MULTILINE)
# An @string declaration's name, as BibTeX reads it: @string{name = ...} or @string(name = ...).
_STRING_MACRO = re.compile(r"@[ \t]*string[ \t\n]*[{(][ \t\n]*([^\s=#{}(),\"%]+)[ \t\n]*=", re.I)
# BST comments run from % to the end of the line.
_STYLE_COMMENT = re.compile(r"%[^\n]*")


@dataclass(frozen=True, slots=True)
class StyleMacros:
    """The string macros the BibTeX style in use is known to define.

    Attributes:
        macros: The macro names the styles are known to define.
        unread: The styles whose definitions are not available here, so any macro they
            might define stays uncertain.
        unknown: Whether no document names a style at all, as in a bibliography-only
            directory.
    """

    macros: frozenset[str] = frozenset()
    unread: tuple[str, ...] = ()
    unknown: bool = False

    @property
    def uncertain(self) -> bool:
        """Whether a macro without a definition might still be defined by a style."""
        return self.unknown or bool(self.unread)

    def explanation(self) -> str:
        """Explain why an undefined macro may still be fine, naming the style when it is known.

        Returns:
            A clause naming the unread styles, or any style when none is named.
        """
        if self.unread:
            files = [repr(name) for name in self.unread if name.endswith(".bib")]
            styles = [repr(name) for name in self.unread if not name.endswith(".bib")]
            named = [
                f"{kind} {', '.join(names)}"
                for kind, names in (("style", styles), ("file", files))
                if names
            ]
            return f"the bibliography {' or '.join(named)} may define it"
        return "a bibliography style used with this file may define it"


def style_macros(
    root: Path, styles: list[StyleReference] | None, installed: tuple[str, ...] = ()
) -> StyleMacros:
    """Collect the macros of the named styles: .bst files are read, standard ones known.

    A style is read from its project .bst file, known when it is standard, and otherwise
    read from the TeX installation's .bst file when the installation holds it.

    Args:
        root: The project directory that the local style files are relative to.
        styles: The styles the documents name, or ``None`` when no document could be read
            to find them.
        installed: The installation paths of .bib files BibTeX also reads, whose
            ``@string`` names are added.

    Returns:
        The known macros, the styles and installation files that could not be read or
        are not standard, and whether the styles are unknown altogether.
    """
    macros: set[str] = set()
    unread: list[str] = []
    for path in installed:
        cancellation_point()
        strings = _read_strings(Path(path))
        if strings is None:
            unread.append(Path(path).name)
        else:
            macros |= strings
    if styles is None:
        return StyleMacros(frozenset(macros), tuple(unread), unknown=True)
    for style in styles:
        cancellation_point()
        defined = _named_style_macros(root, style)
        if defined is None:
            unread.append(style.name)
        else:
            macros |= defined
    return StyleMacros(frozenset(macros), tuple(unread))


def _named_style_macros(root: Path, style: StyleReference) -> frozenset[str] | None:
    """Return the macros one named style defines, or ``None`` when they are not available.

    Args:
        root: The project directory that the local style file is relative to.
        style: The style name and the project .bst file it resolved to, if any.

    Returns:
        The macros of the project .bst file, of a standard style, or of the TeX
        installation's .bst file; ``None`` when the file cannot be read or the style is
        neither standard nor held by the installation.
    """
    if style.local_file is not None:
        return _read_style(root / style.local_file)
    if style.name.removesuffix(".bst").casefold() in STANDARD_BIBTEX_STYLES:
        return _STANDARD_STYLE_MACROS
    for file_name in installation_file_names(style.name, "bibliographystyle"):
        installed = installed_input_path(file_name)
        if installed is not None:
            return _read_style(installed)
    return None


def _read_strings(path: Path) -> set[str] | None:
    """Read the ``@string`` names of an installation .bib file, or ``None`` when it cannot be read.

    BibTeX reads bytes, so a file in another encoding, as ``IEEEabrv.bib`` is, is read too.
    """
    try:
        text = read_regular_file(path, _MAX_STYLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return {name.casefold() for name in _STRING_MACRO.findall(text)}


def _read_style(path: Path) -> frozenset[str] | None:
    """Read the macro names a .bst file defines, or ``None`` when the file cannot be read."""
    try:
        text = read_regular_file(path, _MAX_STYLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return frozenset(name.casefold() for name in _STYLE_MACRO.findall(_STYLE_COMMENT.sub("", text)))
