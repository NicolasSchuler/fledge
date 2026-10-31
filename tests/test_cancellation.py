"""Tests of the cancellation helpers: finishing work despite cancellation, stopping owned work."""

from __future__ import annotations

import asyncio
import threading
import unittest

from latexprep.cancellation import (
    AnalysisCancelled,
    cancel_and_drain,
    cancellation_point,
    complete_despite_cancellation,
    run_in_thread,
)


class CompleteDespiteCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Tests of ``complete_despite_cancellation``: results, errors, cleanup and existing tasks."""

    async def test_returns_the_result_when_nothing_cancels_the_caller(self) -> None:
        async def work() -> str:
            await asyncio.sleep(0)
            return "done"

        self.assertEqual(await complete_despite_cancellation(work()), "done")

    async def test_an_error_of_the_work_propagates_unchanged(self) -> None:
        async def work() -> None:
            raise OSError("cleanup failed")

        with self.assertRaisesRegex(OSError, "cleanup failed"):
            await complete_despite_cancellation(work())

    async def test_cleanup_finishes_before_repeated_cancellation_propagates(self) -> None:
        release = asyncio.Event()
        phases: list[str] = []

        async def cleanup() -> str:
            phases.append("started")
            await release.wait()
            phases.append("finished")
            return "reaped"

        caller = asyncio.create_task(complete_despite_cancellation(cleanup()))
        await asyncio.sleep(0)
        for _ in range(3):
            caller.cancel()
            await asyncio.sleep(0)
        # Repeated cancellation leaves the cleanup running and the caller waiting.
        self.assertFalse(caller.done())
        self.assertEqual(phases, ["started"])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(phases, ["started", "finished"])

    async def test_accepts_an_existing_task_and_leaves_it_finished(self) -> None:
        async def work() -> int:
            await asyncio.sleep(0)
            return 7

        task = asyncio.create_task(work())
        self.assertEqual(await complete_despite_cancellation(task), 7)
        self.assertTrue(task.done())


class OwnedWorkTests(unittest.IsolatedAsyncioTestCase):
    """Tests that ``cancel_and_drain`` and ``run_in_thread`` wait for owned work to stop."""

    async def test_cancel_and_drain_absorbs_repeated_cancellation_of_its_caller(self) -> None:
        release = asyncio.Event()
        stopped: list[str] = []

        async def child(name: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A child that needs time to stop keeps its owner waiting.
                await release.wait()
                stopped.append(name)
                raise

        children = [asyncio.create_task(child(name)) for name in ("a", "b")]
        await asyncio.sleep(0)
        owner = asyncio.create_task(cancel_and_drain(children))
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        self.assertFalse(owner.done())
        release.set()
        # The owner is already unwinding, so draining ends normally rather than re-raising.
        await owner
        self.assertCountEqual(stopped, ["a", "b"])
        self.assertTrue(all(item.cancelled() for item in children))

    async def test_a_cancelled_thread_worker_stops_before_the_cancellation_propagates(self) -> None:
        started = threading.Event()
        finished = threading.Event()

        def reader() -> None:
            started.set()
            while True:
                try:
                    cancellation_point()
                except AnalysisCancelled:
                    finished.set()
                    raise
                finished.wait(0.001)

        caller = asyncio.create_task(run_in_thread(reader))
        await asyncio.to_thread(started.wait, 5)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
