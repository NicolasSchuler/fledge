from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from latexprep.runtime import ToolRunner


class SwallowingSampleRunner(ToolRunner):
    """Swallow the monitor's cancellation once, as asyncio.wait_for can on Python 3.11."""

    def __init__(self) -> None:
        super().__init__(source_date_epoch=0)
        self.monitor_sampling = asyncio.Event()
        self.swallowed_cancellations = 0

    async def _sample_job(self) -> None:
        if asyncio.current_task() is not self._job_monitor or self.swallowed_cancellations:
            # The final sample in job_scope and later monitor samples return at once.
            return
        self.monitor_sampling.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.swallowed_cancellations += 1


class JobMonitorCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_scope_closes_when_a_sample_swallows_the_monitor_cancellation(
        self,
    ) -> None:
        runner = SwallowingSampleRunner()
        with tempfile.TemporaryDirectory() as directory:

            async def use_scope() -> None:
                async with runner.job_scope(Path(directory), max_temp_bytes=1_000_000):
                    await runner.monitor_sampling.wait()

            scope = asyncio.create_task(use_scope())
            done, _ = await asyncio.wait({scope}, timeout=5)
            monitor = runner._job_monitor
            if not done and monitor is not None:
                # Unblock the hung scope so a regression fails instead of hanging.
                while not scope.done():
                    monitor.cancel()
                    await asyncio.sleep(0.01)
            self.assertIn(scope, done, "job_scope did not close after the monitor was cancelled")
            await scope
        self.assertEqual(runner.swallowed_cancellations, 1)
        self.assertIsNone(runner._job_monitor)


if __name__ == "__main__":
    unittest.main()
