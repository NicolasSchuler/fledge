from __future__ import annotations

import asyncio
import unittest

from latexprep.models import PreparationError
from latexprep.scheduler import Scheduler, Task


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_overlap_and_deterministic_results(self):
        active = 0
        peak = 0
        completed = []

        async def worker(name):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            completed.append(name)
            return name

        tasks = [Task(str(i), lambda i=i: worker(str(i)), memory_mb=64) for i in range(5)]
        concurrent = await Scheduler(3, 128).execute(tasks)
        self.assertEqual(peak, 2)
        serial = await Scheduler(1, 128).execute(tasks)
        self.assertEqual(
            [(name, result.value) for name, result in serial.items()],
            [(name, result.value) for name, result in concurrent.items()],
        )

    async def test_failure_blocks_artifact_consumers_but_diagnostics_run(self):
        async def fail():
            raise RuntimeError("build failed")

        async def succeed():
            return "diagnostics"

        results = await Scheduler(2, 128).execute(
            [
                Task("build", fail),
                Task("pdf", succeed, requires=("build",)),
                Task("report", succeed, after=("build", "pdf")),
                Task("source", succeed),
            ]
        )
        self.assertEqual(results["build"].status, "failed")
        # A bare message loses the diagnosis whenever it is empty or just a path.
        self.assertEqual(results["build"].error, "RuntimeError: build failed")
        self.assertEqual(results["pdf"].status, "blocked")
        # Reports group blocked tasks by their failed prerequisite.
        self.assertEqual(results["pdf"].blocked_by, ("build",))
        self.assertEqual(results["source"].blocked_by, ())
        self.assertEqual(results["report"].value, "diagnostics")
        self.assertEqual(results["source"].value, "diagnostics")

    async def test_message_free_failures_still_name_their_exception_type(self):
        async def fail():
            raise OSError

        results = await Scheduler(1, 128).execute([Task("build", fail)])
        self.assertEqual(results["build"].status, "failed")
        self.assertEqual(results["build"].error, "OSError: ")

    async def test_invalid_graphs_and_unsatisfiable_resources_rejected(self):
        async def nothing():
            return None

        for tasks in [
            [Task("a", nothing, requires=("missing",))],
            [Task("a", nothing, requires=("b",)), Task("b", nothing, requires=("a",))],
            [Task("a", nothing, memory_mb=200)],
            [Task("a", nothing), Task("a", nothing)],
        ]:
            with self.subTest(tasks=tasks), self.assertRaises(PreparationError):
                await Scheduler(1, 128).execute(tasks)

    async def test_cancellation_stops_active_work_and_keeps_queued_work_unstarted(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        queued_started = False

        async def long_running():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def queued():
            nonlocal queued_started
            queued_started = True

        execution = asyncio.create_task(
            Scheduler(1, 128).execute([Task("active", long_running), Task("queued", queued)])
        )
        await started.wait()
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertTrue(stopped.is_set())
        self.assertFalse(queued_started)


if __name__ == "__main__":
    unittest.main()
