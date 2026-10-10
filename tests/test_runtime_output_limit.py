from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.runtime import RuntimeLimits
from tests.test_runtime import LifecycleRunner


class OutputLimitDrainTests(unittest.TestCase):
    """A tool killed for excessive output must still finish when pipe reads lag.

    The process counts as finished only once every pipe reaches end-of-file. If the
    reader stopped at the limit while asyncio still buffered data, the pipe never
    drained, and the shielded cleanup waited forever. Slow reads reproduce the
    buffered state deterministically; the scenario runs in its own thread so a
    regression fails the test instead of hanging the suite.
    """

    def test_output_limited_process_finishes_when_reads_lag(self) -> None:
        perl = shutil.which("perl")
        if perl is None:
            self.skipTest("perl is required to produce bulk output")
        original_read = asyncio.StreamReader.read

        async def lagging_read(self: asyncio.StreamReader, n: int = -1) -> bytes:
            await asyncio.sleep(0.05)
            return await original_read(self, n)

        async def scenario(work: Path):
            with patch.object(asyncio.StreamReader, "read", lagging_read):
                runner = LifecycleRunner(RuntimeLimits(max_output_bytes=4096))
                return await runner.run([perl, "-e", 'print "a" x 2000000; sleep 10;'], work, work)

        outcome: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()

            def run_scenario() -> None:
                try:
                    outcome["result"] = asyncio.run(scenario(work))
                except BaseException as error:  # pragma: no cover - reported below
                    outcome["error"] = error

            thread = threading.Thread(target=run_scenario, daemon=True)
            thread.start()
            thread.join(20)
            self.assertFalse(thread.is_alive(), "the output-limited run did not finish")
        self.assertNotIn("error", outcome, outcome.get("error"))
        result = outcome["result"]
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 4096)
        self.assertNotEqual(result.returncode, 0)
