"""The Rich consoles every command prints through.

Owns how Fledge configures Rich: report text is never parsed as markup or highlighted,
because file names and messages are untrusted. Rich itself decides on colors from the
terminal, ``NO_COLOR`` and ``TERM``. The final report goes to standard output and
progress to standard error, so a redirected report stays free of progress lines.
"""

from __future__ import annotations

import re
import sys
from typing import IO, cast

from rich.console import Console

# Spaces and tabs before a line break; Rich pads a block to its width.
_TRAILING_BLANKS = re.compile(r"[ \t]+(?=\n)")


class _TrailingBlankStripper:
    """A write-only view of a stream that drops blanks at the end of each line.

    Rich pads indented blocks with spaces to the width of the block. A person looking
    at a terminal cannot see that, but redirected output carries it into files, diffs and
    pipes. Blanks held back at the end of a chunk are written once the next chunk shows
    that the line goes on.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._pending = ""

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", None) or "utf-8"

    def fileno(self) -> int:
        return self._stream.fileno()

    def isatty(self) -> bool:
        return _is_terminal(self._stream)

    def write(self, text: str) -> int:
        accepted = len(text)
        text = _TRAILING_BLANKS.sub("", self._pending + text)
        stripped = text.rstrip(" \t")
        self._pending = text[len(stripped) :]
        self._stream.write(stripped)
        return accepted

    def flush(self) -> None:
        self._stream.flush()


def _is_terminal(stream: IO[str]) -> bool:
    try:
        return stream.isatty()
    except ValueError:  # a closed stream
        return False


def stdout_console() -> Console:
    """The console for the final report on standard output.

    When standard output is not a terminal the report carries no trailing blanks.
    """
    stream = sys.stdout
    if _is_terminal(stream):
        return Console(markup=False, highlight=False)
    # Rich writes, flushes and asks isatty, encoding and fileno; the wrapper offers exactly those.
    file = cast(IO[str], _TrailingBlankStripper(stream))
    return Console(file=file, markup=False, highlight=False)


def stderr_console() -> Console:
    """The console for progress and other messages on standard error."""
    return Console(stderr=True, markup=False, highlight=False)
