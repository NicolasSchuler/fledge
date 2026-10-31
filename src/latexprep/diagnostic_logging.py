"""Opt-in diagnostic log lines on stderr, for people reporting or investigating a problem.

Every module that has something to say about its progress writes it to a logger named
after the module (``logging.getLogger(__name__)``). The library itself never configures
logging: the package logger carries only a ``NullHandler`` (see ``latexprep/__init__``), so
an embedding program decides what to do with the records. The command line attaches one
stderr handler for the length of a command when ``--debug`` or ``FLEDGE_DEBUG=1`` asks for
it, so standard output stays reserved for the report.

Log messages are written to carry no paths, URLs or contact details; the handler's
formatter is a second line of defence. It redacts credential-shaped text, shows this
machine's home and temporary directories generically, as reports do, and makes control
characters harmless, in the message and in the text of a logged exception. It works on its
own copy of the text, so other handlers of the same record see the record unchanged.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import TextIO

from .redaction import local_directory_labels, redact_data, scrub_local_paths
from .text_safety import sanitize_terminal_text

PACKAGE_LOGGER_NAME = __name__.partition(".")[0]


def safe_diagnostic_text(text: str, labels: dict[str, str] | None = None) -> str:
    """Make ``text`` safe to print: no credentials, no local directories, no escapes.

    Credentials (including URL query tokens) are redacted, this machine's home and temporary
    directories are shown generically, and terminal control characters are removed.

    Args:
        text: The text to make safe.
        labels: The directories to generalize, each with its generic label; it defaults to
            those of this machine.

    Returns:
        The safe text. A text too large to scan safely is withheld whole.
    """
    return sanitize_terminal_text(scrub_local_paths(redact_data(text), labels))


class ConciseFormatter(logging.Formatter):
    """``+1.234s runtime: message``: the time since start, the module and what it says.

    A record above DEBUG also names its level, so a limit that stopped a tool stands out.
    The text of a record's exception, with its traceback, follows the line. The result is
    made safe to print here, on a copy, so the record itself stays as the logger made it.

    Args:
        labels: The directories to generalize, each with its generic label; those of this
            machine when ``None``.
    """

    def __init__(self, labels: dict[str, str] | None = None) -> None:
        super().__init__()
        self._labels = local_directory_labels() if labels is None else labels

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` as one line that is safe to print.

        Returns:
            The line, followed by the text of the record's exception when it has one.
        """
        module = record.name.removeprefix(f"{PACKAGE_LOGGER_NAME}.")
        level = f" [{record.levelname.lower()}]" if record.levelno > logging.DEBUG else ""
        text = f"+{record.relativeCreated / 1000:.3f}s {module}{level}: {record.getMessage()}"
        if record.exc_info and record.exc_info[0] is not None:
            text += "\n" + self.formatException(record.exc_info)
        return safe_diagnostic_text(text, self._labels)


class _DebugHandler(logging.Handler):
    """Writes each record to ``stream``, or to whatever ``sys.stderr`` is at that moment.

    Looking ``sys.stderr`` up per record, as ``print`` does, keeps the handler correct when a
    test runner or embedding program replaces the stream after the command started.

    Args:
        stream: The stream to write to, or ``None`` for whatever ``sys.stderr`` is when a
            record arrives.
    """

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__(logging.DEBUG)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """Write the formatted record and a newline, then flush the stream.

        A closed or broken stream goes to ``handleError`` instead of stopping the command.
        """
        try:
            stream = self._stream or sys.stderr
            stream.write(self.format(record) + "\n")
            stream.flush()
        except (OSError, ValueError):  # a closed or broken stream must not stop the command
            self.handleError(record)


@contextmanager
def debug_logging(stream: TextIO | None = None) -> Generator[None]:
    """Show the package's log records, from DEBUG up, on ``stream`` (default: stderr).

    The package logger returns to its earlier level and handlers on exit. A nested use
    while another is active changes nothing: the outermost one owns the handler.

    Args:
        stream: The stream to write to, or ``None`` for whatever ``sys.stderr`` is when a
            record arrives.

    Yields:
        Nothing; the records are shown while the context is active.
    """
    package = logging.getLogger(PACKAGE_LOGGER_NAME)
    if any(isinstance(handler, _DebugHandler) for handler in package.handlers):
        yield
        return
    handler = _DebugHandler(stream)
    handler.setFormatter(ConciseFormatter())
    previous_level = package.level
    package.addHandler(handler)
    package.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        package.removeHandler(handler)
        package.setLevel(previous_level)
