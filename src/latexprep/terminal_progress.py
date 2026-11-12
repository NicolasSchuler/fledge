"""Progress on stderr: one live line on a terminal, phase-level lines when redirected.

A job reports its progress as short messages. A scheduled stage starts ("Starting
baseline build"), ends ("baseline build: succeeded (4.1s)") or is cancelled ("Cancelled
source checks"); every other message begins a phase ("Rebuilding archive sources").
Stages of one document of several carry its name first ("paper: Starting ...").

On a terminal, one transient line shows the stage that has been running longest with
its own elapsed time, and how many more are running, so a long build stays visible while
quick checks start and end beside it; between stages it shows the current phase. When
stderr is not a terminal, or is a dumb one, only phase-level lines are printed: phases,
the start and end of each build, and every stage that did not end cleanly. A finished
stage reads ``done`` rather than ``succeeded``, and keeps any count of problems that its
message carries ("done, 1 error").
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, NamedTuple

from rich.console import Console
from rich.progress import Progress, ProgressColumn, SpinnerColumn, Task, TimeElapsedColumn
from rich.table import Column
from rich.text import Text

from .text_safety import sanitize_terminal_text

# The name of one document of several, which prefixes the messages of its stages.
_DOCUMENT = r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}: )?"
_STARTED = re.compile(rf"(?P<document>{_DOCUMENT})Starting (?P<stage>.+)")
_CANCELLED = re.compile(rf"(?P<document>{_DOCUMENT})Cancelled (?P<stage>.+)")
_FINISHED = re.compile(
    r"(?P<stage>.+): (?P<status>succeeded|failed|done)(?P<counts>(?:, [^()]+)?)"
    r" \((?P<seconds>\d+(?:\.\d+)?)s\)"
)
# The stages that build a PDF end like this ("baseline build", "archive build"); they are
# the long steps a log should show even when only phases are printed.
_BUILD_SUFFIX = " build"
_STATUS_WORDS = {"succeeded": "done", "done": "done", "failed": "failed"}


class ProgressEvent(NamedTuple):
    """One progress message, read.

    Attributes:
        kind: ``started``, ``finished`` or ``cancelled`` for a stage, ``phase`` otherwise.
        stage: The stage, prefixed with its document's name when it has one, or the whole
            message of a phase.
        text: The line to show: the message, with a finished stage's status as ``done``
            or ``failed``.
        clean: Whether a finished stage succeeded without a count of problems; always
            ``True`` for the other kinds.
    """

    kind: Literal["started", "finished", "cancelled", "phase"]
    stage: str
    text: str
    clean: bool = True

    @property
    def phase_level(self) -> bool:
        """Whether the event belongs in a log that shows phases only."""
        if self.kind in {"phase", "cancelled"} or not self.clean:
            return True
        return self.stage.endswith(_BUILD_SUFFIX)


def read_progress_message(message: str) -> ProgressEvent:
    """Read one progress message as a stage that starts, finishes or is cancelled, or a phase.

    Args:
        message: The message, already safe for the terminal.

    Returns:
        The event.
    """
    if started := _STARTED.fullmatch(message):
        return ProgressEvent("started", started["document"] + started["stage"], message)
    if cancelled := _CANCELLED.fullmatch(message):
        return ProgressEvent("cancelled", cancelled["document"] + cancelled["stage"], message)
    if finished := _FINISHED.fullmatch(message):
        status = _STATUS_WORDS[finished["status"]]
        counts = finished["counts"]
        return ProgressEvent(
            "finished",
            finished["stage"],
            f"{finished['stage']}: {status}{counts} ({finished['seconds']}s)",
            clean=status == "done" and not counts,
        )
    return ProgressEvent("phase", message, message)


def _elapsed(seconds: float) -> str:
    """Write a duration as whole seconds, or minutes and seconds from one minute on."""
    whole = max(0, int(seconds))
    return f"{whole}s" if whole < 60 else f"{whole // 60}m{whole % 60:02d}s"


class _Shown(NamedTuple):
    """What the live line shows.

    Attributes:
        label: The running stage, the current phase or the last finished stage.
        since: When the shown stage or phase began, on the progress clock, or ``None``
            for a finished stage, which shows no running time.
        more: How many other stages are running.
    """

    label: str
    since: float | None
    more: int = 0


# What the live line shows before the first message.
_STARTING = _Shown("Starting", None)


@dataclass
class _Activity:
    """The running stages, the current phase and the last finished stage.

    Only the thread that records events changes this; the live line reads ``shown``,
    which every recorded event replaces as a whole.

    Attributes:
        running: When each running stage started, in the order they started.
        phase: The current phase and when it began, until a stage starts or ends.
        last: The line of the stage that finished last.
        shown: What the live line shows.
    """

    running: dict[str, float] = field(default_factory=dict)
    phase: tuple[str, float] | None = None
    last: str | None = None
    shown: _Shown = _STARTING

    def record(self, event: ProgressEvent, now: float) -> None:
        """Take one event into account and decide what the live line shows next.

        Args:
            event: The event.
            now: The progress clock when the event arrived.
        """
        if event.kind == "phase":
            self.phase = (event.text, now)
        elif event.kind == "started":
            self.running[event.stage] = now
            self.phase = None
        else:
            self.running.pop(event.stage, None)
            self.phase = None
            self.last = event.text
        if self.running:
            stage, since = next(iter(self.running.items()))
            self.shown = _Shown(stage, since, len(self.running) - 1)
        elif self.phase is not None:
            self.shown = _Shown(*self.phase)
        else:
            self.shown = _Shown(self.last, None) if self.last else _STARTING


class _ActivityColumn(ProgressColumn):
    """The live line's text: the shown stage, its running time and how many more run.

    Rich draws it from its own refresh thread, which only reads.

    Args:
        describe: Returns the text to draw.
    """

    def __init__(self, describe: Callable[[], Text]) -> None:
        super().__init__(table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"))
        self._describe = describe

    def render(self, task: Task) -> Text:
        """Return the text of the live line.

        Args:
            task: The progress task, unused: the text comes from the activity.

        Returns:
            The text.
        """
        del task
        return self._describe()


class TerminalProgress:
    """A context manager yielding the core's optional string progress callback.

    Rich handles terminal and color detection; output streams are never captured or
    redirected by this helper. On a terminal that is not dumb, progress is one transient
    line; otherwise only phase-level messages are printed, each as a plain line.

    Attributes:
        console: The console progress is shown on; supply the CLI's stderr console.
        quiet: Whether progress is suppressed; a quiet instance yields ``None``.
    """

    def __init__(
        self,
        console: Console,
        *,
        quiet: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.console = console
        self.quiet = quiet
        self._clock = clock
        self._activity = _Activity()
        self._progress: Progress | None = None

    def __enter__(self) -> Callable[[str], None] | None:
        if self.quiet:
            return None
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            self._progress = Progress(
                SpinnerColumn(style="cyan"),
                _ActivityColumn(self.describe),
                TimeElapsedColumn(),
                console=self.console,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._progress.add_task("Starting", total=None)
            self._progress.start()
        return self.update

    def describe(self) -> Text:
        """Return the live line: the shown stage with its running time and the number of others."""
        shown = self._activity.shown
        text = shown.label
        if shown.since is not None:
            text += f" ({_elapsed(self._clock() - shown.since)})"
        if shown.more:
            text += f" +{shown.more} more"
        return Text(text, style="cyan")

    def update(self, message: str) -> None:
        """Take in one progress message, with terminal control characters made safe.

        Args:
            message: The progress message.
        """
        if self.quiet:
            return
        event = read_progress_message(sanitize_terminal_text(message))
        if self._progress is not None:
            self._activity.record(event, self._clock())
            self._progress.refresh()
        elif event.phase_level:
            self.console.print(Text(event.text), overflow="fold")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
