"""Making untrusted text harmless for one output channel.

File names, source excerpts and tool output reach the terminal, HTML reports, CI
annotation lines and one-line messages. Each channel treats control characters
differently, so there is one function per channel; all of them share the single
decision of which characters are unsafe, :func:`is_unsafe_character`.

``sanitize_terminal_text``
    Terminal output. Drops terminal escape sequences and control characters,
    keeps newlines and tabs, and shows every other unsafe character as an escape.
``escape_html_text``
    HTML content or attribute text. Keeps newlines and tabs, shows every other
    unsafe character as an escape, then applies HTML escaping.
``escape_annotation_text``
    One GitHub Actions workflow command. Percent-encodes characters the command
    syntax reserves (including newlines, so the text stays on one physical line)
    and shows non-ASCII unsafe characters as escapes.
``escape_single_line_text``
    A message that must stay on one line. Shows every unsafe character, newlines
    and tabs included, as an escape.

An unsafe character is shown as a visible ``\\uXXXX`` (``\\UXXXXXXXX`` beyond the
basic plane) escape wherever a channel does not drop it.
"""

from __future__ import annotations

import html
import re
import unicodedata

# An operating-system command (title, hyperlink) ends with BEL or a string terminator;
# a control sequence (colours, cursor movement) ends with a final byte.
_OPERATING_SYSTEM_COMMAND = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_NEWLINE_AND_TAB = "\n\t"


def is_unsafe_character(char: str) -> bool:
    """Whether a character can hide, reorder or corrupt displayed text.

    That is every Unicode "other" character: C0 and C1 controls, format characters
    such as right-to-left overrides, lone surrogates (what an undecodable file name
    becomes), private-use code points and unassigned code points.
    """
    return unicodedata.category(char).startswith("C")


def is_control_character(char: str) -> bool:
    """Whether a character is a C0 or C1 control, DEL included."""
    return unicodedata.category(char) == "Cc"


def visible_escape(char: str) -> str:
    """The ``\\uXXXX`` or ``\\UXXXXXXXX`` escape that shows ``char`` literally."""
    code = ord(char)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _show_unsafe(value: str, keep: str = "") -> str:
    """``value`` with each unsafe character not in ``keep`` replaced by its escape."""
    return "".join(
        visible_escape(char) if is_unsafe_character(char) and char not in keep else char
        for char in value
    )


def sanitize_terminal_text(value: str) -> str:
    """Remove escape sequences and control characters, keeping newlines and tabs.

    Other unsafe characters (for example right-to-left overrides that can reorder a
    displayed path, or a lone surrogate that no terminal encoding can print) are
    shown as ``\\uXXXX`` escapes.
    """
    # File names and diagnostics are untrusted text, never terminal escape commands.
    value = _OPERATING_SYSTEM_COMMAND.sub("", value)
    value = _CONTROL_SEQUENCE.sub("", value)
    return _show_unsafe(
        "".join(
            char for char in value if char in _NEWLINE_AND_TAB or not is_control_character(char)
        ),
        _NEWLINE_AND_TAB,
    )


def escape_html_text(value: str) -> str:
    """Escape text for HTML content and attributes; unsafe characters become visible.

    Newlines and tabs are kept: they are ordinary whitespace in preformatted evidence.
    """
    return html.escape(_show_unsafe(value, _NEWLINE_AND_TAB), quote=True)


def escape_annotation_text(value: str, *, property_value: bool = False) -> str:
    """Escape text for a GitHub Actions workflow command message or property value.

    ``%``, carriage returns, newlines and the other ASCII controls are percent-encoded,
    so injected text cannot start a second command; with ``property_value`` the
    property separators ``:`` and ``,`` are encoded as well.
    """
    reserved = "%:," if property_value else "%"
    return "".join(
        f"%{ord(char):02X}"
        if char in reserved or ord(char) < 32 or ord(char) == 127
        else visible_escape(char)
        if is_unsafe_character(char)
        else char
        for char in value
    )


def escape_single_line_text(value: str) -> str:
    """Show every unsafe character, line breaks and tabs included, as a visible escape."""
    return _show_unsafe(value)
