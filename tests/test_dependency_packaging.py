"""Preparation selects inputs from explicit fake build traces and real source analysis."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Finding, PreparationError, Report
from latexprep.package_inputs import generated_bibliography_inputs
from latexprep.runtime import BuildResult
from latexprep.source_transform import SourceTransformOptions
from latexprep.submission_checks import SubmissionOptions
from latexprep.workflow_options import DocumentOptions, WorkflowOptions


def contents(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class RecordedBuild:
    """Supply fixed per-document traces; fake PDFs validate orchestration only."""

    def __init__(
        self,
        traces: dict[str, tuple[set[str] | None, ...]],
        *,
        legacy_result: bool = False,
        fail_archive_main: str | None = None,
        generated_reads: frozenset[str] = frozenset(),
    ) -> None:
        self.traces = traces
        self.generated_reads = generated_reads
        self.legacy_result = legacy_result
        self.fail_archive_main = fail_archive_main
        self.counts: Counter[str] = Counter()
        self.visits: list[tuple[str, str, dict[str, bytes]]] = []

    async def __call__(self, tree, main, work, _engine, _runner, **_kwargs):
        index = self.counts[main]
        self.counts[main] += 1
        self.visits.append((main, work.name, contents(tree)))
        trace = self.traces[main][index]
        work.mkdir(parents=True)
        pdf = work / "built.pdf"
        pdf.write_bytes(b"%PDF-controlled dependency packaging fixture\n")
        success = not (work.name == "archive-build" and main == self.fail_archive_main)
        result = BuildResult(
            success=success,
            pdf=pdf if success else None,
            findings=[]
            if success
            else [Finding("build.fixture", "Controlled archive rebuild failure", "error")],
            dependencies=set(trace or ()),
            submission_inputs=None if trace is None else set(trace),
            generated_reads=self.generated_reads,
        )
        if self.legacy_result:
            return SimpleNamespace(
                **{key: value for key, value in vars(result).items() if key != "submission_inputs"}
            )
        return result


class GeneratedBibliographyInputTests(unittest.TestCase):
    def test_only_regular_files_beside_the_main_file_with_its_name_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper").mkdir()
            for name in ("paper/main.bbl", "paper/main.run.xml", "main.bbl", "paper/other.bbl"):
                (root / name).write_text("generated\n")
            reads = frozenset({"main.bbl", "main.run.xml", "other.bbl"})
            self.assertEqual(
                generated_bibliography_inputs(root, "paper/main.tex", reads),
                {"paper/main.bbl", "paper/main.run.xml"},
            )
            self.assertEqual(
                generated_bibliography_inputs(root, "paper/main.tex", {"main.bbl"}),
                {"paper/main.bbl"},
            )
            self.assertEqual(generated_bibliography_inputs(root, "main.tex", reads), {"main.bbl"})
            (root / "paper/main.bbl").unlink()
            (root / "paper/main.bbl").symlink_to(root / "main.bbl")
            self.assertEqual(
                generated_bibliography_inputs(root, "paper/main.tex", reads),
                {"paper/main.run.xml"},
            )


class DependencyPackagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        self.source.mkdir()

    def write(self, path: str, value: str | bytes) -> None:
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode() if isinstance(value, str) else value)

    async def prepare(self, settings: Settings, build: RecordedBuild, name: str = "out"):
        original = contents(self.source)
        output = self.base / name
        with (
            patch("latexprep.core.build_project", side_effect=build.__call__),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            report = await run_job(JobRequest("prepare", self.source, settings, output))
        self.assertEqual(contents(self.source), original)
        return report, output

    def archive_contents(self, output: Path, document: str | None = None) -> dict[str, bytes]:
        root = output if document is None else output / document
        with zipfile.ZipFile(root / "submission.zip") as archive:
            return {
                entry.filename: archive.read(entry)
                for entry in archive.infolist()
                if not entry.is_dir()
            }

    def assert_released(self, report) -> None:
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.findings)

    def assert_dependency_blocked(self, report, output: Path) -> None:
        self.assertEqual(report.outcome, "blocked", report.findings)
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})
        diagnostics = [item for item in report.findings if item.rule == "package.dependencies"]
        self.assertTrue(diagnostics, report.findings)
        self.assertTrue(any(item.severity == "error" for item in diagnostics))
        self.assertTrue(all(item.code is None for item in diagnostics))

    async def test_existing_output_archive_is_rejected_before_any_build(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        output = self.base / "existing"
        output.mkdir()
        with zipfile.ZipFile(output / "submission.zip", "w") as archive:
            archive.writestr("previous.tex", "Previously prepared sources.\n")
        (output / "user-notes.bin").write_bytes(b"Existing data\x00must survive")
        previous = contents(output)
        original = contents(self.source)
        report = Report("prepare")
        with (
            patch("latexprep.core.build_project", new=AsyncMock()) as build,
            self.assertRaisesRegex(PreparationError, "Output already exists"),
        ):
            await run_job(
                JobRequest("prepare", self.source, Settings(main="main.tex"), output), report=report
            )
        build.assert_not_called()
        self.assertEqual(contents(output), previous)
        self.assertEqual(contents(self.source), original)
        self.assertEqual(report.artifacts, {})

    async def test_publication_race_preserves_the_other_archive_and_user_data(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        output = (self.base / "raced-output").resolve()
        original = contents(self.source)
        build = RecordedBuild({"main.tex": ({"main.tex"},) * 3})
        report = Report("prepare")
        mkdir = Path.mkdir
        raced_contents: dict[str, bytes] = {}

        def race_mkdir(path: Path, *args, **kwargs) -> None:
            if path == output and not raced_contents:
                self.assertEqual(build.counts["main.tex"], 3)
                mkdir(path, *args, **kwargs)
                with zipfile.ZipFile(path / "submission.zip", "w") as archive:
                    archive.writestr("previous.tex", "Another job's completed archive.\n")
                (path / "user-notes.bin").write_bytes(b"Another job's data\x00must survive")
                raced_contents.update(contents(path))
            mkdir(path, *args, **kwargs)

        with (
            patch("latexprep.core.build_project", side_effect=build.__call__),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            patch.object(Path, "mkdir", race_mkdir),
            self.assertRaisesRegex(PreparationError, "Output appeared while the job ran"),
        ):
            await run_job(
                JobRequest("prepare", self.source, Settings(main="main.tex"), output), report=report
            )
        self.assertTrue(raced_contents)
        self.assertEqual(contents(output), raced_contents)
        self.assertEqual(contents(self.source), original)
        self.assertEqual(build.counts["main.tex"], 3)
        self.assertEqual(report.artifacts, {})

    async def test_unused_files_do_not_ship_or_block_selected_transforms_without_cleanup(self):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n"
            "\\bibliography{references}\n\\end{document}\n",
        )
        self.write("sections/body.tex", "% private TODO note\nSelected \\cite{kept}.\n")
        self.write("references.bib", "@misc{kept,title={Kept}}\n@misc{unused,title={Unused}}\n")
        self.write("draft.tex", "\\documentclass{book}\n\\directlua{require('private')}\n")
        self.write("broken.tex", "\\input{")
        self.write("figures/unused.pdf", b"%PDF-unused\n")
        self.write("data/unused.csv", "private,unused\n")
        self.write("notes.txt", "Not an explicit deliverable.\n")
        selected = {"main.tex", "sections/body.tex", "references.bib"}
        build = RecordedBuild({"main.tex": (selected, selected, selected)})
        settings = Settings(
            cleanup=False,
            workflow=WorkflowOptions(
                documents=(DocumentOptions("paper", "main.tex", include=("*",)),)
            ),
            bibliography_transform=BibliographyTransformOptions(cited_only=True),
            source_transforms=SourceTransformOptions(comment_policy="private"),
        )
        report, output = await self.prepare(settings, build)
        self.assert_released(report)
        archived = self.archive_contents(output, "paper")
        self.assertEqual(set(archived), selected)
        self.assertNotIn(b"private TODO", archived["sections/body.tex"])
        self.assertNotIn(b"@misc{unused", archived["references.bib"])
        self.assertEqual(build.counts["main.tex"], 3)
        self.assertIn("draft.tex", build.visits[0][2])
        self.assertEqual(set(build.visits[1][2]), selected)
        self.assertEqual(build.visits[-1][2], archived)

    async def test_supplied_generated_bibliography_ships_when_the_build_consumed_it(self):
        self.write(
            "paper/main.tex",
            "\\documentclass{article}\n\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n"
            "\\begin{document}\n\\cite{kept}\n\\printbibliography\n\\end{document}\n",
        )
        self.write("paper/refs.bib", "@misc{kept,title={Kept}}\n")
        self.write("paper/main.bbl", "\\datalist[entry]{nty/global//global/global}\n")
        self.write("paper/main.run.xml", "<requests/>\n")
        self.write("paper/main.aux", "\\relax\n")
        self.write("old/main.bbl", "stale bibliography beside another file\n")
        selected = {"paper/main.tex", "paper/refs.bib"}
        # The build regenerates both files in its output directory and reads those.
        build = RecordedBuild(
            {"paper/main.tex": (selected, selected, selected)},
            generated_reads=frozenset({"main.bbl", "main.run.xml", "main.aux"}),
        )
        report, output = await self.prepare(Settings(main="paper/main.tex"), build)
        self.assert_released(report)
        retained = {"paper/main.bbl", "paper/main.run.xml"}
        archived = self.archive_contents(output)
        self.assertEqual(set(archived), selected | retained)
        self.assertEqual(archived["paper/main.bbl"], (self.source / "paper/main.bbl").read_bytes())
        self.assertEqual(build.visits[-1][2], archived)
        inventories = [
            item
            for item in report.findings
            if item.rule == "package.dependencies" and item.status == "passed"
        ]
        self.assertTrue(inventories, report.findings)
        for inventory in inventories:
            self.assertEqual(inventory.details["generated_bibliography"], sorted(retained))
            self.assertIn("paper/main.bbl", inventory.message)
        bbl = [item for item in report.findings if item.code == "TEX009"]
        self.assertTrue(bbl and all(item.status == "passed" for item in bbl), bbl)

    async def test_generated_bibliography_the_build_did_not_read_is_not_shipped(self):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\cite{kept}\n"
            "\\bibliography{refs}\n\\end{document}\n",
        )
        self.write("refs.bib", "@misc{kept,title={Kept}}\n")
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}\n")
        self.write("main.run.xml", "<requests/>\n")
        selected = {"main.tex", "refs.bib"}
        for reads, expected in (
            (frozenset(), selected),
            (frozenset({"main.bbl"}), selected | {"main.bbl"}),
        ):
            with self.subTest(reads=sorted(reads)):
                build = RecordedBuild(
                    {"main.tex": (selected, selected, selected)}, generated_reads=reads
                )
                report, output = await self.prepare(
                    Settings(main="main.tex"), build, name=f"out-{len(reads)}"
                )
                self.assert_released(report)
                self.assertEqual(set(self.archive_contents(output)), expected)

    async def test_static_and_recorded_inputs_preserve_transitive_local_resources(self):
        files = {
            "main.tex": "\\documentclass{styles/custom}\n\\begin{document}\n"
            "\\cite{kept}\n\\bibliography{bib/references}\n"
            "\\bibliographystyle{styles/custom}\n\\end{document}\n",
            "styles/custom.cls": "\\ProvidesClass{custom}\n\\LoadClass{article}\n"
            "\\RequirePackage{styles/helper}\n",
            "styles/helper.sty": "\\ProvidesPackage{helper}\n\\input{parts/body}\n",
            "parts/body.tex": "\\pgfplotstableread{data/table.csv}\\datatable\n"
            "\\setmainfont[Path={fonts/}]{local.otf}\n\\includegraphics{figures/plot.pdf}\n",
            "data/table.csv": "a,b\n1,2\n",
            "fonts/local.otf": "controlled font fixture",
            "fonts/runtime.enc": "controlled engine-only input",
            "figures/plot.pdf": "%PDF-controlled figure\n",
            "bib/references.bib": "@misc{kept,title={Kept}}\n",
            "styles/custom.bst": "ENTRY {}{}{}",
        }
        for path, value in files.items():
            self.write(path, value)
        self.write("other/data.csv", "unused\n")
        # Bibliography backend resources supplement the TeX recorder's inputs.
        observed = set(files) - {"bib/references.bib", "styles/custom.bst"}
        build = RecordedBuild({"main.tex": (observed, observed, observed)})
        report, output = await self.prepare(Settings(main="main.tex", engine="xelatex"), build)
        self.assert_released(report)
        archived = self.archive_contents(output)
        self.assertEqual(set(archived), set(files))
        self.assertEqual(archived, {path: value.encode() for path, value in files.items()})
        self.assertEqual(build.counts["main.tex"], 3)
        self.assertEqual(build.visits[-1][2], archived)

    async def test_missing_or_incomplete_build_evidence_cannot_be_ignored(self):
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        self.write("body.tex", "Body.\n")
        main = {"main.tex"}
        for name, traces, legacy in (
            ("no-recorder", (None,), False),
            ("empty-recorder", (set(),), False),
            ("missing-main", ({"body.tex"},), False),
            ("legacy-result", (main,), True),
            ("transformed-no-recorder", (main, None), False),
        ):
            with self.subTest(evidence=name):
                build = RecordedBuild({"main.tex": traces}, legacy_result=legacy)
                report, output = await self.prepare(
                    Settings(main="main.tex", checks=CheckSelection(ignore=("ALL",))), build, name
                )
                self.assert_dependency_blocked(report, output)
                self.assertEqual(build.counts["main.tex"], len(traces))

    async def test_unsupported_selected_dependencies_are_advisory_against_a_complete_trace(self):
        self.write("body.tex", "Body.\n")
        selected = {"main.tex", "body.tex"}
        for name, command in (
            ("dynamic", "\\newcommand{\\chosen}{body}\n\\input{\\chosen}\n"),
            ("lua", "\\directlua{tex.print('Body.')}\n"),
        ):
            with self.subTest(source=name):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                build = RecordedBuild({"main.tex": (selected,) * 3})
                report, output = await self.prepare(
                    Settings(main="main.tex", checks=CheckSelection(ignore=("ALL",))), build, name
                )
                self.assert_released(report)
                self.assertEqual(set(self.archive_contents(output)), selected)
                advisory = [
                    item
                    for item in report.findings
                    if item.rule == "package.dependencies" and item.status == "inconclusive"
                ]
                self.assertTrue(advisory, report.findings)
                self.assertTrue(all(item.severity == "warning" for item in advisory))
                self.assertEqual(build.counts["main.tex"], 3)
                self.assertEqual(build.visits[-1][2], self.archive_contents(output))

    async def test_unsupported_selected_dependencies_block_without_complete_trace(self):
        self.write("body.tex", "Body.\n")
        for name, command in (
            ("dynamic", "\\newcommand{\\chosen}{body}\n\\input{\\chosen}\n"),
            ("lua", "\\directlua{tex.print('Body.')}\n"),
        ):
            with self.subTest(source=name):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                build = RecordedBuild({"main.tex": (None,)})
                report, output = await self.prepare(
                    Settings(main="main.tex", checks=CheckSelection(ignore=("ALL",))), build, name
                )
                self.assert_dependency_blocked(report, output)
                self.assertEqual(build.counts["main.tex"], 1)

    async def test_consumed_merge_and_bibliography_inputs_are_removed_from_final_bundle(self):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{parts/body}\n\\end{document}\n",
        )
        self.write("parts/body.tex", "Body \\cite{kept}.\n\\bibliography{references}\n")
        self.write("references.bib", "@misc{kept,title={Reference}}\n")
        bibliography = (
            "\\begin{thebibliography}{1}\n\\bibitem{kept} Reference.\n\\end{thebibliography}\n"
        )
        self.write("generated/references.bbl", bibliography)
        initial = {"main.tex", "parts/body.tex"}
        final = {"main.tex"}
        build = RecordedBuild({"main.tex": (initial, final, final)})
        report, output = await self.prepare(
            Settings(
                main="main.tex",
                source_transforms=SourceTransformOptions(
                    merge_inputs=True, inline_bibliography="generated/references.bbl"
                ),
            ),
            build,
        )
        self.assert_released(report)
        archived = self.archive_contents(output)
        self.assertEqual(set(archived), final)
        self.assertIn(b"Body", archived["main.tex"])
        self.assertIn(bibliography.encode(), archived["main.tex"])
        self.assertNotIn(b"\\input", archived["main.tex"])
        self.assertNotIn(b"\\bibliography", archived["main.tex"])
        self.assertEqual(build.counts["main.tex"], 3)
        self.assertEqual(build.visits[-1][2], archived)

    async def test_explicit_deliverables_and_template_files_survive_flattening(self):
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        self.write("deliverables/cover.pdf", b"%PDF-explicit cover\n")
        self.write("notes/summary.txt", "Explicit text deliverable.\n")
        self.write("unused/summary.txt", "Unrelated colliding basename.\n")
        template = b"\\ProvidesPackage{custom}[2026/01/01 Example]\n"
        self.write("template/custom.sty", template)
        trusted = self.base / "trusted-custom.sty"
        trusted.write_bytes(template)
        build = RecordedBuild({"main.tex": ({"main.tex"},) * 4})
        report, output = await self.prepare(
            Settings(
                main="main.tex",
                layout="flat",
                submission_checks=SubmissionOptions(
                    required_deliverables=(
                        ("deliverables/cover.pdf", "pdf"),
                        ("notes/summary.txt", "text"),
                    ),
                    template_references=(("template/custom.sty", str(trusted)),),
                ),
            ),
            build,
        )
        self.assert_released(report)
        archived = self.archive_contents(output)
        self.assertEqual(set(archived), {"main.tex", "cover.pdf", "summary.txt", "custom.sty"})
        self.assertEqual(archived["cover.pdf"], b"%PDF-explicit cover\n")
        self.assertEqual(archived["summary.txt"], b"Explicit text deliverable.\n")
        self.assertEqual(archived["custom.sty"], template)
        self.assertEqual(build.counts["main.tex"], 4)
        self.assertEqual(build.visits[-1][2], archived)
        self.assertTrue(
            any(
                item.rule == "submission.deliverables" and item.status == "passed"
                for item in report.findings
            )
        )

    async def test_shared_candidate_pool_is_partitioned_and_archive_failure_blocks_all_outputs(
        self,
    ):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{first/body}\n\\end{document}\n",
        )
        self.write(
            "other.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{second/body}\n\\end{document}\n",
        )
        for name in ("first", "second"):
            self.write(
                f"{name}/body.tex", f"{name}.\n\\pgfplotstableread{{shared/table.csv}}\\data\n"
            )
        self.write("shared/table.csv", "a,b\n1,2\n")
        self.write("unused.csv", "Unused.\n")
        first = {"main.tex", "first/body.tex", "shared/table.csv"}
        second = {"other.tex", "second/body.tex", "shared/table.csv"}
        settings = Settings(
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex", include=("*",)),
                    DocumentOptions("supplement", "other.tex", include=("*",)),
                )
            )
        )
        for failed_main in (None, "other.tex"):
            with self.subTest(failed_archive=failed_main):
                build = RecordedBuild(
                    {"main.tex": (first,) * 3, "other.tex": (second,) * 3},
                    fail_archive_main=failed_main,
                )
                report, output = await self.prepare(
                    settings, build, "released" if failed_main is None else "blocked"
                )
                self.assertEqual(build.counts, {"main.tex": 3, "other.tex": 3})
                for main, selected in (("main.tex", first), ("other.tex", second)):
                    visits = [visit for visit in build.visits if visit[0] == main]
                    self.assertIn("unused.csv", visits[0][2])
                    self.assertEqual(set(visits[-1][2]), selected)
                if failed_main is None:
                    self.assert_released(report)
                    self.assertEqual(set(self.archive_contents(output, "paper")), first)
                    self.assertEqual(set(self.archive_contents(output, "supplement")), second)
                else:
                    self.assertEqual(report.outcome, "blocked", report.findings)
                    self.assertFalse(output.exists())
                    self.assertEqual(report.artifacts, {})


if __name__ == "__main__":
    unittest.main()
