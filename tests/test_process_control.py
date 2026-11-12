"""Stopping a child process without racing asyncio's child watcher."""

from __future__ import annotations

import asyncio
import signal
import sys
import unittest
from unittest.mock import patch

from latexprep.process_control import kill_unfinished


class KillUnfinishedTests(unittest.IsolatedAsyncioTestCase):
    """``kill_unfinished`` signals a running child by its pid and leaves an exited one alone."""

    async def test_a_running_process_is_killed_without_polling_it(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)"
        )
        # Process.kill polls the child before signalling it; on Python 3.11 and 3.12
        # that poll can reap the child before asyncio's watcher does (bpo-43578).
        with patch.object(process, "kill", side_effect=AssertionError("Process.kill polls")):
            kill_unfinished(process)
        self.assertEqual(await process.wait(), -signal.SIGKILL)

    async def test_a_process_that_has_exited_is_not_signalled(self) -> None:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        self.assertEqual(await process.wait(), 0)
        with patch("latexprep.process_control.os.kill") as kill:
            kill_unfinished(process)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
