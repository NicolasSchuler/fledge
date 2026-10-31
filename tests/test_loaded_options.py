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
from latexprep.build_result import BuildResult
from latexprep.errors import PreparationError
from latexprep.latexmk_build import build_project
from latexprep.loaded_options import (
    BuildEvidence,
    collect_loaded_options,
    decode_native_string,
    parse_kernel_options,
    stage_loaded_options,
)
from latexprep.pdf_comparison import compare_pdfs
from latexprep.runtime import ToolRunner
from tests.fakes import FakeBuildRunner
from tests.support import live_test


def _units(value: str, engine: str = "pdflatex") -> str:
    """Encode ``value`` as the hexadecimal code units of the option evidence.

    Args:
        value: The text to encode.
        engine: The engine whose units to use: UTF-8 bytes for ``pdflatex``, Unicode code
            points for any other engine.

    Returns:
        Each unit as eight upper-case hexadecimal digits, concatenated.
    """
    units = value.encode("utf-8") if engine == "pdflatex" else [ord(char) for char in value]
    return "".join(f"{unit:08X}" for unit in units)


def _evidence(token: str, engine: str = "pdflatex") -> str:
    """Return complete loaded-options evidence for the hook with ``token``.

    The records name a 2025-11-01 kernel, the global options ``11pt,twoside``, and the
    options of ``article.cls`` and of ``local.sty``, each with its raw source text.

    Args:
        token: The hook's token, which opens and closes the records.
        engine: The engine the records name and whose code units encode the values.

    Returns:
        The evidence, one record per line.
    """
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
    """A build runner that leaves the loaded-options evidence a real TeX run would leave.

    Each build writes a recorder file (``.fls``) that lists the main file, ``local.sty``,
    the toolchain's ``article.cls`` and the staged hook, and the hook's sidecar
    (``output/<hook>.lpo`` in the workspace) with the evidence of :func:`_evidence`.

    Args:
        toolchain: The folder that holds the toolchain's ``article.cls``; builds may report
            it as the origin of loaded packages.

    Attributes:
        fixture_toolchain: The ``toolchain`` folder.
        mutation: One way to break the evidence, such as ``"missing-end"``,
            ``"wrong-token"`` or ``"absent"``; the empty default keeps it complete.
        log: The text of the ``.log`` file each build writes.
    """

    def __init__(self, toolchain: Path, mutation: str = "", *, log: str = "") -> None:
        super().__init__(log=log, resource_roots=[toolchain])
        self.fixture_toolchain = toolchain
        self.mutation = mutation

    async def run(self, argv, cwd, workspace, **kwargs):
        """Run the fake build, then write the recorder file and the hook's sidecar.

        The arguments are those of :meth:`ToolRunner.run`; the last item of ``argv`` is the
        main file, and ``workspace`` holds the staged hook and ``output``. Every call
        returns the fake build's result; a version probe writes nothing else.
        """
        result = await super().run(argv, cwd, workspace, **kwargs)
        if argv[-1] in {"-v", "--version"}:
            return result
        trace = [
            cwd / Path(argv[-1]).name,
            cwd / "local.sty",
            self.fixture_toolchain / "article.cls",
        ]
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
                other = self.fixture_toolchain / "local.sty"
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
                sidecar.symlink_to(self.fixture_toolchain / "article.cls")
            elif self.mutation != "absent":
                sidecar.write_text(text)
        (workspace / "output" / (Path(argv[-1]).stem + ".fls")).write_text(
            "".join(f"INPUT {path}\n" for path in trace)
        )
        return result


class LoadedOptionsTests(unittest.IsolatedAsyncioTestCase):
    """Loaded-option collection with controlled builds: evidence, tampering, settings, hook."""

    async def asyncSetUp(self):
        """Create a project and a toolchain folder in a temporary directory.

        ``paper/main.tex`` loads the project's ``local.sty`` with an option, and the
        toolchain holds ``article.cls``.
        """
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
        """Remove the temporary directory."""
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
            self.assertEqual(decode_native_string(_units(value, engine), engine), value)
            data = parse_kernel_options(_evidence("a" * 32, engine), "a" * 32, engine)
            self.assertEqual(data.packages[1].options, "forwarded=actual,value={a,b|c}")
        for value in ("!", "00", "G" * 8, "FFFFFFFF", "00000000" * 16_385):
            with self.subTest(value=value[:20]), self.assertRaises(PreparationError):
                decode_native_string(value, "pdflatex")

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
    """What :func:`collect_loaded_options` says about missing, incomplete or non-ASCII evidence."""

    def collect(self, directory: str, sidecar: bytes | None):
        """Stage the option probe for a one-line project and collect its evidence.

        Args:
            directory: The temporary folder to stage the project in.
            sidecar: The bytes to write as the probe's sidecar, or ``None`` to write none.

        Returns:
            The staged probe and the collected evidence.
        """
        work = Path(directory).resolve() / "staging"
        project = work / "project"
        project.mkdir(parents=True)
        (project / "main.tex").write_bytes(b"\\documentclass{article}\n")
        probe = stage_loaded_options(project, "main.tex", work)
        if sidecar is not None:
            probe.sidecar.parent.mkdir(parents=True)
            probe.sidecar.write_bytes(sidecar)
        evidence = collect_loaded_options(probe, self.build_evidence(probe))
        return probe, evidence

    def build_evidence(self, probe, **overrides) -> BuildEvidence:
        """Return the evidence of a complete pdfLaTeX build that recorded the probe's hook.

        Args:
            probe: The staged probe whose hook the recorder lists.
            **overrides: Fields of :class:`BuildEvidence` that replace the defaults.

        Returns:
            The build evidence.
        """
        return BuildEvidence(
            **{
                "engine": "pdflatex",
                "packages": (),
                "recorder_inputs": frozenset({probe.hook}),
                "recorder_complete": True,
                "build_complete": True,
                "max_bytes": 1_000_000,
                **overrides,
            }
        )

    def test_a_missing_option_file_is_explained_without_local_paths_or_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, evidence = self.collect(directory, None)
            self.assertFalse(evidence.complete)
            self.assertIn("could not be read (no such file or directory)", evidence.uncertainty)
            for private in (directory, str(Path(directory).resolve()), probe.token, ".lpo"):
                self.assertNotIn(private, evidence.uncertainty)
            self.assertNotIn(probe.token, evidence.finding("main.tex").message)

    def test_evidence_needs_a_successful_build_and_a_complete_recorder(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, _ = self.collect(directory, None)
            for overrides, reason in (
                ({"build_complete": False}, "A successful build and complete recorder"),
                ({"recorder_complete": False}, "A successful build and complete recorder"),
                ({"recorder_inputs": frozenset()}, "did not observe the trusted options hook"),
            ):
                with self.subTest(overrides=overrides):
                    build = self.build_evidence(probe, **overrides)
                    evidence = collect_loaded_options(probe, build)
                    self.assertFalse(evidence.complete)
                    self.assertIn(reason, evidence.uncertainty)

    def test_option_evidence_that_is_not_ascii_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as directory:
            _, evidence = self.collect(directory, b"START|\xff\xfe")
            self.assertFalse(evidence.complete)
            self.assertEqual(evidence.uncertainty, "The option evidence is not plain ASCII text")


@live_test("Enable FLEDGE_RUN_INTEGRATION for real restricted TeX option instrumentation")
class LiveLoadedOptionsTests(unittest.IsolatedAsyncioTestCase):
    """Live loaded-option collection with real restricted TeX."""

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
