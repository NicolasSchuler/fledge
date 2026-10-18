"""The scripted command runner honours the contracts the adapters rely on."""

from __future__ import annotations

import asyncio
import unittest

from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError
from latexprep.resource_budget import ResourceBudget
from tests.fakes import FakeCommandRunner, Reply, queued
from tests.support import AsyncProjectTestCase


class ScriptedReplyTests(AsyncProjectTestCase):
    async def run_tool(self, runner: FakeCommandRunner, *argv: str, inputs=()) -> CommandResult:
        return await runner.run(list(argv), self.root, self.root, readonly_inputs=inputs)

    async def test_a_tool_answers_with_its_script_and_others_with_the_default(self) -> None:
        runner = FakeCommandRunner({"pdfinfo": "Pages: 2\n"}, default="other\n")
        self.assertEqual((await self.run_tool(runner, "pdfinfo", "x.pdf")).stdout, "Pages: 2\n")
        self.assertEqual((await self.run_tool(runner, "pdffonts", "x.pdf")).stdout, "other\n")

    async def test_a_function_script_sees_the_call_and_a_reply_can_write_files(self) -> None:
        def render(call):
            return Reply(f"rendered {call.argv[-1]}\n", files={"page.ppm": b"P6"})

        runner = FakeCommandRunner({"pdftoppm": render})
        result = await self.run_tool(runner, "pdftoppm", "-r", "72")
        self.assertEqual(result.stdout, "rendered 72\n")
        self.assertEqual((self.root / "page.ppm").read_bytes(), b"P6")

    async def test_queued_results_are_returned_in_order_and_failures_pass_through(self) -> None:
        failed = CommandResult(1, "", "bad", resolved_command=["tex-fmt"])
        runner = FakeCommandRunner(
            default=queued([failed, CommandResult(0, "ok", "", resolved_command=[])])
        )
        self.assertEqual((await self.run_tool(runner, "tex-fmt")).returncode, 1)
        self.assertEqual((await self.run_tool(runner, "tex-fmt")).stdout, "ok")

    async def test_every_call_is_logged_and_version_probes_are_told_apart(self) -> None:
        runner = FakeCommandRunner()
        await self.run_tool(runner, "pdfinfo", "-v")
        await self.run_tool(runner, "pdfinfo", "x.pdf")
        self.assertEqual([call.tool for call in runner.calls], ["pdfinfo", "pdfinfo"])
        self.assertEqual(runner.probes, ["pdfinfo"])
        self.assertEqual(runner.commands, [["pdfinfo", "x.pdf"]])


class FailureModeTests(AsyncProjectTestCase):
    async def test_a_missing_tool_raises_even_for_its_version_probe(self) -> None:
        runner = FakeCommandRunner()
        runner.missing = {"qpdf", "mutool"}
        for tool in ("qpdf", "mutool"):
            with self.subTest(tool=tool), self.assertRaisesRegex(PreparationError, tool):
                await runner.run([tool, "--version"], self.root, self.root)
        self.assertEqual((await runner.run(["pdfinfo", "x"], self.root, self.root)).returncode, 0)

    async def test_limit_failures_are_reported_by_commands_but_not_by_version_probes(self) -> None:
        for mode, attribute in (
            ("timeout", "timed_out"),
            ("output", "output_limited"),
            ("resource", "resource_exceeded"),
        ):
            runner = FakeCommandRunner()
            runner.failure = mode
            with self.subTest(mode=mode):
                result = await runner.run(["pdfinfo", "x"], self.root, self.root)
                self.assertTrue(getattr(result, attribute))
                self.assertFalse(result.within_limits)
                probe = await runner.run(["pdfinfo", "-v"], self.root, self.root)
                self.assertTrue(probe.within_limits)

    async def test_commands_wait_for_the_block_event_and_signal_that_they_started(self) -> None:
        runner = FakeCommandRunner()
        runner.block = asyncio.Event()
        task = asyncio.create_task(runner.run(["pdfinfo", "x"], self.root, self.root))
        await asyncio.wait_for(runner.started.wait(), 1)
        self.assertFalse(task.done())
        runner.block.set()
        self.assertEqual((await task).returncode, 0)

    async def test_a_command_outside_the_expected_budget_lease_fails_loudly(self) -> None:
        budget = ResourceBudget(2, 1024)
        runner = FakeCommandRunner(budget=budget)
        runner.expected_budget = budget
        with self.assertRaisesRegex(AssertionError, "lease"):
            await runner.run(["pdfinfo", "x"], self.root, self.root)
        async with budget.lease("test"):
            await runner.run(["pdfinfo", "x"], self.root, self.root)


class FrozenInputTests(AsyncProjectTestCase):
    async def test_inputs_are_copied_once_and_only_the_copies_may_be_granted(self) -> None:
        source = self.write("a.pdf", b"%PDF-a")
        runner = FakeCommandRunner()
        frozen = runner.freeze_input(source, self.root / "frozen")
        self.assertEqual(runner.freeze_input(source, self.root / "frozen"), frozen)
        self.assertNotEqual(frozen, source)
        self.assertEqual(frozen.read_bytes(), b"%PDF-a")
        await runner.run(["pdfinfo", str(frozen)], self.root, self.root, readonly_inputs=(frozen,))
        with self.assertRaisesRegex(PreparationError, "runtime-frozen"):
            await runner.run(
                ["pdfinfo", str(source)], self.root, self.root, readonly_inputs=(source,)
            )

    async def test_freezing_a_missing_file_is_a_preparation_failure(self) -> None:
        with self.assertRaises(PreparationError):
            FakeCommandRunner().freeze_input(self.root / "absent.pdf", self.root / "frozen")


class VersionProbeTests(AsyncProjectTestCase):
    async def test_concurrent_probes_of_one_tool_run_once_and_are_remembered(self) -> None:
        runner = FakeCommandRunner()
        versions = await asyncio.gather(
            *(runner.tool_version(["pdfinfo", "-v"], self.root) for _ in range(3))
        )
        self.assertEqual({item.version for item in versions}, {("pdfinfo version fixture",)})
        self.assertEqual(runner.probes, ["pdfinfo"])
        await runner.tool_version(["pdfinfo", "-v"], self.root)
        await runner.tool_version(["pdftotext", "-v"], self.root)
        self.assertEqual(runner.probes, ["pdfinfo", "pdftotext"])
        self.assertEqual(sorted(runner.tool_versions), ["pdfinfo", "pdftotext"])

    async def test_a_probe_that_prints_nothing_does_not_verify_the_tool(self) -> None:

        class SilentRunner(FakeCommandRunner):
            def version_output(self, call):
                return ""

        runner = SilentRunner()
        with self.assertRaisesRegex(PreparationError, "pdfinfo"):
            await runner.tool_version(["pdfinfo", "-v"], self.root)


if __name__ == "__main__":
    unittest.main()
