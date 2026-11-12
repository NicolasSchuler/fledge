"""Progress on stderr: the live line on a terminal and phase-level lines when redirected."""

from __future__ import annotations

import os
import re
import sys
import unittest
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from latexprep.terminal_progress import ProgressEvent, TerminalProgress, read_progress_message


class TerminalBuffer(StringIO):
    """A string buffer that claims to be a terminal."""

    def isatty(self) -> bool:
        """Return ``True``, as a terminal would."""
        return True


class FakeClock:
    """A progress clock that moves only when the test says so.

    Attributes:
        now: The current reading, in seconds.
    """

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


# The messages of a short `check` run, as the job emits them.
CHECK_RUN = (
    "Importing a separate project snapshot",
    "Starting baseline build",
    "Starting source checks",
    "Starting source privacy and bundle checks",
    "source checks: succeeded (0.0s)",
    "source privacy and bundle checks: succeeded (0.1s)",
    "baseline build: succeeded (4.7s)",
    "Starting baseline PDF inspection",
    "baseline PDF inspection: failed (0.3s)",
    "Rebuilding archive sources",
    "archive build: succeeded (3.9s)",
    "Publishing the verified local output",
)


class ProgressMessageTests(unittest.TestCase):
    """How a progress message is read: stage events, phases, wording and phase level."""

    def test_messages_are_read_as_stage_events_or_phases(self):
        cases = (
            ("Starting baseline build", "started", "baseline build", "Starting baseline build"),
            ("Cancelled source checks", "cancelled", "source checks", "Cancelled source checks"),
            (
                "source checks: succeeded (0.1s)",
                "finished",
                "source checks",
                "source checks: done (0.1s)",
            ),
            (
                "baseline build: failed (4.0s)",
                "finished",
                "baseline build",
                "baseline build: failed (4.0s)",
            ),
            (
                "source checks: done, 1 error, 2 warnings (0.1s)",
                "finished",
                "source checks",
                "source checks: done, 1 error, 2 warnings (0.1s)",
            ),
            (
                "paper: Starting baseline build",
                "started",
                "paper: baseline build",
                "paper: Starting baseline build",
            ),
            (
                "paper: baseline build: succeeded (2.0s)",
                "finished",
                "paper: baseline build",
                "paper: baseline build: done (2.0s)",
            ),
            ("Rebuilding archive sources", "phase", "Rebuilding archive sources", None),
        )
        for message, kind, stage, text in cases:
            with self.subTest(message=message):
                event = read_progress_message(message)
                self.assertEqual((event.kind, event.stage), (kind, stage))
                self.assertEqual(event.text, text or message)

    def test_phase_level_means_phases_builds_cancellations_and_unclean_endings(self):
        expected = {
            "Importing a separate project snapshot": True,
            "Starting baseline build": True,
            "Starting source checks": False,
            "source checks: succeeded (0.0s)": False,
            "source checks: done, 1 error (0.0s)": True,
            "baseline PDF inspection: failed (0.3s)": True,
            "archive build: succeeded (3.9s)": True,
            "Cancelled source checks": True,
        }
        for message, phase_level in expected.items():
            with self.subTest(message=message):
                self.assertEqual(read_progress_message(message).phase_level, phase_level)
        self.assertTrue(ProgressEvent("phase", "x", "x").clean)


class RedirectedProgressTests(unittest.TestCase):
    """Progress on a redirected or dumb stderr: phase-level lines only, literal text."""

    def test_redirected_progress_prints_phase_level_lines_only(self):
        output = StringIO()
        with TerminalProgress(Console(file=output, width=100, markup=True)) as update:
            self.assertIsNotNone(update)
            for message in CHECK_RUN:
                update(message)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "Importing a separate project snapshot",
                "Starting baseline build",
                "baseline build: done (4.7s)",
                "baseline PDF inspection: failed (0.3s)",
                "Rebuilding archive sources",
                "archive build: done (3.9s)",
                "Publishing the verified local output",
            ],
        )

    def test_redirected_progress_is_literal_and_free_of_control_characters(self):
        output = StringIO()
        with TerminalProgress(Console(file=output, width=80, markup=True)) as update:
            update("Checking [bold]literal[/bold]\x1b[31m")
        self.assertEqual(output.getvalue(), "Checking [bold]literal[/bold]\n")

    def test_quiet_progress_emits_nothing(self):
        for terminal in (False, True):
            output = TerminalBuffer() if terminal else StringIO()
            with TerminalProgress(Console(file=output), quiet=True) as update:
                self.assertIsNone(update)
            self.assertEqual(output.getvalue(), "")

    def test_dumb_terminal_prints_phase_level_lines(self):
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=False):
            output = TerminalBuffer()
            with TerminalProgress(Console(file=output)) as update:
                update("Starting baseline build")
                update("Starting source checks")
            self.assertEqual(output.getvalue(), "Starting baseline build\n")


class LiveProgressTests(unittest.TestCase):
    """The live line on a terminal: the running stage, its time, and restoring the streams."""

    def live(self) -> tuple[TerminalProgress, FakeClock]:
        """Return progress on a fake 80-column terminal with a clock the test moves."""
        clock = FakeClock()
        console = Console(file=TerminalBuffer(), width=80, force_terminal=True, color_system=None)
        return TerminalProgress(console, clock=clock), clock

    def test_the_live_line_shows_the_running_stage_not_the_last_finished_one(self):
        progress, clock = self.live()
        with progress as update:
            assert update is not None
            update("Importing a separate project snapshot")
            clock.now += 1
            self.assertEqual(
                progress.describe().plain, "Importing a separate project snapshot (1s)"
            )
            update("Starting baseline build")
            update("Starting source checks")
            update("Starting source privacy and bundle checks")
            clock.now += 2
            self.assertEqual(progress.describe().plain, "baseline build (2s) +2 more")
            update("source checks: succeeded (0.0s)")
            update("source privacy and bundle checks: succeeded (0.1s)")
            clock.now += 63
            # The build is still running: the line names it, never a finished stage.
            self.assertEqual(progress.describe().plain, "baseline build (1m05s)")
            update("baseline build: succeeded (65.0s)")
            self.assertEqual(progress.describe().plain, "baseline build: done (65.0s)")
            update("Rebuilding archive sources")
            clock.now += 3
            self.assertEqual(progress.describe().plain, "Rebuilding archive sources (3s)")

    def test_each_document_of_several_keeps_its_own_stages(self):
        progress, clock = self.live()
        with progress as update:
            assert update is not None
            update("paper: Starting baseline build")
            clock.now += 1
            update("supplement: Starting baseline build")
            update("paper: baseline build: succeeded (2.0s)")
            clock.now += 1
            self.assertEqual(progress.describe().plain, "supplement: baseline build (1s)")

    def test_terminal_progress_has_stage_elapsed_and_restores_on_exception(self):
        output = TerminalBuffer()
        stdout, stderr = sys.stdout, sys.stderr
        progress = TerminalProgress(
            Console(file=output, width=60, force_terminal=True, color_system=None)
        )
        with self.assertRaisesRegex(RuntimeError, "controlled failure"):
            with progress as update:
                assert update is not None
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
                update("Checking [bold]literal[/bold]\x1b[31m")
                self.assertIsNotNone(progress._progress)
                raise RuntimeError("controlled failure")
        text = output.getvalue()
        self.assertIn("Checking [bold]literal[/bold]", text)
        self.assertRegex(text, r"\d+:\d\d:\d\d")
        self.assertNotIn("\x1b[31m", text)
        self.assertIsNone(progress._progress)
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)

    def test_no_color_terminal_retains_progress_without_color_codes(self):
        with patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}, clear=False):
            output = TerminalBuffer()
            with TerminalProgress(Console(file=output, width=60)) as update:
                assert update is not None
                update("Checking sources")
            text = output.getvalue()
            self.assertIn("Checking sources", text)
            # Cursor controls are expected for live progress, SGR colors are not.
            self.assertFalse(re.search(r"\x1b\[[0-9;]*(?:3[0-7]|9[0-7])m", text), text)


if __name__ == "__main__":
    unittest.main()
