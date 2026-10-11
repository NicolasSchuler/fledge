"""Transient progress on a terminal, plain stages when stderr is redirected."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Column
from rich.text import Text

from .presentation import _safe


class TerminalProgress:
    """A context manager yielding the core's optional string progress callback.

    Supply the CLI's stderr console. Rich handles terminal and color detection;
    output streams are never captured or redirected by this helper.
    """

    def __init__(self, console: Console, *, quiet: bool = False) -> None:
        self.console = console
        self.quiet = quiet
        self._progress: Progress | None = None
        self._task: TaskID | None = None

    def __enter__(self) -> Callable[[str], None] | None:
        if self.quiet:
            return None
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            self._progress = Progress(
                SpinnerColumn(style="cyan"),
                TextColumn(
                    "{task.description}",
                    style="cyan",
                    markup=False,
                    table_column=Column(ratio=1, overflow="fold"),
                ),
                TimeElapsedColumn(),
                console=self.console,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._task = self._progress.add_task("Starting", total=None)
            self._progress.start()
        return self.update

    def update(self, message: str) -> None:
        if self.quiet:
            return
        message = _safe(message)
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, description=message, refresh=True)
        else:
            self.console.print(Text(message), overflow="fold")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task = None
