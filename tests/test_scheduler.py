"""Scheduling task graphs, and the findings that report blocked and failed tasks."""

from __future__ import annotations

import asyncio
import json
import unittest

from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.resource_budget import ResourceBudget
from latexprep.scheduler import Scheduler, Task, TaskResult, TaskStatus
from latexprep.task_findings import (
    BLOCKED_BY_DETAIL,
    TASK_DETAIL,
    TASK_RULE,
    TRACEBACK_DETAIL,
    blocked_prerequisites,
    blocked_task_finding,
    failed_task_finding,
)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """Running task graphs with :class:`Scheduler`.

    Results are deterministic under bounded overlap; a failure blocks the tasks that need its
    artifacts while diagnostics still run; composite tasks hold no reservation; every result has a
    status that serializes as a plain string; failures name their exception type and only defects
    keep their frames; cycles, invalid graphs and unsatisfiable resources are rejected; and
    cancellation stops active work without starting queued work.
    """

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

    async def test_a_cycle_is_rejected_with_the_tasks_that_form_it(self):
        async def nothing():
            return None

        with self.assertRaisesRegex(PreparationError, r"Cyclic task dependency: \w -> \w -> \w"):
            await Scheduler(ResourceBudget(1, 128)).execute(
                [
                    Task("a", nothing, needs_success_of=("b",)),
                    Task("b", nothing, needs_success_of=("a",)),
                ]
            )

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


class TaskFindingTests(unittest.TestCase):
    """Findings for skipped and failed tasks: the prerequisites they name and a defect's frames."""

    def test_skipped_task_names_its_failed_prerequisites_for_report_renderers(self):
        result = TaskResult(
            "pdf", TaskStatus.BLOCKED, error="Prerequisite failed: build", blocked_by=("build",)
        )
        finding = blocked_task_finding(result)
        self.assertEqual(
            (finding.rule, finding.status, finding.severity), (TASK_RULE, "skipped", "info")
        )
        self.assertEqual(finding.details[TASK_DETAIL], "pdf")
        self.assertEqual(finding.details[BLOCKED_BY_DETAIL], ["build"])
        self.assertEqual(blocked_prerequisites(finding), ("build",))

    def test_only_a_skipped_task_finding_lists_prerequisites(self):
        failed = failed_task_finding(TaskResult("pdf", TaskStatus.FAILED, error="OSError: gone"))
        other = Finding("source.edit_marker", "TODO", details={BLOCKED_BY_DETAIL: ["build"]})
        self.assertEqual(blocked_prerequisites(failed), ())
        self.assertEqual(blocked_prerequisites(other), ())

    def test_a_failed_task_always_names_itself_and_a_defect_adds_its_frames(self):
        expected = failed_task_finding(TaskResult("pdf", TaskStatus.FAILED, error="OSError: gone"))
        self.assertEqual(expected.details, {TASK_DETAIL: "pdf"})
        self.assertIsNone(expected.suggestion)
        defect = failed_task_finding(
            TaskResult(
                "pdf", TaskStatus.FAILED, error="KeyError: 'x'", defect_frames=("scan.py:3 in run",)
            )
        )
        self.assertEqual(defect.details[TRACEBACK_DETAIL], ["scan.py:3 in run"])
        self.assertIn("defect in Fledge", defect.suggestion or "")


if __name__ == "__main__":
    unittest.main()
