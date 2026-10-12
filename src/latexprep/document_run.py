"""One project's run: import, analysis, baseline, staged transformations and a verified package.

``run_project`` imports the input and dispatches single-purpose commands;
``DocumentRun`` carries one selected document through its stages.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from .build_checks import (
    check_build_details,
    check_local_package_shadows,
    check_tex_distribution,
    qualify_citation_findings,
)
from .config import Settings
from .document_tasks import (
    baseline_build_task,
    baseline_pdf_tasks,
    build_document,
    compare_documents,
    final_verification_tasks,
    prepared_submission_options,
    source_analysis_tasks,
)
from .formatting import format_project
from .job_context import (
    JobCommand,
    JobContext,
    is_blocked,
    job_outcome,
    main_selection_error,
    names_project_file,
    record_findings,
)
from .latexmk_build import BuildResult
from .metadata_privacy import check_sanitized_pdf_metadata
from .models import Change, Finding, Outcome, Report, StageRecord, TaskName, has_blockers
from .package_inputs import generated_bibliography_inputs, select_package_inputs
from .package_inventory import PackageStatus, assemble_package_inventory
from .project import ImportLimits, create_archive, import_project
from .publication import VerifiedPackage
from .scheduler import Scheduler, Task, TaskResult, bounded_map
from .source import discover_roots
from .source_staging import (
    apply_contents,
    flatten_sources,
    inline_bibliography_files,
    transform_sources,
)
from .standalone_checks import check_bibliography, check_formatting, check_standalone_pdf
from .submission_checks import check_submission, check_submission_archive
from .toolchain_help import explain_missing_toolchain


def _retained_files(settings: Settings) -> set[str]:
    """Files shipped although the build does not read them.

    Uses the requested (not check-filtered) settings: deselecting PKG104/PKG107
    removes their presence checks but never changes what the package contains.
    """
    return {
        *settings.package.include,
        *(name for name, _ in settings.submission_checks.required_deliverables),
        *(name for name, _ in settings.submission_checks.template_references),
    }


async def select_main(context: JobContext, report: Report, snapshot: Path) -> str | None:
    """The configured main file or the only candidate root; a finding explains no choice."""
    selected = context.settings.main
    roots = (
        []
        if selected
        else await context.thread_operation("discover roots", discover_roots, snapshot)
    )
    if selected is None and len(roots) == 1:
        selected = roots[0]
    if selected is None:
        report.findings.append(
            Finding(
                "project.main_selection",
                f"Select --main explicitly; found {len(roots)} candidate roots: {', '.join(roots)}",
                severity="error",
                status="inconclusive",
                suggestion=(
                    "Rerun with --main set to one of the candidate roots, or set main in the "
                    "configuration."
                    if roots
                    else "Check that INPUT is the paper's source folder or ZIP; no file with "
                    "\\documentclass was found, so name the main file with --main."
                ),
            )
        )
        return None
    if not names_project_file(snapshot, selected):
        roots = await context.thread_operation("discover roots", discover_roots, snapshot)
        raise main_selection_error(selected, roots)
    return Path(selected).as_posix()


async def run_project(context: JobContext, report: Report) -> VerifiedPackage | None:
    """Run the request on one project; return the package when it is ready to publish."""
    request, settings = context.request, context.settings
    if request.command is JobCommand.PDF:
        await check_standalone_pdf(context, report)
        return None
    limits = ImportLimits(
        max_files=settings.max_files, max_bytes=settings.max_bytes, max_depth=settings.max_depth
    )
    context.emit("Importing a separate project snapshot")
    imported = await context.thread_operation(
        "import project", import_project, request.source, context.work / "import", limits
    )
    report.findings.extend(imported.findings)
    report.changes.extend(imported.changes)
    reference = None
    if request.reference_pdf:
        reference = await context.thread_operation(
            "snapshot supplied PDF",
            context.runner.freeze_input,
            request.reference_pdf,
            context.work / "reference-input",
        )
    if request.command is JobCommand.BIBLIOGRAPHY:
        await check_bibliography(context, report, imported.root)
        return None
    if request.command is JobCommand.FORMAT:
        await check_formatting(context, report, imported.root)
        return None
    main = report.main = await select_main(context, report, imported.root)
    run = DocumentRun(
        context,
        report,
        imported=imported.root,
        reference=reference,
        main=main,
        retained=_retained_files(request.settings),
    )
    return await run.run()


class InputSelection(NamedTuple):
    """The inputs copied for preparation, with the selection's changes and evidence."""

    root: Path
    changes: list[Change]
    findings: list[Finding]


class _RepeatedBaseline(NamedTuple):
    number: int
    build: BuildResult
    comparison: list[Finding]


@dataclass(frozen=True, slots=True)
class ShippedSources:
    """Selected and possibly flattened sources, rebuilt and matched against the baseline."""

    root: Path
    main: str
    build: BuildResult
    # Source-to-package names of a flattening; empty for the preserved layout.
    path_mapping: dict[str, str]


@dataclass
class DocumentRun:
    """The changing state of one document's run; each stage is a method."""

    context: JobContext
    report: Report
    _: KW_ONLY
    # The imported project, which the baseline builds and which input selection copies from.
    imported: Path
    # The frozen copy of a supplied reference PDF.
    reference: Path | None
    # The selected main file; None when no main could be selected.
    main: str | None
    retained: set[str]
    completed: dict[str, TaskResult] = field(default_factory=dict)
    input_inventories: list[list[dict]] = field(default_factory=list)
    scheduler: Scheduler = field(init=False)

    def __post_init__(self) -> None:
        self.scheduler = Scheduler(self.context.budget, self.context.emit, self._collect)

    @property
    def settings(self) -> Settings:
        return self.context.settings

    @property
    def command(self) -> JobCommand:
        return self.context.request.command

    def analysis_snapshot(self) -> Path:
        """The selected preparation inputs once their selection succeeded, else the import."""
        selection = self.completed.get(TaskName.SELECT_PREPARATION_INPUTS)
        if selection is not None and selection.status == "succeeded":
            return selection.value.root
        return self.imported

    def baseline_build(self) -> BuildResult:
        return self.completed[TaskName.BASELINE_BUILD].value

    def baseline_pdf(self) -> Path:
        pdf = self.baseline_build().pdf
        # Dependents start only after the baseline build succeeded with a PDF.
        assert pdf is not None
        return pdf

    async def run(self) -> VerifiedPackage | None:
        report, settings = self.report, self.settings
        results = await self.scheduler.execute(self.analysis_tasks())
        if self.command is JobCommand.INSPECT:
            report.scope = "source and bibliography checks; compilation and PDF checks not run"
            report.outcome = job_outcome(report, settings)
            return None
        report.scope = "selected source, bibliography, privacy, isolated build and PDF checks"
        baseline_result = results.get(TaskName.BASELINE_BUILD)
        baseline = baseline_result.value if baseline_result else None
        if baseline is None or not baseline.success or baseline.pdf is None or self.main is None:
            return self._stop()
        if settings.workflow.baseline_runs > 1 and not is_blocked(report, settings):
            await self.repeat_baselines(baseline, self.main)
        if self.command is JobCommand.CHECK or is_blocked(report, settings):
            report.outcome = job_outcome(report, settings)
            self._note_online_checks_not_reached()
            return None
        package = await self.prepare(baseline, self.main)
        if package is None and report.outcome != Outcome.PLANNED:
            return self._stop()
        return package

    def _stop(self) -> None:
        self.report.outcome = Outcome.BLOCKED
        self._note_online_checks_not_reached()

    def analysis_tasks(self) -> list[Task]:
        """Source, bibliography, privacy, baseline build and baseline PDF tasks."""
        sources = source_analysis_tasks(self.context, self.analysis_snapshot, self.main)
        if self.main is None or self.command is JobCommand.INSPECT:
            return sources
        baseline = baseline_build_task(self.context, self.imported, self.main)
        checks = [
            *sources,
            *baseline_pdf_tasks(
                self.context,
                self.baseline_build,
                self.baseline_pdf,
                self.analysis_snapshot,
                self.main,
                self.reference,
            ),
        ]
        if self.command is not JobCommand.PREPARE:
            return [baseline, *checks]
        main = self.main
        selection = Task(
            TaskName.SELECT_PREPARATION_INPUTS,
            lambda: self._select_preparation_inputs(main),
            needs_success_of=(TaskName.BASELINE_BUILD,),
            cpu=0,
            memory_mb=0,
            is_success=lambda selected: not has_blockers(selected.findings),
        )
        # Wait for selection, but still analyze the full snapshot if it fails.
        waiting = [
            replace(task, waits_for=(*task.waits_for, TaskName.SELECT_PREPARATION_INPUTS))
            for task in checks
        ]
        return [baseline, *waiting, selection]

    async def _select_preparation_inputs(self, main: str) -> InputSelection:
        destination = self.context.work / "selected-inputs"
        baseline = self.baseline_build()
        changes, findings = await self.context.thread_operation(
            "select preparation inputs",
            select_package_inputs,
            self.imported,
            destination,
            main,
            # Absent evidence (None) is a blocking diagnostic in select_package_inputs.
            baseline.submission_inputs,
            self.retained | inline_bibliography_files(self.settings),
            baseline.generated_reads,
        )
        return InputSelection(destination, changes, findings)

    def _collect(self, result: TaskResult) -> None:
        report = self.report
        self.completed[result.name] = result
        if result.name == TaskName.BASELINE_BUILD and result.value is not None:
            stage = self._record_build(result.value, "baseline")
            stage["elapsed_seconds"] = result.elapsed_seconds
            stage["queue_seconds"] = result.queue_seconds
            return
        report.stages.append(
            {
                "name": result.name,
                "status": result.status,
                "elapsed_seconds": result.elapsed_seconds,
                "queue_seconds": result.queue_seconds,
            }
        )
        if result.name == TaskName.SELECT_PREPARATION_INPUTS and result.value is not None:
            self._collect_input_selection(result.value)
        elif result.status == "blocked":
            # The failed prerequisite already reports its own blocking finding.
            report.findings.append(
                Finding(
                    "execution.task",
                    f"{result.name} was not run: {result.error}",
                    severity="info",
                    status="skipped",
                    details={"task": result.name, "blocked_by": list(result.blocked_by)},
                )
            )
        elif result.status != "succeeded":
            report.findings.append(
                Finding(
                    "execution.task",
                    f"{result.name}: {result.error}",
                    severity="error",
                    status="inconclusive",
                )
            )
        else:
            _SUCCESS_COLLECTORS.get(result.name, DocumentRun._collect_stage_findings)(self, result)

    def _collect_stage_findings(self, result: TaskResult) -> None:
        record_findings(self.report, result.value, result.name)

    def _collect_unstaged_findings(self, result: TaskResult) -> None:
        self.report.findings.extend(result.value)

    def _collect_source_analysis(self, result: TaskResult) -> None:
        findings = result.value.findings
        if any(item.rule == "project.main_selection" for item in self.report.findings):
            # Root selection already reported the same ambiguity with its candidates.
            findings = [
                item
                for item in findings
                if item.rule not in {"source.root_ambiguous", "source.root_missing"}
            ]
        self.report.findings.extend(findings)

    def _collect_input_selection(self, selection: InputSelection) -> None:
        self.report.changes.extend(selection.changes)
        findings = self.selection_evidence(selection.findings)
        # Record the inventory from this selection's evidence, without duplicating findings.
        self.record_inventory("incomplete" if has_blockers(findings) else "proposed")
        record_findings(self.report, findings, TaskName.SELECT_PREPARATION_INPUTS.value)

    def selection_evidence(self, findings: list[Finding]) -> list[Finding]:
        """Keep intermediate rows once; publish the reconciled inventory in the report."""
        result = []
        for finding in findings:
            inventory = finding.details.get("inventory")
            if isinstance(inventory, list):
                self.input_inventories.append(inventory)
                finding = replace(
                    finding,
                    details={
                        key: value for key, value in finding.details.items() if key != "inventory"
                    },
                )
            result.append(finding)
        return result

    def record_inventory(
        self,
        status: PackageStatus,
        *,
        final_root: Path | None = None,
        path_mapping: dict[str, str] | None = None,
        archive: Path | None = None,
    ) -> None:
        self.report.package_contents = [
            assemble_package_inventory(
                selections=self.input_inventories,
                changes=self.report.changes,
                source_root=self.imported,
                final_root=final_root,
                path_mapping=path_mapping,
                archive=archive,
                document=self.context.request.document_name or self.main,
                status=status,
            )
        ]

    def _record_build(self, result: BuildResult, stage: str) -> StageRecord:
        """Record a build's findings, tools and stage; return the stage record."""
        report, settings = self.report, self.settings
        findings = explain_missing_toolchain(
            qualify_citation_findings(result.findings, build_succeeded=result.success),
            settings.engine,
        )
        toolchain_missing = any("missing_tools" in finding.details for finding in findings)
        record_findings(report, findings, stage)
        record_findings(report, check_build_details(result, settings.build_checks), stage)
        if stage == "baseline" and not toolchain_missing:
            # One observation per document: later builds use the same toolchain. Without
            # TeX the missing-toolchain finding already explains the absent version.
            record_findings(report, [check_tex_distribution(result, settings.build_checks)], stage)
        report.tools.update(result.tools.to_report())
        built = result.success and result.pdf is not None
        record: StageRecord = {
            "name": stage,
            "status": "succeeded" if built else "failed",
            "command": result.command,
        }
        report.stages.append(record)
        # A missing toolchain is already reported once; a failed-build finding would repeat it.
        if not built and not toolchain_missing:
            report.findings.append(
                Finding(
                    f"build.{stage}",
                    f"Required {stage} build did not succeed",
                    severity="error",
                    status="inconclusive",
                )
            )
        return record

    async def repeat_baselines(self, baseline: BuildResult, main: str) -> None:
        """Rebuild the unchanged baseline and require identical evidence (CMP101)."""
        context, settings, report = self.context, self.settings, self.report
        first_pdf = baseline.pdf
        assert first_pdf is not None

        async def attempt(number: int) -> _RepeatedBaseline:
            async with context.budget.lease(
                f"repeat baseline {number}", memory_mb=settings.build_memory_mb, kind="build"
            ):
                repeated = await build_document(context, self.imported, main, f"baseline-{number}")
            if not repeated.success or repeated.pdf is None:
                return _RepeatedBaseline(number, repeated, [])
            compared = await compare_documents(
                context,
                first_pdf,
                repeated.pdf,
                f"compare-baseline-{number}",
                f"baseline repeat {number}",
            )
            return _RepeatedBaseline(number, repeated, compared)

        # Independent repeats overlap up to the configured build concurrency.
        attempts = await bounded_map(
            list(range(2, settings.workflow.baseline_runs + 1)),
            attempt,
            limit=1 if context.request.pdf_previews is not None else settings.build_jobs,
        )
        stable = True
        for number, repeated, compared in attempts:
            self._record_build(repeated, f"baseline repeat {number}")
            record_findings(report, compared, f"baseline repeat {number}")
            if not repeated.success or repeated.pdf is None or has_blockers(compared):
                stable = False
        report.findings.append(
            Finding(
                "compare.baseline_stability",
                f"{settings.workflow.baseline_runs} independent baseline builds match."
                if stable
                else "Baseline builds are inconsistent or could not be fully compared.",
                severity="info" if stable else "error",
                status="passed" if stable else "failed",
                details={
                    "requested_builds": settings.workflow.baseline_runs,
                    "comparison": "exact page count, normalized extracted text and 144-DPI pixels",
                },
            )
        )

    async def build_stage(self, tree: Path, main: str, directory: str, stage: str) -> BuildResult:
        """Build one staged tree under a build lease and record it like the baseline."""
        context, settings, report = self.context, self.settings, self.report
        context.emit(f"Rebuilding {stage} sources")
        async with context.budget.lease(
            f"{stage} build", memory_mb=settings.build_memory_mb, kind="build"
        ) as lease:
            started = time.monotonic()
            result = await build_document(context, tree, main, directory)
            elapsed = time.monotonic() - started
        record = self._record_build(result, stage)
        # Like the scheduled baseline build, so every build's time appears in the report.
        record["elapsed_seconds"] = round(elapsed, 6)
        record["queue_seconds"] = round(lease.queue_seconds, 6)
        context.emit(f"{stage} build: {record['status']} ({elapsed:.1f}s)")
        if result.success and result.submission_inputs is None:
            record_findings(
                report,
                [
                    Finding(
                        "package.dependencies",
                        "Complete build dependency evidence is unavailable.",
                        severity="error",
                        status="inconclusive",
                        suggestion="Resolve the submission dependency diagnostic and retry.",
                    )
                ],
                stage,
            )
        if settings.build_checks.check_local_package_shadows:
            record_findings(
                report,
                await check_local_package_shadows(
                    result, context.work / f"shadows-{stage}", context.runner, budget=context.budget
                ),
                stage,
            )
        return result

    async def prepare(self, baseline: BuildResult, main: str) -> VerifiedPackage | None:
        report = self.report
        prepared = await self.stage_transformations(baseline, main)
        if prepared is None:
            return None
        if self.context.request.dry_run:
            self.record_inventory("proposed")
            report.scope = (
                "preparation plan; baseline inputs selected and transformations previewed; "
                "final input selection, layout and bundle not verified"
            )
            report.outcome = Outcome.PLANNED
            return None
        shipped = await self.ship(prepared, main)
        if shipped is None:
            return None
        package = await self.package_and_verify(shipped)
        report.outcome = job_outcome(report, self.settings)
        return package

    async def stage_transformations(self, baseline: BuildResult, main: str) -> Path | None:
        """Apply the selected transformations and formatting to a staging copy."""
        context, settings, report = self.context, self.settings, self.report
        context.emit("Planning and applying selected transformations in staging")
        snapshot = self.analysis_snapshot()
        dependencies = set(baseline.recorder_project_inputs)
        analysis = self.completed[TaskName.SOURCE_CHECKS].value
        if analysis:
            dependencies.update(analysis.dependencies)
        # Cleanup must not remove a selected generated bibliography (.run.xml is otherwise
        # rebuildable debris); the build reads the regenerated copy, not the supplied one.
        dependencies.update(generated_bibliography_inputs(snapshot, main, baseline.generated_reads))
        transformed = await context.thread_operation(
            "transform sources",
            transform_sources,
            snapshot,
            context.work,
            main,
            settings,
            dependencies,
            self.retained,
        )
        report.changes.extend(transformed.changes)
        report.findings.extend(transformed.findings)
        prepared = transformed.prepared
        if settings.apply_formatting and not is_blocked(report, settings):
            formatted = await format_project(
                prepared,
                context.work / "format-prepare",
                context.runner,
                settings.formatting_options,
                budget=context.budget,
            )
            report.tools.update(formatted.tools)
            report.findings.extend(
                finding for finding in formatted.findings if finding.rule != "source.formatting"
            )
            report.changes.extend(formatted.changes)
            if not has_blockers(formatted.findings):
                await context.thread_operation(
                    "apply formatting",
                    apply_contents,
                    prepared,
                    formatted.contents,
                    originals=formatted.originals,
                )
        return None if is_blocked(report, settings) else prepared

    async def ship(self, prepared: Path, main: str) -> ShippedSources | None:
        """Rebuild staging, select the shipped inputs, flatten and compare with the baseline."""
        context, settings, report = self.context, self.settings, self.report
        staged = await self.build_stage(prepared, main, "transformed-build", "transformed")
        if not staged.success or staged.pdf is None:
            return None
        context.emit("Selecting the transformed document's inputs for submission")
        shipment = context.work / "shipment"
        changes, findings = await context.thread_operation(
            "select submission inputs",
            select_package_inputs,
            prepared,
            shipment,
            main,
            staged.submission_inputs,
            self.retained,
            staged.generated_reads,
        )
        report.changes.extend(changes)
        record_findings(report, self.selection_evidence(findings), "select submission inputs")
        if is_blocked(report, settings):
            return None
        flattened = await context.thread_operation(
            "flatten selected inputs", flatten_sources, shipment, context.work, main, settings
        )
        report.changes.extend(flattened.changes)
        report.findings.extend(flattened.findings)
        blocked = is_blocked(report, settings)
        self.record_inventory(
            "incomplete" if blocked else "prepared",
            final_root=flattened.root,
            path_mapping=flattened.path_mapping,
        )
        if blocked:
            return None
        build = staged
        if settings.layout == "flat":
            build = await self.build_stage(
                flattened.root, flattened.main, "staged-build", "prepared"
            )
        pdf = build.pdf
        if not build.success or pdf is None:
            return None
        record_findings(
            report,
            await compare_documents(
                context,
                self.baseline_pdf(),
                pdf,
                "compare-prepared",
                "baseline versus prepared",
                options=settings.workflow.comparison,
            ),
            "baseline versus prepared",
        )
        if is_blocked(report, settings):
            return None
        return ShippedSources(flattened.root, flattened.main, build, flattened.path_mapping)

    async def package_and_verify(self, shipped: ShippedSources) -> VerifiedPackage | None:
        """Write the archive, rebuild a fresh extraction and run the final checks."""
        context, settings, report = self.context, self.settings, self.report
        submission = prepared_submission_options(settings, shipped.path_mapping)
        record_findings(
            report,
            await context.thread_operation(
                "prepared submission checks",
                check_submission,
                shipped.root,
                shipped.main,
                submission,
            ),
            "prepared submission checks",
        )
        if is_blocked(report, settings):
            return None
        context.emit("Packaging sources and rebuilding a fresh extraction of the exact ZIP")
        archive = context.work / "submission.zip"
        await context.thread_operation("create archive", create_archive, shipped.root, archive)
        self.record_inventory(
            "prepared", final_root=shipped.root, path_mapping=shipped.path_mapping, archive=archive
        )
        record_findings(
            report,
            await context.thread_operation(
                "compressed archive policy",
                check_submission_archive,
                archive,
                settings.submission_checks,
            ),
            "archive",
        )
        if is_blocked(report, settings):
            return None
        if archive.stat().st_size > settings.max_bytes:
            report.findings.append(
                Finding(
                    "package.archive_limit",
                    f"Prepared archive has {archive.stat().st_size} bytes; max_bytes is "
                    f"{settings.max_bytes}. It cannot be re-extracted for verification.",
                    severity="error",
                    status="failed",
                    suggestion="Reduce the shipped files or raise max_bytes explicitly.",
                )
            )
            return None
        # The archive is our own bounded output; preserve byte/count limits without
        # rejecting legitimate highly compressible sources on ratio alone.
        final_limits = ImportLimits(
            max_files=settings.max_files,
            max_bytes=settings.max_bytes,
            max_depth=settings.max_depth,
            max_ratio=settings.max_bytes,
        )
        extracted = await context.thread_operation(
            "extract archive",
            import_project,
            archive,
            context.work / "extracted",
            final_limits,
            unwrap=False,
            exclude_metadata=False,
        )
        final = await self.build_stage(extracted.root, shipped.main, "archive-build", "archive")
        staged_pdf, final_pdf = shipped.build.pdf, final.pdf
        if not final.success or final_pdf is None:
            return None
        assert staged_pdf is not None
        await self.scheduler.execute(
            final_verification_tasks(
                context,
                extracted.root,
                shipped.main,
                staged_pdf=staged_pdf,
                final_pdf=final_pdf,
                submission=submission,
                reference=self.reference,
            )
        )
        final_inspection = self.completed.get(TaskName.FINAL_PDF_INSPECTION)
        record_findings(
            report,
            check_sanitized_pdf_metadata(
                final_inspection.value
                if final_inspection and final_inspection.status == "succeeded"
                else [],
                settings.metadata_privacy,
            ),
            "final metadata sanitization",
        )
        report.scope = "selected preparation checks, visual/text preservation and fresh ZIP rebuild"
        blocked = is_blocked(report, settings)
        self.record_inventory(
            "prepared" if blocked else "verified",
            final_root=shipped.root,
            path_mapping=shipped.path_mapping,
            archive=archive,
        )
        return None if blocked else VerifiedPackage(shipped.root, archive, final_pdf)

    def _note_online_checks_not_reached(self) -> None:
        """Selected online checks run on the final extraction; say so when it was never reached."""
        options, request = self.settings.online_checks, self.context.request
        if not options.online or request.command is not JobCommand.PREPARE or request.dry_run:
            return
        if any(finding.rule.startswith("online.") for finding in self.report.findings):
            return
        if not options.selected_rules:
            return
        self.report.findings.append(
            Finding(
                "online.not_reached",
                "Selected online checks run on the final archive extraction and were not run "
                "because preparation stopped earlier.",
                severity="info",
                status="skipped",
                suggestion="Resolve the blocking findings, or run `fledge check --online` "
                "for online evidence on the original sources.",
            )
        )


# How a succeeded task's findings enter the report; others are tagged with their task.
_SUCCESS_COLLECTORS: dict[str, Callable[[DocumentRun, TaskResult], None]] = {
    TaskName.SOURCE_CHECKS: DocumentRun._collect_source_analysis,
    TaskName.BIBLIOGRAPHY_CHECKS: DocumentRun._collect_unstaged_findings,
    TaskName.MANUSCRIPT_CHECKS: DocumentRun._collect_unstaged_findings,
}
