"""Opt-in diagnostic log lines on stderr, for people reporting or investigating a problem.

Every module that has something to say about its progress writes it to a logger named
after the module (``logging.getLogger(__name__)``). The library itself never configures
logging: the package logger carries only a ``NullHandler`` (see ``latexprep/__init__``), so
an embedding program decides what to do with the records. The command line attaches one
stderr handler for the length of a command when ``--debug`` or ``FLEDGE_DEBUG=1`` asks for
it, so standard output stays reserved for the report.

Log messages are written to carry no paths, URLs or contact details; the handler's filter
is a second line of defence that shows this machine's home and temporary directories
generically, as reports do.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import TextIO

from .redaction import local_directory_labels, scrub_local_paths

PACKAGE_LOGGER_NAME = __name__.partition(".")[0]


class LocalPathFilter(logging.Filter):
    """Replaces this machine's home and temporary directories in every record's message."""

    def __init__(self, labels: dict[str, str] | None = None) -> None:
        super().__init__()
        self._labels = local_directory_labels() if labels is None else labels

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub_local_paths(record.getMessage(), self._labels)
        record.args = ()
        return True


class ConciseFormatter(logging.Formatter):
    """``+1.234s runtime: message``: the time since start, the module and what it says.

    A record above DEBUG also names its level, so a limit that stopped a tool stands out.
    """

    def format(self, record: logging.LogRecord) -> str:
        module = record.name.removeprefix(f"{PACKAGE_LOGGER_NAME}.")
        level = f" [{record.levelname.lower()}]" if record.levelno > logging.DEBUG else ""
        return f"+{record.relativeCreated / 1000:.3f}s {module}{level}: {record.getMessage()}"


class _DebugHandler(logging.Handler):
    """Writes each record to ``stream``, or to whatever ``sys.stderr`` is at that moment.

    Looking ``sys.stderr`` up per record, as ``print`` does, keeps the handler correct when a
    test runner or embedding program replaces the stream after the command started.
    """

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__(logging.DEBUG)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
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
    """
    package = logging.getLogger(PACKAGE_LOGGER_NAME)
    if any(isinstance(handler, _DebugHandler) for handler in package.handlers):
        yield
        return
    handler = _DebugHandler(stream)
    handler.addFilter(LocalPathFilter())
    handler.setFormatter(ConciseFormatter())
    previous_level = package.level
    package.addHandler(handler)
    package.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        package.removeHandler(handler)
        package.setLevel(previous_level)
