"""Preparation selects inputs from explicit fake build traces and real source analysis."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.bbl_checks import BblOptions
from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.build_dependencies import SubmissionDependencies
from latexprep.build_result import BuildResult, BuildTools
from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError
from latexprep.job_context import JobRequest
from latexprep.report import Report
from latexprep.source_transform import SourceTransformOptions
from latexprep.submission_options import SubmissionOptions
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import RecordedBuild, controlled_backends, directory_contents


class GeneratedFileBuild:
    """A build backend that also generates files in its output folder and reports reading them.

    Attributes:
        inputs: The project inputs every build records.
        generated: The text of each generated file, by name in the output folder.
        calls: The workspace name and the options of each build.
        visits: The workspace name and the project contents of each build.
    """

    def __init__(self, inputs: set[str], generated: dict[str, str]) -> None:
        self.inputs = inputs
        self.generated = generated
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.visits: list[tuple[str, dict[str, bytes]]] = []

    async def __call__(self, tree, main, work, _engine, _runner, **options) -> BuildResult:
        self.calls.append((work.name, options))
        self.visits.append((work.name, directory_contents(tree)))
        output = work / "output"
        output.mkdir(parents=True)
        files = {}
        for name, text in self.generated.items():
            (output / name).write_text(text)
            files[name] = output / name
        pdf = output / "main.pdf"
        pdf.write_bytes(b"%PDF-controlled generated-file fixture\n")
        dependencies = SubmissionDependencies(
            set(self.inputs), generated_reads=frozenset(files), generated_files=files
        )
        return BuildResult(
            success=True,
            pdf=pdf,
            recorder_project_inputs=set(self.inputs),
            submission_inputs=set(self.inputs),
            tools=BuildTools(submission_dependencies=dependencies),
            generated_reads=frozenset(files),
        )


class DependencyPackagingTests(unittest.IsolatedAsyncioTestCase):
    """Which files a ``prepare`` job ships, and when missing build evidence blocks it."""

    async def asyncSetUp(self) -> None:
        """Create a temporary base directory with an empty ``input`` project folder."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        self.source.mkdir()

    def write(self, path: str, value: str | bytes) -> None:
        """Write a file of the source project, creating its parent directories.

        Args:
            path: The path of the file relative to the source project.
            value: The contents; text is encoded as UTF-8.
        """
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode() if isinstance(value, str) else value)

    async def prepare(self, settings: Settings, build: RecordedBuild, name: str = "out"):
        """Run a ``prepare`` job on the source project with a recorded build.

        Asserts that the job leaves the source project unchanged.

        Args:
            settings: The settings of the job.
            build: The recorded build that stands in for TeX.
            name: The name of the output directory in the base directory.

        Returns:
            The report of the job and the path of its output directory.
        """
        original = directory_contents(self.source)
        output = self.base / name
        backends = controlled_backends(build=build.__call__)
        report = await run_job(
            JobRequest("prepare", self.source, settings, output), backends=backends
        )
        self.assertEqual(directory_contents(self.source), original)
        return report, output

    def archive_contents(self, output: Path, document: str | None = None) -> dict[str, bytes]:
        """Read every file entry of a published ``submission.zip``.

        Args:
            output: The output directory of the job.
            document: The subdirectory of one document's package, or ``None`` for an
                archive directly in ``output``.

        Returns:
            The bytes of each file entry by its archive name; directories are left out.
        """
        root = output if document is None else output / document
        with zipfile.ZipFile(root / "submission.zip") as archive:
            return {
                entry.filename: archive.read(entry)
                for entry in archive.infolist()
                if not entry.is_dir()
            }

    def assert_released(self, report) -> None:
        """Assert that the outcome of ``report`` released the package."""
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.findings)

    def assert_dependency_blocked(self, report, output: Path) -> None:
        """Assert that a mandatory ``package.dependencies`` error blocked the job.

        Args:
            report: The report of the job.
            output: The output directory, which must not exist.
        """
        self.assertEqual(report.outcome, "blocked", report.findings)
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})
        diagnostics = [item for item in report.findings if item.rule == "package.dependencies"]
        self.assertTrue(diagnostics, report.findings)
        self.assertTrue(any(item.severity == "error" for item in diagnostics))
        # Ignoring every check does not remove the finding: PKG302 is mandatory.
        self.assertTrue(all(item.code == "PKG302" for item in diagnostics))

    async def test_existing_output_archive_is_rejected_before_any_build(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        output = self.base / "existing"
        output.mkdir()
        with zipfile.ZipFile(output / "submission.zip", "w") as archive:
            archive.writestr("previous.tex", "Previously prepared sources.\n")
        (output / "user-notes.bin").write_bytes(b"Existing data\x00must survive")
        previous = directory_contents(output)
        original = directory_contents(self.source)
        report = Report("prepare")
        build = AsyncMock()
        backends = controlled_backends(build=build)
        with self.assertRaisesRegex(PreparationError, "Output already exists"):
            await run_job(
                JobRequest("prepare", self.source, Settings(main="main.tex"), output),
                report=report,
                backends=backends,
            )
        build.assert_not_called()
        self.assertEqual(directory_contents(output), previous)
        self.assertEqual(directory_contents(self.source), original)
        self.assertEqual(report.artifacts, {})

    async def test_publication_race_preserves_the_other_archive_and_user_data(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        output = (self.base / "raced-output").resolve()
        original = directory_contents(self.source)
        build = RecordedBuild({"main.tex": ({"main.tex"},) * 2})
        report = Report("prepare")
        mkdir = Path.mkdir
        raced_contents: dict[str, bytes] = {}

        def race_mkdir(path: Path, *args, **kwargs) -> None:
            if path == output and not raced_contents:
                self.assertEqual(build.counts["main.tex"], 2)
                mkdir(path, *args, **kwargs)
                with zipfile.ZipFile(path / "submission.zip", "w") as archive:
                    archive.writestr("previous.tex", "Another job's completed archive.\n")
                (path / "user-notes.bin").write_bytes(b"Another job's data\x00must survive")
                raced_contents.update(directory_contents(path))
            mkdir(path, *args, **kwargs)

        backends = controlled_backends(build=build.__call__)
        with (
            patch.object(Path, "mkdir", race_mkdir),
            self.assertRaisesRegex(PreparationError, "Output appeared while the job ran"),
        ):
            await run_job(
                JobRequest("prepare", self.source, Settings(main="main.tex"), output),
                report=report,
                backends=backends,
            )
        self.assertTrue(raced_contents)
        self.assertEqual(directory_contents(output), raced_contents)
        self.assertEqual(directory_contents(self.source), original)
        self.assertEqual(build.counts["main.tex"], 2)
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

    async def test_generated_files_ship_from_the_verified_build_for_services_without_the_tools(
        self,
    ):
        """Ship the index the build generated always, and its .bbl only where it is needed.

        arXiv runs neither makeindex nor makeglossaries, so the index the verified build
        generated ships beside the main file; the supplied .bbl does not ship while the
        venue runs BibTeX itself. Where it does not, the build's .bbl ships instead of the
        stale supplied one. The archive rebuild then runs without the tools whose files
        are shipped, and so proves that they suffice.
        """
        self.write(
            "paper/main.tex",
            "\\documentclass{article}\n\\usepackage{makeidx}\n\\makeindex\n"
            "\\begin{document}\nText\\index{text} \\cite{kept}.\n"
            "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\printindex\n\\end{document}\n",
        )
        self.write("paper/refs.bib", "@misc{kept,title={Kept}}\n")
        self.write("paper/main.bbl", "stale supplied bibliography\n")
        self.write("paper/main.ind", "stale supplied index\n")
        selected = {"paper/main.tex", "paper/refs.bib"}
        generated = {"main.bbl": "generated bibliography\n", "main.ind": "generated index\n"}
        for venue_runs_tools, shipped in ((True, {".ind"}), (False, {".bbl", ".ind"})):
            with self.subTest(venue_runs_bibliography_tools=venue_runs_tools):
                build = GeneratedFileBuild(selected, generated)
                settings = Settings(
                    main="paper/main.tex",
                    bbl_checks=BblOptions(venue_runs_bibliography_tools=venue_runs_tools),
                )
                report, output = await self.prepare(settings, build, name=f"out-{venue_runs_tools}")
                self.assert_released(report)
                archived = self.archive_contents(output)
                expected = {"paper/main.tex", "paper/refs.bib", "paper/main.ind"}
                if not venue_runs_tools:
                    expected.add("paper/main.bbl")
                    self.assertEqual(archived["paper/main.bbl"], b"generated bibliography\n")
                self.assertEqual(set(archived), expected)
                self.assertEqual(archived["paper/main.ind"], b"generated index\n")
                # Only the archive rebuild keeps the shipped files as they are.
                kept = {name: options.get("kept_generated") for name, options in build.calls}
                self.assertEqual(kept["archive-build"], frozenset(shipped))
                self.assertEqual({name for name, value in kept.items() if value}, {"archive-build"})
                self.assertEqual(build.visits[-1][1], archived)
                selections = [
                    item
                    for item in report.findings
                    if item.rule == "package.dependencies" and item.status == "passed"
                ]
                self.assertTrue(selections, report.findings)
                for selection in selections:
                    self.assertIn("paper/main.ind", selection.details["generated_files"])
                    self.assertIn("generated by the verified build", selection.message)
                lists = [item for item in report.findings if item.code == "TEX012"]
                self.assertTrue(lists and lists[-1].status == "passed", lists)

    async def test_a_supplied_bbl_is_not_shipped_where_the_venue_runs_bibtex(self):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\cite{kept}\n"
            "\\bibliography{refs}\n\\end{document}\n",
        )
        self.write("refs.bib", "@misc{kept,title={Kept}}\n")
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}\n")
        self.write("main.run.xml", "<requests/>\n")
        selected = {"main.tex", "refs.bib"}
        for reads in (frozenset(), frozenset({"main.bbl"})):
            with self.subTest(reads=sorted(reads)):
                build = RecordedBuild(
                    {"main.tex": (selected, selected, selected)}, generated_reads=reads
                )
                report, output = await self.prepare(
                    Settings(main="main.tex"), build, name=f"out-{len(reads)}"
                )
                self.assert_released(report)
                self.assertEqual(set(self.archive_contents(output)), selected)

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
        build = RecordedBuild({"main.tex": (observed, observed)})
        report, output = await self.prepare(Settings(main="main.tex", engine="xelatex"), build)
        self.assert_released(report)
        archived = self.archive_contents(output)
        self.assertEqual(set(archived), set(files))
        self.assertEqual(archived, {path: value.encode() for path, value in files.items()})
        self.assertEqual(build.counts["main.tex"], 2)
        self.assertEqual(build.visits[-1][2], archived)

    async def test_missing_or_incomplete_build_evidence_cannot_be_ignored(self):
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        self.write("body.tex", "Body.\n")
        main = {"main.tex"}
        # The build result always declares submission_inputs; None is absent evidence.
        for name, traces in (
            ("no-recorder", (None,)),
            ("empty-recorder", (set(),)),
            ("missing-main", ({"body.tex"},)),
            ("transformed-no-recorder", (main, None)),
        ):
            with self.subTest(evidence=name):
                build = RecordedBuild({"main.tex": traces})
                report, output = await self.prepare(
                    Settings(main="main.tex", checks=CheckSelection(ignore=("ALL",))), build, name
                )
                self.assert_dependency_blocked(report, output)
                self.assertEqual(build.counts["main.tex"], len(traces))

    async def test_incomplete_evidence_names_its_reason_in_the_blocking_finding(self):
        """Quote the dependency uncertainty in PKG302, so the terminal shows why it blocks."""
        self.write("main.tex", "\\documentclass{article}\n\\begin{document}Body.\\end{document}\n")
        reason = "Dependency rule 'bibtex main' did not complete successfully"

        async def build(tree, main, work, _engine, _runner, **_options):
            """Build a PDF whose dependency evidence is incomplete for ``reason``."""
            work.mkdir(parents=True)
            pdf = work / "built.pdf"
            pdf.write_bytes(b"%PDF-controlled incomplete evidence\n")
            dependencies = SubmissionDependencies(None, uncertainty=(reason,))
            return BuildResult(
                success=True,
                pdf=pdf,
                recorder_project_inputs={"main.tex"},
                tools=BuildTools(submission_dependencies=dependencies),
            )

        report, output = await self.prepare(Settings(main="main.tex"), build)
        self.assert_dependency_blocked(report, output)
        blocking = [item for item in report.findings if item.code == "PKG302" and item.is_blocking]
        self.assertTrue(blocking, report.findings)
        for finding in blocking:
            self.assertIn(reason, finding.message)
            self.assertEqual(finding.details["uncertainty"], [reason])

    async def test_unsupported_selected_dependencies_are_advisory_against_a_complete_trace(self):
        self.write("body.tex", "Body.\n")
        selected = {"main.tex", "body.tex"}
        for name, command in (
            ("dynamic", "\\newcommand{\\chosen}{body}\n\\input{\\chosen}\n"),
            ("lua", "\\directlua{tex.print('Body.')}\n"),
        ):
            with self.subTest(source=name):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                build = RecordedBuild({"main.tex": (selected,) * 2})
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
                self.assertEqual(build.counts["main.tex"], 2)
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
        build = RecordedBuild({"main.tex": ({"main.tex"},) * 3})
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
        # Baseline, prepared (flat) and archive: staging changed nothing, so there is no
        # transformed rebuild.
        self.assertEqual(build.counts["main.tex"], 3)
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
                    {"main.tex": (first,) * 2, "other.tex": (second,) * 2},
                    fail_archive_main=failed_main,
                )
                report, output = await self.prepare(
                    settings, build, "released" if failed_main is None else "blocked"
                )
                self.assertEqual(build.counts, {"main.tex": 2, "other.tex": 2})
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
