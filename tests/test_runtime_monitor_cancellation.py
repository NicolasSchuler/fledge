"""A job scope closes even when a resource sample swallows the monitor's cancellation."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from latexprep.resource_monitor import JobScope
from latexprep.runtime import ToolRunner
from tests.fakes import ScriptedJobMonitor


class SwallowingSampleRunner(ToolRunner):
    """Swallow the monitor's cancellation once, as ``asyncio.wait_for`` can on Python 3.11.

    Attributes:
        monitor_sampling: Set when the monitor task starts the sample that blocks.
        swallowed_cancellations: How many cancellations the blocking sample swallowed.
    """

    def __init__(self) -> None:
        self.monitor_sampling = asyncio.Event()
        self.swallowed_cancellations = 0
        super().__init__(
            source_date_epoch=0,
            monitor_factory=lambda processes, statistics: ScriptedJobMonitor(
                processes, statistics, self.sample_job
            ),
        )

    async def sample_job(self, scope: JobScope) -> None:
        """Block the monitor's first sample until it is cancelled, then swallow the cancellation.

        Samples from other tasks, such as the final one in ``job_scope``, and every sample after the
        swallowed cancellation return at once.

        Args:
            scope: The job scope being sampled; its ``monitor`` task identifies the monitor.
        """
        if asyncio.current_task() is not scope.monitor or self.swallowed_cancellations:
            # The final sample in job_scope and later monitor samples return at once.
            return
        self.monitor_sampling.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.swallowed_cancellations += 1


class JobMonitorCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Closing a job scope whose monitor swallows its cancellation."""

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
            job_scope = runner.monitor.scope
            if not done and job_scope is not None:
                monitor = job_scope.monitor
                # Unblock the hung scope so a regression fails instead of hanging.
                while not scope.done():
                    monitor.cancel()
                    await asyncio.sleep(0.01)
            self.assertIn(scope, done, "job_scope did not close after the monitor was cancelled")
            await scope
        self.assertEqual(runner.swallowed_cancellations, 1)
        self.assertIsNone(runner.monitor.scope)


if __name__ == "__main__":
    unittest.main()
