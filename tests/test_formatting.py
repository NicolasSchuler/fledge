from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.command_runner import CommandResult, CommandRunner, ToolVersion
from latexprep.config import Settings
from latexprep.file_safety import byte_count_text
from latexprep.findings import has_blockers
from latexprep.formatting import FormattingOptions, FormattingResult, format_project
from latexprep.runtime import ToolRunner


async def format_with(
    source: Path,
    work: Path,
    runner: CommandRunner,
    settings: Settings | None = None,
    **options,
) -> FormattingResult:
    """Format with the formatting settings of ``settings``, as the job core passes them."""
    settings = settings or Settings()
    return await format_project(
        source,
        work,
        runner,
        settings.formatting_options,
        **options,
    )


class FakeFormatter(ToolRunner):
    def __init__(self, results):
        super().__init__()
        self.results = iter(results)

    async def tool_version(self, argv, workspace) -> ToolVersion:
        return ToolVersion(("test formatter",), argv[0], ("--version",))

    async def run(self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None):
        if "--version" in argv:
            return CommandResult(0, "test formatter", "", resolved_command=argv)
        return next(self.results)


class FormattingTests(unittest.IsolatedAsyncioTestCase):
    async def proposal(self, source_text, output_text, **options):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "main.tex").write_text(source_text)
            output = CommandResult(0, output_text, "", resolved_command=[])
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(
                    source,
                    base / "work",
                    FakeFormatter([output, output]),
                    Settings(formatting_options=FormattingOptions(**options)),
                )
            self.assertEqual((source / "main.tex").read_text(), source_text)
            return result

    async def test_protected_regions(self):
        for source in (
            "\\begin{verbatim}\n  exact text\n\\end{verbatim}\n",
            "\\verb| exact  text |\n",
            "% tex-fmt: off\n exact  text\n% tex-fmt: on\n",
            "% Copyright Example\n% Permission statement\n",
        ):
            with self.subTest(source=source):
                unchanged = await self.proposal(source, source)
                self.assertEqual(
                    next(f for f in unchanged.findings if f.code == "FMT101").status, "passed"
                )
                changed = await self.proposal(
                    source, source.replace("exact", "changed").replace("Example", "Changed")
                )
                self.assertTrue(has_blockers(changed.findings))
                self.assertEqual(
                    next(f for f in changed.findings if f.code == "FMT101").status, "failed"
                )
                self.assertEqual(changed.contents, {})
        unclosed = await self.proposal("\\begin{verbatim}\nmissing end", "unused")
        self.assertEqual(
            next(f for f in unclosed.findings if f.code == "FMT101").status, "inconclusive"
        )

    async def test_project_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            (source / "generated").mkdir(parents=True)
            (source / "main.tex").write_text("original\n")
            (source / "generated/auto.tex").write_text("generated\n")
            output = CommandResult(0, "formatted\n", "", resolved_command=[])
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                settings = Settings(formatting_options=FormattingOptions(exclude=("generated/*",)))
                result = await format_with(
                    source, base / "work", FakeFormatter([output, output]), settings
                )
                unmatched = await format_with(
                    source,
                    base / "other",
                    FakeFormatter([output] * 4),
                    Settings(formatting_options=FormattingOptions(exclude=("absent/*",))),
                )
            self.assertEqual(set(result.contents), {"main.tex"})
            self.assertEqual(
                next(f for f in result.findings if f.code == "FMT102").status, "passed"
            )
            self.assertEqual(
                next(f for f in unmatched.findings if f.code == "FMT102").status, "failed"
            )
            self.assertEqual((source / "generated/auto.tex").read_text(), "generated\n")
            (source / "linked.tex").symlink_to(source / "main.tex")
            linked = await format_with(source, base / "linked", FakeFormatter([]), Settings())
            self.assertEqual(
                next(f for f in linked.findings if f.code == "FMT102").status, "inconclusive"
            )
            self.assertEqual(linked.contents, {})

    async def test_source_files_are_recognised_by_extension_in_any_letter_case(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "MAIN.TEX").write_text("original\n")
            (source / "notes.txt").write_text("not TeX\n")
            output = CommandResult(0, "formatted\n", "", resolved_command=[])
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(source, base / "work", FakeFormatter([output, output]))
            self.assertEqual(set(result.contents), {"MAIN.TEX"})

    async def test_blank_line_policy(self):
        original = "a\n\n\nb\n"
        preserved = await self.proposal(original, original, blank_lines="preserve")
        self.assertEqual(next(f for f in preserved.findings if f.code == "FMT103").status, "passed")
        changed = await self.proposal(original, "a\n\nb\n", blank_lines="preserve")
        self.assertEqual(next(f for f in changed.findings if f.code == "FMT103").status, "failed")
        self.assertEqual(changed.contents, {})
        collapsed = await self.proposal(original, original, blank_lines="collapse")
        self.assertEqual(collapsed.contents, {"main.tex": b"a\n\nb\n"})
        literal = "\\begin{verbatim}\na\n\n\nb\n\\end{verbatim}\n"
        protected = await self.proposal(literal, literal, blank_lines="collapse")
        self.assertEqual(protected.contents, {})
        unresolved = await self.proposal("% tex-fmt: off\n", "unused", blank_lines="preserve")
        self.assertEqual(
            next(f for f in unresolved.findings if f.code == "FMT103").status, "inconclusive"
        )

    async def test_resource_and_output_limits_block_even_with_zero_return_code(self):
        for limit in ("output", "memory"):
            for pass_number in (1, 2):
                with self.subTest(limit=limit, pass_number=pass_number):
                    with tempfile.TemporaryDirectory() as directory:
                        base = Path(directory)
                        source = base / "source"
                        source.mkdir()
                        original = b"\\begin{document}text\\end{document}\n"
                        (source / "main.tex").write_bytes(original)
                        good = CommandResult(0, "\\endinput\n", "", resolved_command=[])
                        bad = CommandResult(
                            0,
                            "\\endinput\n",
                            "",
                            resolved_command=[],
                            output_limited=limit == "output",
                            resource_exceeded="memory" if limit == "memory" else None,
                        )
                        runner = FakeFormatter([bad] if pass_number == 1 else [good, bad])
                        with patch(
                            "latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"
                        ):
                            result = await format_with(source, base / "work", runner, Settings())
                        self.assertTrue(has_blockers(result.findings))
                        self.assertEqual(result.contents, {})
                        self.assertEqual(result.originals, {})
                        self.assertEqual(result.changes, [])
                        self.assertEqual((source / "main.tex").read_bytes(), original)

    async def test_formatter_failures_quote_a_short_pathless_message(self):
        stderr = (
            "error: cannot read /private/var/folders/ab/work/files/main.tex: boom\n"
            "second line\n" + "x" * 5000
        )
        for stderr_text, expected in (
            (stderr, "tex-fmt failed with exit status 2: error: cannot read <path>: boom"),
            ("", "tex-fmt failed with exit status 2"),
        ):
            with self.subTest(stderr=stderr_text[:20]), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / "source").mkdir()
                (base / "source" / "main.tex").write_text("text\n")
                failed = CommandResult(2, "", stderr_text, resolved_command=[])
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    result = await format_with(
                        base / "source", base / "work", FakeFormatter([failed]), Settings()
                    )
                (finding,) = [f for f in result.findings if f.rule == "source.format_execution"]
                self.assertEqual(finding.message, expected)
                self.assertNotIn("second line", finding.message)
                self.assertNotIn(str(base), finding.message)

    async def test_a_formatter_stopped_by_a_limit_names_the_limit_it_exceeded(self):
        limits = FakeFormatter([]).limits
        for stopped, expected in (
            (
                CommandResult(-9, "", "", resolved_command=[], timed_out=True),
                f"tex-fmt was stopped after {limits.timeout_seconds} s because it ran too long",
            ),
            (
                CommandResult(0, "", "", resolved_command=[], output_limited=True),
                "tex-fmt was stopped after printing more than "
                f"{byte_count_text(limits.max_output_bytes)} of output",
            ),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / "source").mkdir()
                (base / "source" / "main.tex").write_text("text\n")
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    result = await format_with(
                        base / "source", base / "work", FakeFormatter([stopped]), Settings()
                    )
                (finding,) = [f for f in result.findings if f.rule == "source.format_execution"]
                self.assertEqual(finding.message, expected)

    async def test_a_failed_second_pass_is_named_with_its_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "source").mkdir()
            (base / "source" / "main.tex").write_text("text\n")
            first = CommandResult(0, "text\n", "", resolved_command=[])
            second = CommandResult(3, "", "", resolved_command=[])
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(
                    base / "source", base / "work", FakeFormatter([first, second]), Settings()
                )
        (finding,) = [f for f in result.findings if f.rule == "source.format_execution"]
        self.assertEqual(finding.message, "tex-fmt's second pass failed with exit status 3")
        self.assertEqual((result.contents, result.changes), ({}, []))

    async def test_operating_system_errors_are_reported_by_cause_without_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "source").mkdir()
            (base / "source" / "main.tex").write_text("text\n")
            denied = PermissionError(13, "Permission denied", str(base / "work/files/main.tex"))
            with (
                patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"),
                patch("latexprep.formatting._format_file", side_effect=denied),
            ):
                result = await format_with(
                    base / "source", base / "work", FakeFormatter([]), Settings()
                )
            (finding,) = [f for f in result.findings if f.rule == "source.format_execution"]
            self.assertEqual(finding.path, "main.tex")
            self.assertIn("permission denied", finding.message)
            self.assertNotIn(str(base), finding.message)

    async def test_only_idempotent_output_is_proposed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "main.tex").write_text("a\n")
            first = CommandResult(0, "b\n", "", resolved_command=[])
            second = CommandResult(0, "c\n", "", resolved_command=[])
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(
                    source, base / "work", FakeFormatter([first, second]), Settings()
                )
            self.assertTrue(has_blockers(result.findings))
            self.assertEqual(result.contents, {})
            finding = next(item for item in result.findings if item.code == "FMT002")
            self.assertEqual(finding.status, "failed")
            self.assertEqual(finding.severity, "error")
            self.assertEqual(finding.path, "main.tex")
            self.assertEqual((source / "main.tex").read_text(), "a\n")

    async def test_formatting_differences_preserve_original(self):
        for formatted_text, status in (("b\n", "failed"), ("a\n", "passed")):
            with self.subTest(formatted_text=formatted_text):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    source = base / "source"
                    source.mkdir()
                    (source / "main.tex").write_text("a\n")
                    output = CommandResult(0, formatted_text, "", resolved_command=[])
                    with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                        result = await format_with(
                            source, base / "work", FakeFormatter([output, output]), Settings()
                        )
                    finding = next(item for item in result.findings if item.code == "FMT001")
                    self.assertEqual(finding.status, status)
                    self.assertEqual(finding.severity, "warning" if status == "failed" else "info")
                    self.assertFalse(has_blockers(result.findings))
                    self.assertEqual((source / "main.tex").read_text(), "a\n")
                    self.assertNotIn("FMT002", {item.code for item in result.findings})
                    if status == "failed":
                        self.assertEqual(finding.path, "main.tex")
                        self.assertEqual(result.contents, {"main.tex": b"b\n"})
                        self.assertEqual(result.originals, {"main.tex": b"a\n"})
                        self.assertIn("-a\n+b\n", result.changes[0].diff or "")
                    else:
                        self.assertEqual(result.contents, {})


if __name__ == "__main__":
    unittest.main()
