"""Actual option inventory requires complete kernel evidence, not source guesses."""

from __future__ import annotations

import fnmatch
import tempfile
import tomllib
import unittest
from dataclasses import FrozenInstanceError
from importlib import resources
from pathlib import Path

from latexprep.build_checks import BuildCheckOptions, check_build_details
from latexprep.latexmk_build import BuildResult, build_project
from latexprep.loaded_options import (
    _native_string,
    _parse_options,
    collect_loaded_options,
    stage_loaded_options,
)
from latexprep.models import PreparationError
from latexprep.pdf_comparison import compare_pdfs
from latexprep.runtime import ToolRunner
from tests.support import live_tests_enabled
from tests.test_runtime import FakeBuildRunner


def _units(value: str, engine: str = "pdflatex") -> str:
    units = value.encode("utf-8") if engine == "pdflatex" else [ord(char) for char in value]
    return "".join(f"{unit:08X}" for unit in units)


def _evidence(token: str, engine: str = "pdflatex") -> str:
    return "\n".join(
        [
            f"START|{token}",
            f"ENGINE|{engine}",
            "FORMAT|" + _units("2025-11-01", engine),
            "GLOBAL|" + _units("11pt,twoside", engine),
            "UNUSED|",
            "PACKAGE|article.cls",
            "OPTIONS|" + _units("11pt,twoside", engine),
            "RAW|" + _units("11pt, twoside", engine),
            "PACKAGE|local.sty",
            "OPTIONS|" + _units("forwarded=actual,value={a,b|c}", engine),
            "RAW|" + _units(r"forwarded=\selected , value={a,b|c}", engine),
            f"END|{token}",
            "",
        ]
    )


class OptionsRunner(FakeBuildRunner):
    def __init__(self, toolchain: Path, mutation: str = "", *, log: str = "") -> None:
        super().__init__(log=log)
        self.toolchain = toolchain
        self.mutation = mutation
        self._test_resource_roots.add(toolchain)

    async def run(self, argv, cwd, workspace, **kwargs):
        result = await super().run(argv, cwd, workspace, **kwargs)
        if argv[-1] in {"-v", "--version"}:
            return result
        trace = [cwd / Path(argv[-1]).name, cwd / "local.sty", self.toolchain / "article.cls"]
        hooks = list(workspace.glob("latexprep-loaded-options-*.tex"))
        if hooks:
            hook = hooks[0]
            token = hook.stem.rsplit("-", 1)[1]
            if self.mutation != "unrecorded-hook":
                trace.append(hook)
            text = _evidence(token)
            if self.mutation == "missing-end":
                text = text.replace(f"END|{token}\n", "")
            elif self.mutation == "wrong-token":
                text = text.replace(token, "0" * 32)
            elif self.mutation == "unsupported-engine":
                text = text.replace("ENGINE|pdflatex", "ENGINE|unknown")
            elif self.mutation == "old-kernel":
                text = text.replace(_units("2025-11-01"), _units("2019-01-01"))
            elif self.mutation == "reordered-records":
                text = text.replace("OPTIONS|", "RAW|", 1)
            elif self.mutation == "missing-register":
                text = text.replace("UNUSED|\n", "UNUSED|!\n")
            elif self.mutation == "graph-mismatch":
                text = text.replace("PACKAGE|local.sty", "PACKAGE|other.sty")
            elif self.mutation == "duplicate-record":
                text = text.replace("PACKAGE|local.sty", "PACKAGE|article.cls")
            elif self.mutation == "ambiguous-origin":
                other = self.toolchain / "local.sty"
                other.write_text("\\ProvidesPackage{local}")
                trace.append(other)
            elif self.mutation == "unexpanded-value":
                text = text.replace(_units("11pt,twoside"), _units(r"\unresolved"))
            elif self.mutation == "unsupported-name":
                text = text.replace("PACKAGE|local.sty", "UNSUPPORTED|nonliteral-package-filename")
            elif self.mutation == "modified-hook":
                hook.write_bytes(hook.read_bytes() + b"% modified\n")
            elif self.mutation == "modified-source":
                source = cwd / Path(argv[-1]).name
                source.write_bytes(source.read_bytes() + b"% modified\n")
            elif self.mutation == "oversized":
                text = "x" * 2_097_153
            sidecar = workspace / "output" / f"{hook.stem}.lpo"
            if self.mutation == "sidecar-symlink":
                sidecar.symlink_to(self.toolchain / "article.cls")
            elif self.mutation != "absent":
                sidecar.write_text(text)
        (workspace / "output" / (Path(argv[-1]).stem + ".fls")).write_text(
            "".join(f"INPUT {path}\n" for path in trace)
        )
        return result


class LoadedOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source, self.toolchain = self.root / "source", self.root / "toolchain"
        (self.source / "paper").mkdir(parents=True)
        self.toolchain.mkdir()
        self.main = "paper/main.tex"
        self.original = (
            b"\\documentclass[10pt]{article}\n"
            b"\\usepackage[literal-only]{local}\n"
            b"\\begin{document}\nHello.\\end{document}\n"
        )
        (self.source / self.main).write_bytes(self.original)
        (self.source / "paper/local.sty").write_text("\\ProvidesPackage{local}")
        (self.toolchain / "article.cls").write_text("\\ProvidesClass{article}")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_actual_options_and_origins_match_recorder(self):
        runner = OptionsRunner(self.toolchain)
        result = await build_project(
            self.source,
            self.main,
            self.root / "build",
            "pdflatex",
            runner,
            inventory_loaded_options=True,
        )
        self.assertTrue(result.success, result.findings)
        finding = next(item for item in result.findings if item.rule == "build.loaded_options")
        self.assertEqual((finding.code, finding.status), ("BLD301", "passed"))
        self.assertEqual(finding.details["global_class_options"], "11pt,twoside")
        packages = {item["name"]: item for item in finding.details["packages"]}
        self.assertEqual(packages["article.cls"]["options"], "11pt,twoside")
        self.assertEqual(packages["article.cls"]["origin"], "toolchain")
        self.assertEqual(packages["local.sty"]["options"], "forwarded=actual,value={a,b|c}")
        self.assertEqual(
            packages["local.sty"]["raw_options"], r"forwarded=\selected , value={a,b|c}"
        )
        self.assertEqual(packages["local.sty"]["origin"], "project")
        self.assertEqual(packages["local.sty"]["path"], "paper/local.sty")
        self.assertEqual((self.source / self.main).read_bytes(), self.original)
        self.assertEqual(result.pdf.name, "main.pdf")
        self.assertEqual(result.command[-1], "./main.tex")
        self.assertIn("-no-shell-escape", result.command)
        self.assertEqual(result.recorder_project_inputs, {self.main, "paper/local.sty"})
        copied = (self.root / "build/project" / self.main).read_bytes()
        self.assertEqual(copied.count(b"\n"), self.original.count(b"\n"))
        self.assertTrue(copied.endswith(self.original))
        self.assertEqual(
            check_build_details(result, BuildCheckOptions(inventory_loaded_options=True)), []
        )

    async def test_absent_tampered_and_unsupported_evidence_is_inconclusive(self):
        for mutation in (
            "absent",
            "missing-end",
            "wrong-token",
            "unsupported-engine",
            "old-kernel",
            "reordered-records",
            "missing-register",
            "graph-mismatch",
            "duplicate-record",
            "ambiguous-origin",
            "unexpanded-value",
            "unsupported-name",
            "modified-hook",
            "modified-source",
            "unrecorded-hook",
            "sidecar-symlink",
            "oversized",
        ):
            with self.subTest(mutation=mutation):
                result = await build_project(
                    self.source,
                    self.main,
                    self.root / mutation,
                    "pdflatex",
                    OptionsRunner(self.toolchain, mutation),
                    inventory_loaded_options=True,
                )
                finding = next(
                    item for item in result.findings if item.rule == "build.loaded_options"
                )
                self.assertEqual(
                    (finding.code, finding.status, finding.severity),
                    ("BLD301", "inconclusive", "error"),
                )
                self.assertFalse(result.success)
                self.assertIsNone(result.pdf)
                self.assertFalse(finding.details["coverage_complete"])
        self.assertEqual((self.source / self.main).read_bytes(), self.original)

    async def test_disabled_preserves_original_build_and_diagnostics(self):
        result = await build_project(
            self.source,
            self.main,
            self.root / "disabled",
            "pdflatex",
            OptionsRunner(self.toolchain),
        )
        self.assertTrue(result.success)
        self.assertFalse(list((self.root / "disabled").glob("latexprep-loaded-options-*")))
        self.assertEqual((self.root / "disabled/project" / self.main).read_bytes(), self.original)
        self.assertNotIn("build.loaded_options", {item.rule for item in result.findings})
        result = await build_project(
            self.source,
            self.main,
            self.root / "diagnostics",
            "pdflatex",
            OptionsRunner(self.toolchain, log="main.tex:4: Undefined control sequence."),
            inventory_loaded_options=True,
        )
        diagnostic = next(item for item in result.findings if item.rule == "build.tex_error")
        self.assertEqual((diagnostic.path, diagnostic.line), (self.main, 4))
        self.assertEqual(
            next(item for item in result.findings if item.rule == "build.loaded_options").status,
            "inconclusive",
        )

    def test_option_settings_are_immutable_and_missing_collection_is_not_a_pass(self):
        with self.assertRaises(PreparationError):
            BuildCheckOptions(inventory_loaded_options=1)
        options = BuildCheckOptions(inventory_loaded_options=True)
        with self.assertRaises(FrozenInstanceError):
            options.inventory_loaded_options = False
        finding = check_build_details(BuildResult(success=True), options)[0]
        self.assertEqual((finding.code, finding.status), ("BLD301", "inconclusive"))

    def test_transport_preserves_delimiters_spaces_and_native_unicode_without_expansion(self):
        value = "empty=,key={a,b|c},word=Grüße,space=two words\nnewline"
        for engine in ("pdflatex", "xelatex", "lualatex"):
            self.assertEqual(_native_string(_units(value, engine), engine), value)
            data = _parse_options(_evidence("a" * 32, engine), "a" * 32, engine)
            self.assertEqual(data.packages[1].options, "forwarded=actual,value={a,b|c}")
        for value in ("!", "00", "G" * 8, "FFFFFFFF", "00000000" * 16_385):
            with self.subTest(value=value[:20]), self.assertRaises(PreparationError):
                _native_string(value, "pdflatex")

    def test_hook_ships_as_package_data_with_its_token_placeholders(self):
        root = Path(__file__).resolve().parents[1]
        hook = resources.files("latexprep").joinpath("loaded_options_hook.tex")
        text = hook.read_text(encoding="ascii")
        self.assertTrue(text.startswith("\\ifdefined\\ExplSyntaxOn"))
        self.assertIn("START|TOKEN", text)
        self.assertIn("END|TOKEN", text)
        with (root / "pyproject.toml").open("rb") as stream:
            patterns = tomllib.load(stream)["tool"]["setuptools"]["package-data"]["latexprep"]
        self.assertTrue(any(fnmatch.fnmatch("loaded_options_hook.tex", item) for item in patterns))
        manifest = (root / "MANIFEST.in").read_text().splitlines()
        self.assertIn("include src/latexprep/loaded_options_hook.tex", manifest)

    def test_bom_and_line_numbers_are_preserved_and_hook_stays_outside_project(self):
        work = self.root / "staging"
        project = work / "project"
        project.mkdir(parents=True)
        original = b"\xef\xbb\xbf\\documentclass{article}\r\nbody\r\n"
        (project / "main.tex").write_bytes(original)
        probe = stage_loaded_options(project, "main.tex", work)
        self.assertTrue(probe.staged_bytes.startswith(b"\xef\xbb\xbf\\input{../"))
        self.assertTrue(probe.staged_bytes.endswith(original[3:]))
        self.assertEqual(probe.staged_bytes.count(b"\n"), 2)
        self.assertEqual(probe.hook.parent, work)


class LoadedOptionsDiagnosticsTests(unittest.TestCase):
    def collect(self, directory: str, sidecar: bytes | None):
        work = Path(directory).resolve() / "staging"
        project = work / "project"
        project.mkdir(parents=True)
        (project / "main.tex").write_bytes(b"\\documentclass{article}\n")
        probe = stage_loaded_options(project, "main.tex", work)
        if sidecar is not None:
            probe.sidecar.parent.mkdir(parents=True)
            probe.sidecar.write_bytes(sidecar)
        evidence = collect_loaded_options(
            probe,
            [],
            {probe.hook},
            engine="pdflatex",
            recorder_complete=True,
            build_complete=True,
            max_bytes=1_000_000,
        )
        return probe, evidence

    def test_a_missing_option_file_is_explained_without_local_paths_or_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, evidence = self.collect(directory, None)
            self.assertFalse(evidence.complete)
            self.assertIn("could not be read (no such file or directory)", evidence.uncertainty)
            for private in (directory, str(Path(directory).resolve()), probe.token, ".lpo"):
                self.assertNotIn(private, evidence.uncertainty)
            self.assertNotIn(probe.token, evidence.finding("main.tex").message)

    def test_option_evidence_that_is_not_ascii_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as directory:
            _, evidence = self.collect(directory, b"START|\xff\xfe")
            self.assertFalse(evidence.complete)
            self.assertEqual(evidence.uncertainty, "The option evidence is not plain ASCII text")


@unittest.skipUnless(
    live_tests_enabled(),
    "Enable FLEDGE_RUN_INTEGRATION for real restricted TeX option instrumentation",
)
class LiveLoadedOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_forwarded_options_preserve_rendered_pdf_and_source(self):
        with tempfile.TemporaryDirectory(prefix="latex-prep-options-") as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.mkdir()
            original = (
                b"\\PassOptionsToPackage{draft}{graphicx}\n"
                b"\\documentclass[11pt,twoside]{article}\n"
                b"\\usepackage{graphicx}\n"
                b"\\begin{document}A stable page.\\end{document}\n"
            )
            (source / "main.tex").write_bytes(original)
            runner = ToolRunner(source_date_epoch=1_700_000_000)
            baseline = await build_project(
                source, "main.tex", root / "baseline", "pdflatex", runner
            )
            measured = await build_project(
                source,
                "main.tex",
                root / "measured",
                "pdflatex",
                runner,
                inventory_loaded_options=True,
            )
            self.assertTrue(baseline.success, baseline.findings)
            self.assertTrue(measured.success, measured.findings)
            finding = next(
                item for item in measured.findings if item.rule == "build.loaded_options"
            )
            self.assertEqual((finding.code, finding.status), ("BLD301", "passed"))
            records = {item["name"]: item for item in finding.details["packages"]}
            self.assertEqual(records["article.cls"]["options"], "11pt,twoside")
            self.assertEqual(records["graphicx.sty"]["options"].strip(","), "draft")
            self.assertEqual(records["graphics.sty"]["options"].strip(","), "draft")
            self.assertEqual(records["graphicx.sty"]["origin"], "toolchain")
            self.assertEqual((source / "main.tex").read_bytes(), original)
            self.assertEqual(measured.recorder_project_inputs, baseline.recorder_project_inputs)
            comparison = await compare_pdfs(baseline.pdf, measured.pdf, root / "comparison", runner)
            self.assertTrue(comparison, comparison)
            self.assertTrue(all(item.status == "passed" for item in comparison), comparison)


if __name__ == "__main__":
    unittest.main()
