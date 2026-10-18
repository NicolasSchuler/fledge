from __future__ import annotations

import asyncio
import json
import unittest

from latexprep.errors import PreparationError
from latexprep.resource_budget import ResourceBudget
from latexprep.scheduler import Scheduler, Task, TaskStatus


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
        concurrent = await Scheduler(ResourceBudget(3, 128)).execute(tasks)
        self.assertEqual(peak, 2)
        serial = await Scheduler(ResourceBudget(1, 128)).execute(tasks)
        self.assertEqual(
            [(name, result.value) for name, result in serial.items()],
            [(name, result.value) for name, result in concurrent.items()],
        )

    async def test_failure_blocks_artifact_consumers_but_diagnostics_run(self):
        async def fail():
            raise RuntimeError("build failed")

        async def succeed():
            return "diagnostics"

        results = await Scheduler(ResourceBudget(2, 128)).execute(
            [
                Task("build", fail),
                Task("pdf", succeed, needs_success_of=("build",)),
                Task("report", succeed, waits_for=("build", "pdf")),
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

    async def test_every_result_has_a_status_that_serializes_as_its_plain_string(self):
        async def fail():
            raise RuntimeError("build failed")

        async def succeed():
            return None

        results = await Scheduler(ResourceBudget(2, 128)).execute(
            [
                Task("build", fail),
                Task("pdf", succeed, needs_success_of=("build",)),
                Task("source", succeed),
            ]
        )
        expected = {
            "build": TaskStatus.FAILED,
            "pdf": TaskStatus.BLOCKED,
            "source": TaskStatus.SUCCEEDED,
        }
        for name, status in expected.items():
            with self.subTest(task=name):
                result = results[name]
                self.assertIs(result.status, status)
                self.assertEqual(result.status, status.value)
                self.assertEqual(result.succeeded, status is TaskStatus.SUCCEEDED)
                # Stage records and progress lines carry the bare string.
                self.assertEqual(json.dumps(result.status), json.dumps(status.value))
                self.assertEqual(f"{result.status}", status.value)
                self.assertEqual(str(result.status), status.value)
        self.assertEqual(
            [status.value for status in TaskStatus], ["succeeded", "failed", "blocked", "cancelled"]
        )

    async def test_cancelled_results_use_the_cancelled_status(self):
        started = asyncio.Event()
        results = []

        async def hang():
            started.set()
            await asyncio.Event().wait()

        async def never_started():
            raise AssertionError("must not start")

        scheduler = Scheduler(ResourceBudget(1, 128), on_result=results.append)
        execution = asyncio.create_task(
            scheduler.execute([Task("active", hang), Task("queued", never_started)])
        )
        await started.wait()
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertEqual(
            {item.name: item.status for item in results},
            {
                "active": TaskStatus.CANCELLED,
                "queued": TaskStatus.CANCELLED,
            },
        )
        self.assertTrue(all(not item.succeeded for item in results))

    async def test_composite_tasks_hold_no_reservation_and_need_no_capacity(self):
        async def nothing():
            return "done"

        composite = Task.composite("composite", nothing)
        self.assertTrue(composite.is_composite)
        self.assertEqual((composite.cpu, composite.memory_mb, composite.kind), (0, 0, None))
        self.assertFalse(Task("leased", nothing).is_composite)
        self.assertFalse(Task("render", nothing, kind="render").is_composite)
        dependent = Task.composite(
            "dependent", nothing, needs_success_of=("composite",), waits_for=("composite",)
        )
        self.assertEqual(dependent.dependencies, ("composite", "composite"))
        # A zero CPU request with a memory reservation is not composite, and is rejected.
        self.assertFalse(Task("odd", nothing, cpu=0).is_composite)
        with self.assertRaises(PreparationError):
            await Scheduler(ResourceBudget(1, 128)).execute([Task("odd", nothing, cpu=0)])
        budget = ResourceBudget(1, 128)
        results = await Scheduler(budget).execute([composite, dependent])
        self.assertEqual(
            {name: item.value for name, item in results.items()},
            {
                "composite": "done",
                "dependent": "done",
            },
        )
        self.assertEqual(budget.statistics()["operations"], [])

    async def test_message_free_failures_still_name_their_exception_type(self):
        async def fail():
            raise OSError

        results = await Scheduler(ResourceBudget(1, 128)).execute([Task("build", fail)])
        self.assertEqual(results["build"].status, "failed")
        self.assertEqual(results["build"].error, "OSError: ")

    async def test_a_defect_keeps_path_free_frames_but_expected_errors_do_not(self):
        async def defect():
            return {}["missing"]

        async def bad_input():
            raise PreparationError("The input cannot be read")

        async def unreadable():
            raise OSError("disk unavailable")

        results = await Scheduler(ResourceBudget(3, 192)).execute(
            [Task("defect", defect), Task("input", bad_input), Task("disk", unreadable)]
        )
        self.assertEqual(results["defect"].error, "KeyError: 'missing'")
        frames = results["defect"].defect_frames
        self.assertTrue(frames)
        self.assertIn("test_scheduler.py", frames[-1])
        self.assertTrue(all("/" not in frame and "\\" not in frame for frame in frames), frames)
        self.assertEqual(results["input"].defect_frames, ())
        self.assertEqual(results["disk"].defect_frames, ())

    async def test_invalid_graphs_and_unsatisfiable_resources_rejected(self):
        async def nothing():
            return None

        for tasks in [
            [Task("a", nothing, needs_success_of=("missing",))],
            [
                Task("a", nothing, needs_success_of=("b",)),
                Task("b", nothing, needs_success_of=("a",)),
            ],
            [Task("a", nothing, memory_mb=200)],
            [Task("a", nothing), Task("a", nothing)],
        ]:
            with self.subTest(tasks=tasks), self.assertRaises(PreparationError):
                await Scheduler(ResourceBudget(1, 128)).execute(tasks)

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
            Scheduler(ResourceBudget(1, 128)).execute(
                [Task("active", long_running), Task("queued", queued)]
            )
        )
        await started.wait()
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertTrue(stopped.is_set())
        self.assertFalse(queued_started)


if __name__ == "__main__":
    unittest.main()
