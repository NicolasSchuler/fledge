"""Choosing the TeX engine, and reporting a project latexmkrc that latexmk never executes.

The engine setting wins over the main file's ``% !TEX program`` comment, which wins over
pdfLaTeX; a build records what chose its engine; a comment that names a program Fledge cannot
run is a warning; and a latexmkrc is read as text to say which of its choices do not apply.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.engine_selection import (
    EngineChoice,
    EngineComment,
    LatexmkConfiguration,
    choose_engine,
    engine_finding,
    find_latexmk_configurations,
    latexmk_configuration_finding,
    read_engine_comment,
)
from latexprep.latexmk_build import build_project
from tests.fakes import FakeBuildRunner


class EngineCommentTests(unittest.TestCase):
    """The ``% !TEX program`` comment as TeXShop, TeXstudio, TeXworks and VS Code write it."""

    def setUp(self) -> None:
        """Create a temporary directory for the main files."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def comment(self, text: str) -> EngineComment | None:
        """Write ``text`` as the main file and read its engine comment."""
        main = self.root / "main.tex"
        main.write_text(text, encoding="utf-8")
        return read_engine_comment(main)

    def test_spellings_are_recognised_case_insensitively(self) -> None:
        for line, engine in (
            ("% !TEX program = xelatex", "xelatex"),
            ("%!TeX program=LuaLaTeX", "lualatex"),
            ("  % !TEX TS-program = pdflatex", "pdflatex"),
            ("% !TEX TS-program = xelatexmk", "xelatex"),
        ):
            with self.subTest(line=line):
                comment = self.comment(f"{line}\n\\documentclass{{article}}\n")
                self.assertIsNotNone(comment)
                assert comment is not None
                self.assertEqual((comment.engine, comment.line), (engine, 1))

    def test_other_programs_and_late_or_absent_comments(self) -> None:
        comment = self.comment("% !TEX program = uplatex\n")
        self.assertEqual((comment.program, comment.engine) if comment else None, ("uplatex", None))
        late = "\n" * 25 + "% !TEX program = xelatex\n"
        self.assertIsNone(self.comment(late))
        self.assertIsNone(self.comment("\\documentclass{article}\n% TEX program = xelatex\n"))
        self.assertIsNone(read_engine_comment(self.root / "missing.tex"))


class EngineChoiceTests(unittest.TestCase):
    """The engine setting, then the magic comment, then pdfLaTeX, and the finding that says so."""

    def test_priority_setting_comment_default(self) -> None:
        xelatex = EngineComment("xelatex", 1, "xelatex")
        self.assertEqual(choose_engine("lualatex", xelatex).engine, "lualatex")
        self.assertEqual(
            choose_engine("auto", xelatex), EngineChoice("xelatex", "magic comment", xelatex)
        )
        self.assertEqual(choose_engine("auto", None), EngineChoice("pdflatex", "default", None))
        setting = engine_finding(choose_engine("pdflatex", xelatex), "main.tex")
        self.assertEqual(
            (setting.code, setting.severity, setting.status), ("BLD106", "info", "passed")
        )
        self.assertIn(
            "takes precedence over the `% !TEX program = xelatex` comment", setting.message
        )
        comment = engine_finding(choose_engine("auto", xelatex), "main.tex")
        self.assertIn("XeLaTeX builds the document, as the `% !TEX program", comment.message)
        self.assertEqual((comment.line, comment.details["source"]), (1, "magic comment"))
        default = engine_finding(choose_engine("auto", None), "main.tex")
        self.assertEqual(default.details["source"], "default")
        self.assertIn("pdfLaTeX builds the document", default.message)

    def test_unusable_comment_is_a_warning(self) -> None:
        unusable = EngineComment("uplatex", 2, None)
        finding = engine_finding(choose_engine("auto", unusable), "main.tex")
        self.assertEqual((finding.code, finding.severity, finding.line), ("BLD106", "warning", 2))
        self.assertIn("cannot run", finding.message)
        overridden = engine_finding(choose_engine("xelatex", unusable), "main.tex")
        self.assertEqual(overridden.severity, "info")


class LatexmkConfigurationTests(unittest.TestCase):
    """A project's latexmkrc is read as text and never executed."""

    def setUp(self) -> None:
        """Create a temporary project folder."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_engine_and_rules_are_named(self) -> None:
        (self.root / "latexmkrc").write_text(
            "# $pdf_mode = 4;\n$pdf_mode = 5;\nadd_cus_dep('glo', 'gls', 0, 'mg');\n"
            "$pdflatex = 'pdflatex -shell-escape %O %S';\n"
        )
        [configuration] = find_latexmk_configurations(self.root, "main.tex")
        self.assertEqual(
            configuration,
            LatexmkConfiguration("latexmkrc", "5", custom_rules=True, shell_escape=True),
        )
        finding = latexmk_configuration_finding(
            configuration, choose_engine("auto", None), "main.tex"
        )
        self.assertEqual(
            (finding.code, finding.severity, finding.path), ("BLD107", "warning", "latexmkrc")
        )
        self.assertIn("is not executed", finding.message)
        self.assertIn("It selects XeLaTeX ($pdf_mode = 5)", finding.message)
        self.assertIn("custom rules", finding.message)
        self.assertIn("shell escape", finding.message)
        self.assertIn("--engine xelatex", finding.suggestion or "")

    def test_matching_or_absent_configuration(self) -> None:
        self.assertEqual(find_latexmk_configurations(self.root, "main.tex"), [])
        (self.root / "paper").mkdir()
        (self.root / "paper" / ".latexmkrc").write_text("$pdf_mode = 5;\n")
        (self.root / ".latexmkrc").write_text("$pdf_previewer = 'open';\n")
        found = find_latexmk_configurations(self.root, "paper/main.tex")
        self.assertEqual([item.name for item in found], ["paper/.latexmkrc", ".latexmkrc"])
        matching = latexmk_configuration_finding(
            found[0], choose_engine("xelatex", None), "paper/main.tex"
        )
        self.assertEqual(matching.severity, "info")
        self.assertNotIn("--engine xelatex", matching.suggestion or "")
        plain = latexmk_configuration_finding(
            found[1], choose_engine("auto", None), "paper/main.tex"
        )
        self.assertEqual(plain.severity, "info")
        self.assertNotIn("selects", plain.message)


class BuildEngineTests(unittest.IsolatedAsyncioTestCase):
    """A build runs the engine the magic comment names unless the setting chooses one."""

    async def asyncSetUp(self) -> None:
        """Create a project whose main file asks for XeLaTeX."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text(
            "% !TEX program = xelatex\n\\documentclass{article}\n"
        )

    async def test_auto_follows_the_comment_and_a_setting_overrides_it(self) -> None:
        runner = FakeBuildRunner()
        result = await build_project(self.source, "main.tex", self.root / "auto", "auto", runner)
        self.assertIn("-xelatex", runner.commands[-1])
        self.assertIn("xelatex", result.tools.versions)
        engine = next(item for item in result.findings if item.code == "BLD106")
        self.assertEqual(engine.details["source"], "magic comment")
        runner = FakeBuildRunner()
        result = await build_project(self.source, "main.tex", self.root / "set", "pdflatex", runner)
        self.assertIn("-pdf", runner.commands[-1])
        engine = next(item for item in result.findings if item.code == "BLD106")
        self.assertEqual(engine.details["source"], "setting")

    async def test_a_latexmkrc_is_reported_and_not_executed(self) -> None:
        (self.source / "latexmkrc").write_text("$pdf_mode = 4;\ndie 'must not run';\n")
        runner = FakeBuildRunner()
        result = await build_project(self.source, "main.tex", self.root / "build", "auto", runner)
        self.assertTrue(result.success)
        self.assertIn("-norc", runner.commands[-1])
        configuration = next(item for item in result.findings if item.code == "BLD107")
        self.assertIn("--engine lualatex", configuration.suggestion or "")
