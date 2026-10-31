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

    Args:
        stream: The stream that receives the stripped text.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._pending = ""

    @property
    def encoding(self) -> str:
        """The encoding of the wrapped stream, or ``utf-8`` when it names none."""
        return getattr(self._stream, "encoding", None) or "utf-8"

    def fileno(self) -> int:
        """Return the file descriptor of the wrapped stream."""
        return self._stream.fileno()

    def isatty(self) -> bool:
        """Return whether the wrapped stream is an open terminal."""
        return _is_terminal(self._stream)

    def write(self, text: str) -> int:
        """Write ``text`` without the blanks that end its lines.

        Blanks at the end of ``text`` are held back until the next chunk shows whether its
        line goes on.

        Args:
            text: The chunk to write.

        Returns:
            The length of ``text``, as if all of it had been written.
        """
        accepted = len(text)
        text = _TRAILING_BLANKS.sub("", self._pending + text)
        stripped = text.rstrip(" \t")
        self._pending = text[len(stripped) :]
        self._stream.write(stripped)
        return accepted

    def flush(self) -> None:
        """Flush the wrapped stream; blanks held back stay pending."""
        self._stream.flush()


def _is_terminal(stream: IO[str]) -> bool:
    """Return whether ``stream`` is a terminal; a closed stream is not."""
    try:
        return stream.isatty()
    except ValueError:  # a closed stream
        return False


class _ReportConsole(Console):
    """The console of the final report: a reader that stops early is not an error.

    Rich ends the process with exit status 1 when the reader of a pipe closes it, such as
    ``fledge inspect INPUT | head``. That would hide the verdict of the run. Raising the
    ``BrokenPipeError`` instead lets the command handle it, keep its own exit code and
    stop writing to the closed stream.
    """

    def on_broken_pipe(self) -> None:
        """Raise the broken pipe to the command instead of ending the process.

        Raises:
            BrokenPipeError: Always, so the command keeps its own exit code.
        """
        raise BrokenPipeError


def stdout_console() -> Console:
    """Return the console for the final report on standard output.

    When standard output is not a terminal the report carries no trailing blanks.
    """
    stream = sys.stdout
    if _is_terminal(stream):
        return _ReportConsole(markup=False, highlight=False)
    # Rich writes, flushes and asks isatty, encoding and fileno; the wrapper offers exactly those.
    file = cast(IO[str], _TrailingBlankStripper(stream))
    return _ReportConsole(file=file, markup=False, highlight=False)


def stderr_console() -> Console:
    """Return the console for progress and other messages on standard error."""
    return Console(stderr=True, markup=False, highlight=False)
