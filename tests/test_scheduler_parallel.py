from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from latexprep.models import Finding, PreparationError
from latexprep.runtime import BuildResult
from latexprep.scheduler import (
    ResourceBudget,
    Scheduler,
    Task,
    bounded_map,
    cancellation_point,
    run_in_thread,
)


class SharedBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_cancelled_leaf_still_records_every_final_task_state_once(self):
        independent_started = asyncio.Event()
        collected = []
        budget = ResourceBudget(2, 128)

        async def cancelled():
            await independent_started.wait()
            raise asyncio.CancelledError

        async def independent():
            independent_started.set()
            await asyncio.Event().wait()

        async def forbidden():
            self.fail("A cancelled prerequisite must not start its dependent")

        with self.assertRaises(asyncio.CancelledError):
            await Scheduler(2, 128, on_result=collected.append, budget=budget).execute(
                [
                    Task("producer", cancelled),
                    Task("consumer", forbidden, requires=("producer",)),
                    Task("independent", independent),
                ]
            )
        self.assertEqual(
            sorted(item.name for item in collected), ["consumer", "independent", "producer"]
        )
        self.assertTrue(all(item.status == "cancelled" for item in collected))
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["queued"], 0)

    async def wait_for_events(self, *events):
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), 2)

    async def checkpoint(self):
        resumed = asyncio.Event()
        asyncio.get_running_loop().call_soon(resumed.set)
        await resumed.wait()

    async def drain(self, *tasks):
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_each_resource_cap_blocks_atomically_without_partial_reservations(self):
        cases: list[tuple[str, dict[str, int], dict[str, Any]]] = [
            ("CPU", {"jobs": 1, "memory_mb": 512}, {"cpu": 1, "memory_mb": 64}),
            (
                "memory",
                {"jobs": 4, "memory_mb": 256, "parent_memory_mb": 64},
                {"cpu": 1, "memory_mb": 128},
            ),
            (
                "builds",
                {"jobs": 4, "memory_mb": 512, "build_jobs": 1},
                {"cpu": 1, "memory_mb": 64, "kind": "build"},
            ),
            (
                "renders",
                {"jobs": 4, "memory_mb": 512, "render_jobs": 1},
                {"cpu": 1, "memory_mb": 64, "kind": "render"},
            ),
        ]

        async def hold(budget, request, name, entered, release, requested=None):
            if requested is not None:
                requested.set()
            async with budget.lease(name, **request):
                entered.set()
                await release.wait()

        for name, settings, request in cases:
            with self.subTest(resource=name):
                budget = ResourceBudget(**settings)
                first_entered, second_requested, second_entered = (
                    asyncio.Event() for _ in range(3)
                )
                first_release, second_release = asyncio.Event(), asyncio.Event()

                first_task = asyncio.create_task(
                    hold(budget, request, "first", first_entered, first_release)
                )
                second_task = None
                try:
                    await self.wait_for_events(first_entered)
                    second_task = asyncio.create_task(
                        hold(
                            budget,
                            request,
                            "second",
                            second_entered,
                            second_release,
                            second_requested,
                        )
                    )
                    await self.wait_for_events(second_requested)
                    self.assertFalse(second_entered.is_set())
                    statistics = budget.statistics()
                    self.assertEqual(statistics["active_cpu"], request["cpu"])
                    self.assertEqual(statistics["active_memory_mb"], request["memory_mb"])
                    self.assertEqual(statistics["queued"], 1)
                    first_release.set()
                    await self.wait_for_events(second_entered)
                    self.assertEqual(budget.statistics()["queued"], 0)
                    second_release.set()
                    await asyncio.wait_for(asyncio.gather(first_task, second_task), 2)
                finally:
                    first_release.set()
                    second_release.set()
                    await self.drain(*([first_task, second_task] if second_task else [first_task]))
                statistics = budget.statistics()
                self.assertEqual(statistics["active_cpu"], 0)
                self.assertEqual(statistics["active_memory_mb"], 0)
                self.assertEqual(statistics["peak_reserved_cpu"], request["cpu"])
                self.assertEqual(
                    statistics["peak_reserved_memory_mb"],
                    request["memory_mb"] + settings.get("parent_memory_mb", 0),
                )

    async def test_build_and_render_overlap_under_one_combined_budget(self):
        budget = ResourceBudget(2, 320, build_jobs=1, render_jobs=1, parent_memory_mb=64)
        entered = {kind: asyncio.Event() for kind in ("build", "render")}
        release = asyncio.Event()

        async def worker(kind):
            async with budget.lease(kind, cpu=1, memory_mb=128, kind=kind):
                entered[kind].set()
                await release.wait()

        tasks = [asyncio.create_task(worker(kind)) for kind in entered]
        try:
            await self.wait_for_events(*entered.values())
            statistics = budget.statistics()
            self.assertEqual(statistics["active_cpu"], 2)
            self.assertEqual(statistics["active_memory_mb"], 256)
            self.assertEqual(statistics["peak_builds"], 1)
            self.assertEqual(statistics["peak_renders"], 1)
            self.assertEqual(statistics["peak_reserved_memory_mb"], 320)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        finally:
            release.set()
            await self.drain(*tasks)
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)

    async def test_unsatisfiable_reservations_reject_before_work_or_queueing(self):
        requests: list[dict[str, Any]] = [
            {"cpu": 3, "memory_mb": 64},
            {"cpu": 1, "memory_mb": 193},
            {"cpu": 0, "memory_mb": 64},
            {"cpu": 1, "memory_mb": 0},
            {"cpu": True, "memory_mb": 64},
            {"cpu": 1, "memory_mb": 64, "kind": "unknown"},
        ]
        budget = ResourceBudget(2, 256, parent_memory_mb=64)
        for request in requests:
            with self.subTest(request=request), self.assertRaises(PreparationError):
                async with budget.lease("invalid", **request):
                    self.fail("An invalid reservation started work")
            self.assertEqual(budget.statistics()["active_cpu"], 0)
            self.assertEqual(budget.statistics()["queued"], 0)
        self.assertEqual(budget.statistics()["operations"], [])

    async def test_nested_leases_reject_and_inherited_context_does_not_grant_ownership(self):
        budget = ResourceBudget(1, 64)
        stale_child_ready, stale_child_release = asyncio.Event(), asyncio.Event()

        async def child_while_parent_owns():
            self.assertIsNone(budget.current_lease)
            with self.assertRaisesRegex(PreparationError, "nested leases"):
                async with budget.lease("nested child"):
                    self.fail("A child reused its parent's capacity")

        async def child_after_parent_releases():
            stale_child_ready.set()
            await stale_child_release.wait()
            self.assertIsNone(budget.current_lease)
            async with budget.lease("later child") as owned:
                self.assertIs(budget.current_lease, owned)
                self.assertIs(owned.owner, asyncio.current_task())
                return "ran after release"

        stale_child = None
        try:
            async with budget.lease("parent") as parent:
                self.assertIs(budget.current_lease, parent)
                self.assertIs(parent.owner, asyncio.current_task())
                with self.assertRaisesRegex(PreparationError, "nested leases"):
                    async with budget.lease("nested"):
                        self.fail("A nested lease deadlocked or consumed extra capacity")
                await asyncio.wait_for(asyncio.create_task(child_while_parent_owns()), 2)
                stale_child = asyncio.create_task(child_after_parent_releases())
                await self.wait_for_events(stale_child_ready)
                self.assertEqual(budget.statistics()["active_cpu"], 1)
            self.assertFalse(parent.active)
            self.assertIsNone(budget.current_lease)
            stale_child_release.set()
            self.assertEqual(await asyncio.wait_for(stale_child, 2), "ran after release")
        finally:
            stale_child_release.set()
            if stale_child is not None:
                await self.drain(stale_child)
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["peak_reserved_cpu"], 1)

    async def test_cancelled_waiter_releases_queue_position_without_affecting_owner(self):
        budget = ResourceBudget(1, 64)
        entered, release, requested = (asyncio.Event() for _ in range(3))

        async def owner():
            async with budget.lease("owner"):
                entered.set()
                await release.wait()

        async def waiting():
            requested.set()
            async with budget.lease("waiting"):
                self.fail("A queued cancelled operation started")

        owner_task = asyncio.create_task(owner())
        waiting_task = None
        try:
            await self.wait_for_events(entered)
            waiting_task = asyncio.create_task(waiting())
            await self.wait_for_events(requested)
            self.assertEqual(budget.statistics()["queued"], 1)
            waiting_task.cancel()
            waiting_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiting_task, 2)
            self.assertEqual(budget.statistics()["queued"], 0)
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            self.assertEqual(budget.statistics()["active_memory_mb"], 64)
            release.set()
            await asyncio.wait_for(owner_task, 2)
        finally:
            release.set()
            await self.drain(*([owner_task, waiting_task] if waiting_task else [owner_task]))
        self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_bounded_workers_complete_out_of_order_but_results_keep_input_order(self):
        entered = [asyncio.Event() for _ in range(20)]
        first_release, second_release = asyncio.Event(), asyncio.Event()
        owners = set()
        completed = []
        active = 0
        peak = 0

        async def worker(index):
            nonlocal active, peak
            owners.add(asyncio.current_task())
            active += 1
            peak = max(peak, active)
            try:
                entered[index].set()
                if index == 0:
                    await first_release.wait()
                elif index == 1:
                    await second_release.wait()
                completed.append(index)
                return f"result {index}"
            finally:
                active -= 1

        execution = asyncio.create_task(bounded_map(list(range(20)), worker, limit=2))
        try:
            await self.wait_for_events(entered[0], entered[1])
            self.assertFalse(any(event.is_set() for event in entered[2:]))
            self.assertEqual(active, 2)
            second_release.set()
            await self.wait_for_events(entered[-1])
            self.assertFalse(execution.done())
            first_release.set()
            result = await asyncio.wait_for(execution, 2)
        finally:
            first_release.set()
            second_release.set()
            await self.drain(execution)
        self.assertEqual(result, [f"result {index}" for index in range(20)])
        self.assertEqual(completed, [*range(1, 20), 0])
        self.assertEqual(peak, 2)
        self.assertEqual(len(owners), 2)
        self.assertEqual(active, 0)

    async def test_bounded_worker_failure_drains_sibling_before_propagating(self):
        loop = asyncio.get_running_loop()
        contexts = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        self.addCleanup(loop.set_exception_handler, original_handler)
        sibling_entered, cleanup_started, cleanup_release = (asyncio.Event() for _ in range(3))
        cleaned = asyncio.Event()
        started = []

        async def worker(index):
            started.append(index)
            if index == 0:
                await sibling_entered.wait()
                raise RuntimeError("required worker failed")
            sibling_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()
                raise

        execution = asyncio.create_task(bounded_map(list(range(100)), worker, limit=2))
        try:
            await self.wait_for_events(cleanup_started)
            self.assertFalse(execution.done())
            self.assertFalse(cleaned.is_set())
            self.assertEqual(started, [0, 1])
            cleanup_release.set()
            with self.assertRaisesRegex(RuntimeError, "required worker failed"):
                await asyncio.wait_for(execution, 2)
        finally:
            cleanup_release.set()
            await self.drain(execution)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(started, [0, 1])
        await self.checkpoint()
        self.assertEqual(contexts, [])

    async def test_bounded_repeated_cancellation_drains_without_late_diagnostics(self):
        loop = asyncio.get_running_loop()
        contexts = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        self.addCleanup(loop.set_exception_handler, original_handler)
        budget = ResourceBudget(2, 128)
        entered = [asyncio.Event(), asyncio.Event()]
        cleaning = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        owners = []
        cleaned = []

        async def worker(index):
            self.assertLess(index, 2, "Cancellation must stop admission of queued work")
            owners.append(asyncio.current_task())
            async with budget.lease(f"worker {index}"):
                entered[index].set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning[index].set()
                    await release.wait()
                    cleaned.append(index)
                    raise

        execution = asyncio.create_task(bounded_map(list(range(20)), worker, limit=2))
        try:
            await self.wait_for_events(*entered)
            execution.cancel()
            await self.wait_for_events(*cleaning)
            execution.cancel()
            await self.checkpoint()
            self.assertFalse(execution.done())
            self.assertEqual(budget.statistics()["active_cpu"], 2)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 2)
        finally:
            release.set()
            await self.drain(execution)
        await self.checkpoint()
        self.assertEqual(contexts, [])
        self.assertEqual(sorted(cleaned), [0, 1])
        self.assertTrue(all(owner.done() for owner in owners))
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)
        self.assertEqual(budget.statistics()["queued"], 0)

    async def test_composite_holds_no_capacity_while_its_leaves_are_serial_at_one_job(self):
        budget = ResourceBudget(1, 64)
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        observations = []

        async def leaf(index):
            async with budget.lease(f"leaf {index}"):
                entered[index].set()
                await release[index].wait()
                observations.append(index)
                return index

        async def composite():
            self.assertIsNone(budget.current_lease)
            return await bounded_map([0, 1], leaf, limit=2)

        async def dependent():
            self.assertIsNotNone(budget.current_lease)
            return "read complete result"

        execution = asyncio.create_task(
            Scheduler(1, 64, budget=budget).execute(
                [
                    Task("composite", composite, cpu=0, memory_mb=0),
                    Task("dependent", dependent, requires=("composite",)),
                ]
            )
        )
        try:
            await self.wait_for_events(entered[0])
            self.assertFalse(entered[1].is_set())
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            release[0].set()
            await self.wait_for_events(entered[1])
            self.assertEqual(observations, [0])
            release[1].set()
            results = await asyncio.wait_for(execution, 2)
        finally:
            for event in release:
                event.set()
            await self.drain(execution)
        self.assertEqual(results["composite"].value, [0, 1])
        self.assertEqual(results["dependent"].value, "read complete result")
        self.assertEqual(budget.statistics()["peak_reserved_cpu"], 1)
        self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_false_artifact_outcome_retains_diagnostics_and_only_blocks_requires(self):
        failed_build = BuildResult(
            success=False,
            findings=[Finding("build.failed", "The build produced no valid PDF", "error")],
        )
        consumer_called, report_called = asyncio.Event(), asyncio.Event()
        callbacks = []

        async def build():
            return failed_build

        async def consume():
            consumer_called.set()

        async def report():
            report_called.set()
            return "diagnostics assembled"

        results = await Scheduler(2, 128, on_result=callbacks.append).execute(
            [
                Task("report", report, after=("build", "pdf")),
                Task("pdf", consume, requires=("build",)),
                Task("build", build, is_success=lambda value: value.success),
            ]
        )
        self.assertEqual(list(results), ["build", "pdf", "report"])
        self.assertEqual(results["build"].status, "failed")
        self.assertIs(results["build"].value, failed_build)
        self.assertEqual(
            results["build"].value.findings[0].message, failed_build.findings[0].message
        )
        self.assertEqual(results["pdf"].status, "blocked")
        self.assertFalse(consumer_called.is_set())
        self.assertTrue(report_called.is_set())
        self.assertEqual(results["report"].status, "succeeded")
        self.assertEqual(results["report"].value, "diagnostics assembled")
        self.assertEqual(sorted(item.name for item in callbacks), list(results))

    async def test_scheduler_prevalidates_all_tasks_before_starting_any_work(self):
        called = asyncio.Event()

        async def worker():
            called.set()

        budget = ResourceBudget(2, 256, parent_memory_mb=64)
        with self.assertRaises(PreparationError):
            await Scheduler(2, 256, budget=budget).execute(
                [Task("fits", worker), Task("too large", worker, memory_mb=193)]
            )
        self.assertFalse(called.is_set())
        self.assertEqual(budget.statistics()["operations"], [])

    async def test_repeated_scheduler_cancellation_drains_before_releasing_capacity(self):
        budget = ResourceBudget(1, 64)
        entered, cleanup_started, cleanup_release, cleaned = (asyncio.Event() for _ in range(4))
        queued_started = asyncio.Event()
        callbacks = []

        async def active():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()
                raise

        async def queued():
            queued_started.set()

        execution = asyncio.create_task(
            Scheduler(1, 64, budget=budget, on_result=callbacks.append).execute(
                [
                    Task("active", active),
                    Task("queued", queued),
                    Task("dependent", queued, requires=("active",)),
                ]
            )
        )
        try:
            await self.wait_for_events(entered)
            self.assertEqual(budget.statistics()["queued"], 1)
            execution.cancel()
            await self.wait_for_events(cleanup_started)
            execution.cancel()
            await self.checkpoint()
            self.assertFalse(execution.done())
            self.assertFalse(cleaned.is_set())
            self.assertFalse(queued_started.is_set())
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 2)
        finally:
            cleanup_release.set()
            await self.drain(execution)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)
        self.assertEqual(budget.statistics()["queued"], 0)
        self.assertEqual(
            sorted((item.name, item.status) for item in callbacks),
            [("active", "cancelled"), ("dependent", "cancelled"), ("queued", "cancelled")],
        )

    async def test_cancelled_thread_keeps_lease_and_snapshot_until_reader_exits(self):
        budget = ResourceBudget(1, 64)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        read_gate = threading.Event()
        exited = threading.Event()
        observed_cancellation = threading.Event()
        paths: dict[str, Path] = {}
        observed_contents = []

        def reader(path):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not read_gate.wait(5):
                    raise RuntimeError("Reader test gate was not released")
                observed_contents.append(path.read_text())
                try:
                    cancellation_point()
                except PreparationError:
                    observed_cancellation.set()
                    raise
            finally:
                exited.set()

        async def owned_read():
            with tempfile.TemporaryDirectory() as directory:
                snapshot = Path(directory) / "source.tex"
                snapshot.write_text("frozen source")
                paths["snapshot"] = snapshot
                async with budget.lease("source reader"):
                    return await run_in_thread(reader, snapshot)

        execution = asyncio.create_task(owned_read())
        try:
            await self.wait_for_events(entered)
            execution.cancel()
            await self.checkpoint()
            execution.cancel()
            await self.checkpoint()
            self.assertFalse(execution.done())
            self.assertFalse(exited.is_set())
            self.assertTrue(paths["snapshot"].is_file())
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            read_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 2)
        finally:
            read_gate.set()
            await self.drain(execution)
        self.assertTrue(exited.is_set())
        self.assertTrue(observed_cancellation.is_set())
        self.assertEqual(observed_contents, ["frozen source"])
        self.assertFalse(paths["snapshot"].exists())
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)

    async def test_thread_success_and_failure_keep_their_original_outcomes(self):
        self.assertEqual(await run_in_thread(lambda value: value + 1, 4), 5)

        def fail():
            raise ValueError("invalid source")

        with self.assertRaisesRegex(ValueError, "invalid source"):
            await run_in_thread(fail)

    async def test_cooperative_thread_cancellation_does_not_log_late_worker_exception(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        contexts = []
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        def analyze():
            loop.call_soon_threadsafe(started.set)
            while True:
                cancellation_point()

        task = asyncio.create_task(run_in_thread(analyze))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertEqual(contexts, [])
        finally:
            loop.set_exception_handler(original_handler)


if __name__ == "__main__":
    unittest.main()
