"""The string macros that BibTeX styles define, so that they are not reported as undefined.

A .bib file may use macros such as ``jan`` or ``jacm`` that no ``@string`` in it defines,
because the bibliography style defines them. This module owns what is known about that:
the month macros every style has, the journal macros of the standard styles, and the
``MACRO`` definitions of a .bst file that ships in the project. A style that is named
but unavailable is recorded as unread, so the caller can report an uncertainty instead
of an error. It reads no .bib file and builds no finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .cancellation import cancellation_point
from .citation_graph import StyleReference
from .file_safety import read_regular_file
from .limits import MIB

MONTH_MACROS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
# The standard styles, and the natbib and other styles derived from them, define these
# journal macros besides the months; other styles, such as ACM-Reference-Format, define
# more or fewer.
_STANDARD_STYLE_MACROS = frozenset(
    "acmcs acta cacm ibmjrd ibmsj ieeese ieeetc ieeetcad ipl jacm jcss scp sicomp tocs "
    "tods tog toms toois toplas tcs".split()
)
_STANDARD_STYLES = frozenset(
    "plain unsrt alpha abbrv ieeetr acm siam apalike plainnat unsrtnat abbrvnat".split()
)
_MAX_STYLE_BYTES = 2 * MIB
_STYLE_MACRO = re.compile(r"^[ \t]*MACRO[ \t]*\{[ \t]*([^\s{}]+)[ \t]*\}", re.MULTILINE)
# BST comments run from % to the end of the line.
_STYLE_COMMENT = re.compile(r"%[^\n]*")


@dataclass(frozen=True, slots=True)
class StyleMacros:
    """The string macros the BibTeX style in use is known to define.

    ``unread`` names the styles whose definitions are not available here, so any
    macro they might define stays uncertain. ``unknown`` means no document names
    a style at all, as in a bibliography-only directory.
    """

    macros: frozenset[str] = frozenset()
    unread: tuple[str, ...] = ()
    unknown: bool = False

    @property
    def uncertain(self) -> bool:
        """Whether a macro without a definition might still be defined by a style."""
        return self.unknown or bool(self.unread)

    def explanation(self) -> str:
        """Why an undefined macro may still be fine, naming the style when it is known."""
        if self.unread:
            styles = ", ".join(repr(name) for name in self.unread)
            return f"the bibliography style {styles} may define it"
        return "a bibliography style used with this file may define it"


def style_macros(root: Path, styles: list[StyleReference] | None) -> StyleMacros:
    """Macros of the styles ``styles`` name: project .bst files are read, standard ones known.

    ``styles`` is None when no document could be read to find them.
    """
    if styles is None:
        return StyleMacros(unknown=True)
    macros: set[str] = set()
    unread: list[str] = []
    for style in styles:
        cancellation_point()
        base = style.name.removesuffix(".bst").casefold()
        if style.local_file is not None:
            defined = _read_style(root / style.local_file)
            if defined is None:
                unread.append(style.name)
            else:
                macros |= defined
        elif base in _STANDARD_STYLES:
            macros |= _STANDARD_STYLE_MACROS
        else:
            unread.append(style.name)
    return StyleMacros(frozenset(macros), tuple(unread))


def _read_style(path: Path) -> set[str] | None:
    """The macro names a .bst file defines, or None when the file cannot be read."""
    try:
        text = read_regular_file(path, _MAX_STYLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return {name.casefold() for name in _STYLE_MACRO.findall(_STYLE_COMMENT.sub("", text))}
