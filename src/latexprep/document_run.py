"""One project's run: import, analysis, baseline, staged transformations and a verified package.

``run_project`` imports the input and selects its main file; ``DocumentRun`` carries
one selected document through its stages. Reporting (``task_reporting``), rebuilds
(``document_builds``), the final package verification (``package_verification``) and
main-file selection (``main_selection``) live in their own modules.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass, field, replace
from pathlib import Path

from .build_result import BuildResult
from .config import Settings
from .document_builds import PREPARED, TRANSFORMED, build_stage, repeat_baselines
from .document_tasks import (
    TaskName,
    baseline_build_task,
    baseline_pdf_tasks,
    compare_documents,
    source_analysis_tasks,
)
from .findings import Finding, has_blockers
from .formatting import format_project
from .job_context import JobCommand, JobContext, import_input
from .job_report import is_blocked, job_outcome, record_findings
from .main_selection import select_main
from .package_inputs import InputEvidence, generated_bibliography_inputs, select_package_inputs
from .package_verification import PackageVerification, ShippedSources
from .publication import VerifiedPackage
from .report import Outcome, Report
from .review_policy import BASELINE_VERSUS_PREPARED_STAGE
from .scheduler import Scheduler, Task, TaskResult
from .staging import (
    apply_contents,
    flatten_sources,
    inline_bibliography_files,
    transform_sources,
)
from .task_reporting import InputSelection, PackageLayout, TaskReporter

_SELECT_SUBMISSION_INPUTS_STAGE = "select submission inputs"
# Rule names of the online checks all start with this; none means they never ran.
_ONLINE_RULE_PREFIX = "online."


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


async def run_project(context: JobContext, report: Report) -> VerifiedPackage | None:
    """Inspect, check or prepare one project; return the package when ready to publish."""
    request = context.request
    context.emit("Importing a separate project snapshot")
    imported = await import_input(context, report)
    reference = None
    if request.reference_pdf:
        reference = await context.thread_operation(
            "snapshot supplied PDF",
            context.runner.freeze_input,
            request.reference_pdf,
            context.work / "reference-input",
        )
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
    # Every finished task's result, in the one place that tasks and stages read it from.
    completed: dict[str, TaskResult] = field(default_factory=dict)
    reporter: TaskReporter = field(init=False)
    scheduler: Scheduler = field(init=False)
    verification: PackageVerification = field(init=False)

    def __post_init__(self) -> None:
        self.reporter = TaskReporter(
            self.report,
            self.settings,
            source_root=self.imported,
            main=self.main,
            document_name=self.context.request.document_name,
        )
        self.scheduler = Scheduler(self.context.budget, self.context.emit, self._on_result)
        self.verification = PackageVerification(
            self.context, self.reporter, self.scheduler, self.completed, self.reference
        )

    @property
    def settings(self) -> Settings:
        return self.context.settings

    @property
    def command(self) -> JobCommand:
        return self.context.request.command

    def _on_result(self, result: TaskResult) -> None:
        self.completed[result.name] = result
        self.reporter.collect(result)

    def _analysis_snapshot(self) -> Path:
        """The selected preparation inputs once their selection succeeded, else the import."""
        selection = self.completed.get(TaskName.SELECT_PREPARATION_INPUTS)
        if selection is not None and selection.succeeded:
            return selection.value.root
        return self.imported

    def _baseline_build(self) -> BuildResult:
        return self.completed[TaskName.BASELINE_BUILD].value

    def _baseline_pdf(self) -> Path:
        pdf = self._baseline_build().pdf
        # Dependents start only after the baseline build succeeded with a PDF.
        assert pdf is not None
        return pdf

    async def run(self) -> VerifiedPackage | None:
        """Run the document's stages; the package when preparation verified one."""
        report, settings = self.report, self.settings
        await self.scheduler.execute(self._analysis_tasks())
        # Later stages read other trees; free the graphs the analysis tasks shared.
        self.context.sources.clear()
        if self.command is JobCommand.INSPECT:
            report.scope = "source and bibliography checks; compilation and PDF checks not run"
            report.outcome = job_outcome(report, settings)
            return None
        report.scope = "selected source, bibliography, privacy, isolated build and PDF checks"
        baseline = self.completed.get(TaskName.BASELINE_BUILD)
        if baseline is None or not baseline.succeeded or self.main is None:
            self._stop()
            return None
        if settings.workflow.baseline_runs > 1 and not is_blocked(report, settings):
            await repeat_baselines(
                self.context, self.reporter, self.imported, self.main, self._baseline_pdf()
            )
        if self.command is JobCommand.CHECK or is_blocked(report, settings):
            report.outcome = job_outcome(report, settings)
            self._note_online_checks_not_reached()
            return None
        return await self._prepare(baseline.value, self.main)

    def _stop(self) -> None:
        """End the run without a package: blocked, with a note on online checks never reached."""
        self.report.outcome = Outcome.BLOCKED
        self._note_online_checks_not_reached()

    def _analysis_tasks(self) -> list[Task]:
        """Source, bibliography, privacy, baseline build and baseline PDF tasks."""
        sources = source_analysis_tasks(self.context, self._analysis_snapshot, self.main)
        if self.main is None or self.command is JobCommand.INSPECT:
            return sources
        baseline = baseline_build_task(self.context, self.imported, self.main)
        checks = [
            *sources,
            *baseline_pdf_tasks(
                self.context,
                self._baseline_build,
                self._baseline_pdf,
                self._analysis_snapshot,
                self.main,
                self.reference,
            ),
        ]
        if self.command is not JobCommand.PREPARE:
            return [baseline, *checks]
        main = self.main
        selection = Task.composite(
            TaskName.SELECT_PREPARATION_INPUTS,
            lambda: self._select_preparation_inputs(main),
            needs_success_of=(TaskName.BASELINE_BUILD,),
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
        baseline = self._baseline_build()
        selected = await self.context.thread_operation(
            "select preparation inputs",
            select_package_inputs,
            self.imported,
            destination,
            main,
            # Absent evidence (None) is a blocking diagnostic in select_package_inputs.
            InputEvidence(
                baseline.submission_inputs,
                self.retained | inline_bibliography_files(self.settings),
                baseline.generated_reads,
            ),
        )
        return InputSelection(destination, *selected)

    async def _prepare(self, baseline: BuildResult, main: str) -> VerifiedPackage | None:
        """Transform, ship and verify the document; the package, or None after stopping."""
        report = self.report
        prepared = await self._stage_transformations(baseline, main)
        if prepared is None:
            self._stop()
            return None
        if self.context.request.dry_run:
            self.reporter.record_inventory("proposed")
            report.scope = (
                "preparation plan; baseline inputs selected and transformations previewed; "
                "final input selection, layout and bundle not verified"
            )
            report.outcome = Outcome.PLANNED
            return None
        shipped = await self._ship(prepared, main)
        if shipped is None:
            self._stop()
            return None
        package = await self.verification.package_and_verify(shipped)
        if package is None:
            self._stop()
            return None
        report.outcome = job_outcome(report, self.settings)
        return package

    async def _stage_transformations(self, baseline: BuildResult, main: str) -> Path | None:
        """Apply the selected transformations and formatting to a staging copy."""
        context, settings, report = self.context, self.settings, self.report
        context.emit("Planning and applying selected transformations in staging")
        snapshot = self._analysis_snapshot()
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

    async def _ship(self, prepared: Path, main: str) -> ShippedSources | None:
        """Rebuild staging, select the shipped inputs, flatten and compare with the baseline."""
        context, settings, report = self.context, self.settings, self.report
        staged = await build_stage(context, self.reporter, prepared, main, TRANSFORMED)
        if not staged.success or staged.pdf is None:
            return None
        context.emit("Selecting the transformed document's inputs for submission")
        shipment = context.work / "shipment"
        selected = await context.thread_operation(
            "select submission inputs",
            select_package_inputs,
            prepared,
            shipment,
            main,
            InputEvidence(staged.submission_inputs, self.retained, staged.generated_reads),
        )
        self.reporter.record_selection(selected, _SELECT_SUBMISSION_INPUTS_STAGE)
        if is_blocked(report, settings):
            return None
        flattened = await context.thread_operation(
            "flatten selected inputs", flatten_sources, shipment, context.work, main, settings
        )
        report.changes.extend(flattened.changes)
        report.findings.extend(flattened.findings)
        blocked = is_blocked(report, settings)
        layout = PackageLayout(flattened.root, flattened.path_mapping)
        self.reporter.record_inventory("incomplete" if blocked else "prepared", layout)
        if blocked:
            return None
        build = staged
        if settings.layout == "flat":
            build = await build_stage(
                context, self.reporter, flattened.root, flattened.main, PREPARED
            )
        pdf = build.pdf
        if not build.success or pdf is None:
            return None
        record_findings(
            report,
            await compare_documents(
                context,
                self._baseline_pdf(),
                pdf,
                "compare-prepared",
                BASELINE_VERSUS_PREPARED_STAGE,
                options=settings.workflow.comparison,
            ),
            BASELINE_VERSUS_PREPARED_STAGE,
        )
        if is_blocked(report, settings):
            return None
        return ShippedSources(flattened.root, flattened.main, pdf, flattened.path_mapping)

    def _note_online_checks_not_reached(self) -> None:
        """Selected online checks run on the final extraction; say so when it was never reached."""
        options, request = self.settings.online_checks, self.context.request
        if not options.online or request.command is not JobCommand.PREPARE or request.dry_run:
            return
        if any(finding.rule.startswith(_ONLINE_RULE_PREFIX) for finding in self.report.findings):
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
