"""Configured build policies use real log/recorder evidence, never registry-only fixtures."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latexprep.build_checks import (
    BuildCheckOptions,
    check_build_details,
    check_local_package_shadows,
    check_tex_distribution,
)
from latexprep.models import Finding, PreparationError
from latexprep.runtime import (
    BuildResult,
    CommandResult,
    _log_findings,
    _package_record,
    build_project,
)
from tests.test_runtime import FakeBuildRunner


class LookupRunner:
    def __init__(self, status, output):
        self.status, self.output = status, output
        self.calls = []

    async def tool_version(self, argv, workspace):
        assert workspace.is_dir()
        return {"version": "Kpathsea fixture"}

    async def run(self, argv, cwd, workspace):
        self.calls.append((argv, cwd, workspace))
        assert cwd == workspace
        assert not list(workspace.iterdir())
        return CommandResult(self.status, self.output, "", False, argv)


class BuildCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorder_collects_loaded_packages_and_rejects_empty_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, toolchain = root / "source", root / "toolchain"
            source.mkdir()
            toolchain.mkdir()
            (source / "main.tex").write_text("\\documentclass{article}")
            (source / "local.sty").write_text("\\ProvidesPackage{local}[2025/02/03 v1]")
            (toolchain / "article.cls").write_text(
                "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesClass{article}[2025/01/01 v1]\n"
            )

            class RecorderRunner(FakeBuildRunner):
                async def run(self, argv, cwd, workspace, **kwargs):
                    result = await super().run(argv, cwd, workspace, **kwargs)
                    if argv[-1] not in {"-v", "--version"}:
                        (workspace / "output/main.fls").write_text(
                            f"INPUT {cwd / 'main.tex'}\nINPUT {cwd / 'local.sty'}\n"
                            f"INPUT {toolchain / 'article.cls'}\nINPUT {cwd / 'local.sty'}\n"
                        )
                    return result

            runner = RecorderRunner()
            runner._test_resource_roots.add(toolchain)
            result = await build_project(source, "main.tex", root / "build", "pdflatex", runner)
            self.assertTrue(result.success, result.findings)
            self.assertTrue(result.recorder_complete)
            records = {item["name"]: item for item in result.loaded_packages}
            self.assertEqual(len(result.loaded_packages), 2)
            self.assertEqual(records["local.sty"]["origin"], "project")
            self.assertEqual(records["local.sty"]["path"], "local.sty")
            self.assertEqual(records["article.cls"]["declared_date"], "2025/01/01")
            self.assertEqual(records["article.cls"]["origin"], "toolchain")
            findings = check_build_details(
                result,
                BuildCheckOptions(
                    required_loaded_packages=("article.cls", "local.sty"),
                    minimum_package_dates=(("local.sty", "2025-02-03"),),
                ),
            )
            self.assertTrue(all(item.status == "passed" for item in findings), findings)
            missing = await build_project(
                source,
                "main.tex",
                root / "empty",
                "pdflatex",
                FakeBuildRunner(trace="\n"),
            )
            self.assertFalse(missing.recorder_complete)
            self.assertFalse(missing.success)
            self.assertIn("build.dependency_trace", {item.rule for item in missing.findings})

    def test_inactive_and_ambiguous_package_headers_never_supply_a_required_date(self):
        declaration = "\\ProvidesPackage{custom}[2099/01/01 v9]"
        for content in (
            "\\endinput\n" + declaration,
            "\\iffalse\n" + declaration + "\n\\fi",
            "\\def\\unused{" + declaration + "}",
            "\\begin{verbatim}" + declaration + "\\end{verbatim}",
            "{" + declaration + "}",
            "% " + declaration,
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "custom.sty"
                path.write_text(content)
                record = _package_record(path, root)
                self.assertIsNone(record["declared_date"])
                finding = check_build_details(
                    BuildResult(success=True, recorder_complete=True, loaded_packages=[record]),
                    BuildCheckOptions(minimum_package_dates=(("custom.sty", "2026-01-01"),)),
                )[0]
                self.assertEqual(
                    (finding.code, finding.status, finding.severity),
                    ("BLD103", "inconclusive", "error"),
                )

    def test_overfull_tolerance(self):
        result = BuildResult(
            success=True,
            findings=_log_findings(
                "Overfull \\hbox (2.5pt too wide) in paragraph at lines 12--14\n", "main.tex"
            ),
        )
        passed = check_build_details(result, BuildCheckOptions(overfull_tolerance_pt=2.5))[0]
        self.assertEqual((passed.code, passed.status), ("BLD101", "passed"))
        failed = check_build_details(result, BuildCheckOptions(overfull_tolerance_pt=2.4))[0]
        self.assertEqual(
            (failed.code, failed.status, failed.severity), ("BLD101", "failed", "error")
        )
        self.assertEqual(failed.details["measurements"][0]["overflow_tex_pt"], 2.5)
        result.findings.append(Finding("build.overfull_box", "Overfull but amount unavailable"))
        incomplete = check_build_details(result, BuildCheckOptions(overfull_tolerance_pt=4))[0]
        self.assertEqual(incomplete.status, "inconclusive")
        self.assertEqual(
            check_build_details(
                BuildResult(success=False), BuildCheckOptions(overfull_tolerance_pt=4)
            )[0].status,
            "inconclusive",
        )

    def test_loaded_package_policy(self):
        result = BuildResult(
            success=True,
            recorder_complete=True,
            loaded_packages=[
                {"name": "article.cls", "origin": "toolchain", "path": "/toolchain/article.cls"},
                {"name": "graphicx.sty", "origin": "toolchain", "path": "/toolchain/graphicx.sty"},
            ],
        )
        options = BuildCheckOptions(
            allowed_loaded_packages=("article.cls", "graphicx.sty"),
            required_loaded_packages=("graphicx.sty",),
        )
        self.assertEqual(check_build_details(result, options)[0].status, "passed")
        failed = check_build_details(
            result, replace(options, forbidden_loaded_packages=("article.cls",))
        )[0]
        self.assertEqual((failed.code, failed.status), ("BLD102", "failed"))
        self.assertEqual(failed.details["forbidden"], ["article.cls"])
        forbidden = check_build_details(result, BuildCheckOptions(allowed_loaded_packages=()))[0]
        self.assertEqual(forbidden.details["not_allowed"], ["article.cls", "graphicx.sty"])
        missing = check_build_details(
            result, BuildCheckOptions(required_loaded_packages=("missing.sty",))
        )[0]
        self.assertEqual(missing.status, "failed")
        result.recorder_complete = False
        self.assertEqual(check_build_details(result, options)[0].status, "inconclusive")
        # Directly observed forbidden input remains evidence even in a partial build.
        self.assertEqual(
            check_build_details(
                result, BuildCheckOptions(forbidden_loaded_packages=("graphicx.sty",))
            )[0].status,
            "failed",
        )

    def test_package_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "local.sty"
            path.write_text(
                "% \\ProvidesPackage{decoy}[1900/01/01]\n"
                "\\ProvidesPackage{local}[2025/05/09 v1.2 Local]\n"
            )
            record = _package_record(path, root)
            self.assertEqual(record["declared_date"], "2025/05/09")
            self.assertEqual(record["declared_version"], "v1.2")
            result = BuildResult(success=True, recorder_complete=True, loaded_packages=[record])
            passed = check_build_details(
                result, BuildCheckOptions(minimum_package_dates=(("local.sty", "2025-05-09"),))
            )[0]
            self.assertEqual((passed.code, passed.status), ("BLD103", "passed"))
            self.assertEqual(
                check_build_details(
                    result, BuildCheckOptions(minimum_package_dates=(("local.sty", "2025-05-10"),))
                )[0].status,
                "failed",
            )
            path.write_text("\\ProvidesPackage{local}[\\release_date]\n")
            result.loaded_packages = [_package_record(path, root)]
            self.assertEqual(
                check_build_details(
                    result, BuildCheckOptions(minimum_package_dates=(("local.sty", "2025-01-01"),))
                )[0].status,
                "inconclusive",
            )
            path.write_text("\\ProvidesExplPackage{local}{2026-10-06}{1.0}{Fixture}\n")
            self.assertEqual(_package_record(path, root)["declared_date"], "2026-10-06")

    async def test_shadow_lookup(self):
        result = BuildResult(
            success=True,
            recorder_complete=True,
            loaded_packages=[{"name": "article.cls", "path": "article.cls", "origin": "project"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for index, (code, text, status) in enumerate(
                (
                    (0, "/toolchain/article.cls\n", "failed"),
                    (1, "", "passed"),
                    (2, "", "inconclusive"),
                    (0, "", "inconclusive"),
                )
            ):
                runner = LookupRunner(code, text)
                findings = await check_local_package_shadows(result, base / str(index), runner)
                finding = findings[0]
                self.assertEqual((finding.code, finding.status), ("BLD104", status))
                if status == "failed":
                    self.assertEqual(finding.severity, "warning")
                    self.assertEqual(finding.path, "article.cls")
                self.assertEqual(runner.calls[0][0], ["kpsewhich", "--format=tex", "article.cls"])
            result.recorder_complete = False
            runner = LookupRunner(0, "/toolchain/article.cls\n")
            self.assertEqual(
                (await check_local_package_shadows(result, base / "partial", runner))[0].status,
                "inconclusive",
            )
            self.assertEqual(runner.calls, [])

    def test_invalid_build_options_fail_before_analysis(self):
        for options in (
            {"overfull_tolerance_pt": float("nan")},
            {"overfull_tolerance_pt": -1},
            {"allowed_loaded_packages": ("../article.cls",)},
            {"minimum_package_dates": (("test.sty", "2026/02/31"),)},
            {"required_loaded_packages": ("a.sty",), "forbidden_loaded_packages": ("a.sty",)},
        ):
            with self.subTest(options=options), self.assertRaises(PreparationError):
                BuildCheckOptions(**options)


class VersionRunner(FakeBuildRunner):
    """Answer the engine version probe with a fixed banner, or fail it."""

    def __init__(self, banner: str | None) -> None:
        super().__init__()
        self.banner = banner

    async def run(self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None):
        if argv[-1] == "--version":
            if self.banner is None:
                return CommandResult(127, "", "pdflatex: command not found", False, argv)
            return CommandResult(0, self.banner + "\nkpathsea version 6.4.2\n", "", False, argv)
        return await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )


class TexDistributionTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, banner: str | None) -> BuildResult:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "main.tex").write_text("\\documentclass{article}")
            return await build_project(
                source, "main.tex", Path(directory) / "build", "pdflatex", VersionRunner(banner)
            )

    async def test_records_texlive_year_and_compares_explicit_expectation(self) -> None:
        result = await self.build("pdfTeX 3.141592653-2.6-1.40.29 (TeX Live 2026)")
        observed = check_tex_distribution(result)
        self.assertEqual(
            (observed.code, observed.severity, observed.status), ("BLD105", "info", "passed")
        )
        self.assertEqual(observed.details["texlive_year"], 2026)
        self.assertIn("arXiv", observed.message)
        same = check_tex_distribution(result, BuildCheckOptions(expected_texlive_year=2026))
        self.assertEqual((same.severity, same.status), ("info", "passed"))
        other = check_tex_distribution(result, BuildCheckOptions(expected_texlive_year=2023))
        self.assertEqual((other.severity, other.status), ("warning", "failed"))
        self.assertIn("expected TeX Live 2023", other.message)
        packaged = BuildResult(
            True, tools={"xelatex": {"version": ["XeTeX 3.14 (TeX Live 2022/Debian)"]}}
        )
        self.assertEqual(check_tex_distribution(packaged).details["packager"], "Debian")
        miktex = BuildResult(
            True, tools={"pdflatex": {"version": ["MiKTeX-pdfTeX 4.19 (MiKTeX 24.1)"]}}
        )
        self.assertEqual(check_tex_distribution(miktex).status, "passed")
        self.assertEqual(
            check_tex_distribution(miktex, BuildCheckOptions(expected_texlive_year=2024)).status,
            "failed",
        )
        for value in (1995, 2101, "2024", True):
            with self.subTest(value=value), self.assertRaises(PreparationError):
                BuildCheckOptions(expected_texlive_year=value)  # type: ignore[arg-type]

    async def test_missing_or_unrecognized_version_is_inconclusive(self) -> None:
        missing = await self.build(None)
        self.assertIn("build.execution_unavailable", {item.rule for item in missing.findings})
        observed = check_tex_distribution(missing)
        self.assertEqual((observed.severity, observed.status), ("info", "inconclusive"))
        policy = check_tex_distribution(missing, BuildCheckOptions(expected_texlive_year=2026))
        self.assertEqual((policy.severity, policy.status), ("warning", "inconclusive"))
        unknown = BuildResult(True, tools={"pdflatex": {"version": ["pdfTeX 3.14 (custom)"]}})
        self.assertEqual(check_tex_distribution(unknown).status, "inconclusive")
        self.assertEqual(
            check_tex_distribution(unknown, BuildCheckOptions(expected_texlive_year=2026)).status,
            "inconclusive",
        )
