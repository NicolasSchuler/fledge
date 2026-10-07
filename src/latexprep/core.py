"""Application workflow. Interfaces submit jobs; this module owns mutations and release."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .bibliography import check_bibliography, normalize_dois
from .bibliography_checks import check_bibliography_details
from .bibliography_transform import plan_bibliography
from .build_checks import check_build_details, check_local_package_shadows
from .check_pipeline import inspect_additional_pdf_checks, inspect_standalone_pdf
from .check_policy import MANDATORY_CODES, effective_settings, selected_findings
from .config import PARENT_MEMORY_MB, Settings, load_settings
from .formatting import format_project
from .manuscript import check_manuscript
from .manuscript_checks import check_manuscript_details
from .metadata_privacy import (
    check_image_metadata,
    check_sanitized_pdf_metadata,
    plan_metadata_sanitization,
)
from .models import Finding, PreparationError, Report, has_blockers
from .online_checks import check_online_references
from .package_inputs import select_package_inputs
from .pdf import compare_pdfs, inspect_pdf
from .project import ImportLimits, create_archive, import_project, plan_cleanup
from .reporting import apply_reviews, has_unaccepted_blockers
from .runtime import SELECTABLE_BUILD_ERRORS, BuildResult, RuntimeLimits, ToolRunner, build_project
from .scheduler import (
    ResourceBudget,
    Scheduler,
    Task,
    TaskResult,
    bounded_map,
    cancellation_point,
    run_in_thread,
)
from .source import analyze_sources, discover_roots, plan_flatten
from .source_transform import plan_source_transforms
from .structure_checks import check_author_records
from .submission_checks import check_submission, check_submission_archive
from .workflow_options import DocumentOptions, stage_document


@dataclass(frozen=True)
class JobRequest:
    command: str
    source: Path
    settings: Settings
    output: Path | None = None
    dry_run: bool = False
    reference_pdf: Path | None = None
    preview_output: Path | None = None


@dataclass
class DocumentResult:
    name: str
    report: Report
    pending: tuple[Path, Path, Path] | None


async def _build(
    tree: Path, main: str, work: Path, settings: Settings, runner: ToolRunner
) -> BuildResult:
    kwargs: dict[str, Any] = (
        {"bibliography_backend": settings.bibliography_backend}
        if settings.bibliography_backend != "auto"
        else {}
    )
    if settings.build_checks.inventory_loaded_options:
        kwargs["inventory_loaded_options"] = True
    ignored_logs = tuple(
        code for code in sorted(SELECTABLE_BUILD_ERRORS) if not settings.checks.enabled(code)
    )
    if ignored_logs:
        kwargs["ignored_log_checks"] = ignored_logs
    return await build_project(tree, main, work, settings.engine, runner, **kwargs)


def validate_destination(source: Path, destination: Path) -> Path:
    resolved_source = source.resolve()
    resolved = destination.resolve()
    if destination.exists() or destination.is_symlink():
        raise PreparationError(f"Output already exists: {destination}")
    if resolved_source == resolved or (
        resolved_source.is_dir() and resolved.is_relative_to(resolved_source)
    ):
        raise PreparationError("Output must be outside the original project")
    if not resolved.parent.is_dir():
        raise PreparationError(f"Output parent directory does not exist: {resolved.parent}")
    return resolved


def _blocked(report: Report, settings: Settings) -> bool:
    report.findings = selected_findings(report.findings, settings.checks)
    return has_unaccepted_blockers(report.findings, settings.reporting)


def _source_submission_options(settings: Settings):
    options = settings.submission_checks
    # Source and rendered-PDF identity policies share terms but have separate codes.
    return options if settings.checks.enabled("PRV001") else replace(options, identity_terms=())


def _outcome(report: Report, settings: Settings | None = None) -> str:
    if _blocked(report, settings) if settings is not None else has_blockers(report.findings):
        return "blocked"
    if any(item.severity == "warning" and item.status != "passed" for item in report.findings):
        return "passed_with_advisories"
    return "passed"


def _collect_build(report: Report, result: BuildResult, stage: str, settings: Settings) -> None:
    _record_findings(report, result.findings, stage)
    _record_findings(report, check_build_details(result, settings.build_checks), stage)
    report.tools.update(result.tools)
    report.stages.append(
        {
            "name": stage,
            "status": "succeeded" if result.success and result.pdf is not None else "failed",
            "command": result.command,
        }
    )
    if not result.success or result.pdf is None:
        report.findings.append(
            Finding(
                f"build.{stage}", f"Required {stage} build did not succeed", "error", "inconclusive"
            )
        )


def _record_findings(report: Report, findings: list[Finding], stage: str) -> None:
    report.findings.extend(
        replace(finding, details={**finding.details, "stage": stage}) for finding in findings
    )


def _apply_contents(
    root: Path, contents: dict[str, bytes], *, originals: dict[str, bytes] | None = None
) -> None:
    # Validate every proposal before applying the first one. Workers never write source files.
    targets: list[tuple[Path, bytes]] = []
    if originals is not None and set(contents) != set(originals):
        raise PreparationError("Formatting proposals lack complete original-byte preconditions")
    for relative, data in sorted(contents.items()):
        cancellation_point()
        path = root / relative
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
        ):
            raise PreparationError(f"Invalid transformation path: {relative}")
        if originals is not None and path.read_bytes() != originals[relative]:
            raise PreparationError(f"Source changed after formatting was planned: {relative}")
        targets.append((path, data))
    for path, data in targets:
        cancellation_point()
        path.write_bytes(data)


def _publish(
    output: Path,
    prepared: Path,
    archive: Path,
    pdf: Path,
    report: Report,
    check_deadline: Callable[[], None] = lambda: None,
    *,
    previews: tuple[tuple[Path, Path], ...] = (),
) -> None:
    check_deadline()
    artifacts = {
        **report.artifacts,
        "sources": str(output / "sources"),
        "archive": str(output / "submission.zip"),
        "pdf": str(output / "manuscript.pdf"),
        "report": str(output / "report.json"),
    }
    # Include final paths before the first publication mutation: paths themselves
    # can contain credentials or push the complete report beyond redaction limits.
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error":
        report.outcome = "error"
        report.artifacts.clear()
        report.findings.append(
            Finding(
                "report.redaction_limit",
                "The final report could not be safely represented; no output was published.",
                "error",
                "inconclusive",
                suggestion="Use an output path without credential values or reduce report size.",
            )
        )
        return
    encoded = json.dumps(public, indent=2, ensure_ascii=False) + "\n"
    check_deadline()
    # mkdir is exclusive, so a concurrent creation cannot be overwritten by a rename.
    try:
        output.mkdir()
    except FileExistsError as error:
        raise PreparationError(f"Output appeared while the job ran: {output}") from error
    try:
        shutil.copytree(prepared, output / "sources")
        check_deadline()
        shutil.copyfile(archive, output / "submission.zip")
        shutil.copyfile(pdf, output / "manuscript.pdf")
        _copy_previews(previews, check_deadline)
        check_deadline()
        with (output / "report.json").open("x", encoding="utf-8") as stream:
            stream.write(encoded)
        report.artifacts.update(artifacts)
        check_deadline()
    except BaseException:
        # This directory was created exclusively by this call; never touch previous user data.
        shutil.rmtree(output)
        report.artifacts.clear()
        raise


async def _thread_operation(
    budget: ResourceBudget, name: str, function: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    async with budget.lease(name):
        return await run_in_thread(function, *args, **kwargs)


def _preview_plan(report: Report, work: Path, destination: Path) -> tuple[tuple[Path, Path], ...]:
    paths: list[Path] = []
    for finding in report.sorted_findings():
        artifacts = finding.details.get("artifacts", [])
        if not isinstance(artifacts, list):
            continue
        for name in artifacts:
            if not isinstance(name, str):
                continue
            path = Path(name)
            if path not in paths:
                if (
                    path.suffix != ".png"
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(work)
                    or not path.is_file()
                ):
                    raise PreparationError("Preview adapter returned an invalid artifact")
                if path.stat().st_size > 16_000_000 or len(paths) >= 10000:
                    raise PreparationError("Preview artifacts exceed bounded export limits")
                paths.append(path)
    return tuple(
        (source, destination / f"page-{index:04d}.png") for index, source in enumerate(paths, 1)
    )


def _locate_previews(report: Report, previews: tuple[tuple[Path, Path], ...]) -> None:
    mapping = {str(source): str(target) for source, target in previews}
    findings = []
    used = set()
    for item in report.findings:
        artifacts = item.details.get("artifacts")
        if isinstance(artifacts, list):
            exported = [
                mapping[path] for path in artifacts if isinstance(path, str) and path in mapping
            ]
            used.update(exported)
            item = replace(
                item,
                details={**item.details, "artifacts": exported, "previews_exported": len(exported)},
            )
        findings.append(item)
    report.findings = findings
    for index, (_, destination) in enumerate(previews, 1):
        if str(destination) in used:
            report.artifacts[f"preview.{index}"] = str(destination)


def _copy_previews(
    previews: tuple[tuple[Path, Path], ...], check_deadline: Callable[[], None]
) -> None:
    for source, destination in previews:
        check_deadline()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(source.read_bytes())


def _source_policies(root: Path, main: str, settings: Settings) -> list[Finding]:
    """Re-evaluate the actual shipped sources after all transformations."""
    return [
        *analyze_sources(root, main).findings,
        *check_bibliography(root),
        *check_bibliography_details(root, main, settings.bibliography_checks),
        *check_manuscript(root, main, settings.manuscript_options()),
        *check_manuscript_details(root, main, settings.manuscript_checks),
        *check_image_metadata(root, settings.metadata_privacy),
        *check_author_records(root, main, settings.structure_checks),
    ]


def _transform_sources(
    snapshot: Path,
    work: Path,
    selected: str,
    settings: Settings,
    dependencies: set[str],
    retained: set[str],
) -> tuple[Path, str, list, list[Finding], dict[str, str]]:
    prepared = work / "prepared"
    shutil.copytree(snapshot, prepared)
    changes = []
    findings: list[Finding] = []
    path_mapping: dict[str, str] = {}
    cancellation_point()
    if settings.cleanup:
        protected = (
            dependencies
            | retained
            | (
                {settings.source_transforms.inline_bibliography}
                if settings.source_transforms.inline_bibliography
                else set()
            )
        )
        cleanup = plan_cleanup(prepared, protected)
        for change in cleanup:
            cancellation_point()
            candidate = prepared / change.path
            if candidate.is_file():
                candidate.unlink()
        changes.extend(cleanup)
    if not has_blockers(findings):
        metadata = plan_metadata_sanitization(prepared, settings.metadata_privacy)
        findings.extend(metadata.findings)
        changes.extend(metadata.changes)
        if not has_blockers(metadata.findings):
            _apply_contents(prepared, metadata.contents, originals=metadata.originals)
    if not has_blockers(findings):
        contents, bib_changes, bib_findings = plan_bibliography(
            prepared, selected, settings.bibliography_transform
        )
        findings.extend(bib_findings)
        changes.extend(bib_changes)
        if not has_blockers(bib_findings):
            _apply_contents(prepared, contents)
    if settings.normalize_doi and not has_blockers(findings):
        contents, doi_changes, doi_findings = normalize_dois(prepared)
        findings.extend(doi_findings)
        changes.extend(doi_changes)
        if not has_blockers(doi_findings):
            _apply_contents(prepared, contents)
    if not has_blockers(findings):
        plan = plan_source_transforms(prepared, selected, settings.source_transforms)
        findings.extend(plan.findings)
        changes.extend(plan.changes)
        if not has_blockers(plan.findings):
            if set(plan.removal_originals) != plan.removals:
                raise PreparationError("Source removal plan lacks complete preconditions")
            for name, original in plan.removal_originals.items():
                candidate = prepared / name
                if (
                    candidate.is_symlink()
                    or not candidate.resolve().is_relative_to(prepared.resolve())
                    or candidate.read_bytes() != original
                ):
                    raise PreparationError("Source changed after removal was planned")
            _apply_contents(prepared, plan.contents, originals=plan.originals)
            for name in sorted(plan.removals - retained):
                cancellation_point()
                (prepared / name).unlink()
            changes = [
                change
                for change in changes
                if not (change.kind == "exclude" and change.path in retained)
            ]
    return prepared, selected, changes, findings, path_mapping


def _flatten_sources(
    prepared: Path, work: Path, selected: str, settings: Settings
) -> tuple[Path, str, list, list[Finding], dict[str, str]]:
    changes = []
    findings: list[Finding] = []
    path_mapping: dict[str, str] = {}
    if settings.layout == "flat" and not has_blockers(findings):
        flatten = plan_flatten(
            prepared, selected, filename_overrides=settings.source_transforms.filename_overrides
        )
        findings.extend(flatten.findings)
        changes.extend(flatten.changes)
        if not has_blockers(flatten.findings):
            flat = work / "flat"
            flat.mkdir()
            for old, new in sorted(flatten.mapping.items()):
                cancellation_point()
                with (flat / new).open("xb") as stream:
                    stream.write(flatten.contents.get(old, (prepared / old).read_bytes()))
            selected = flatten.mapping[selected]
            path_mapping = flatten.mapping
            prepared = flat
    return prepared, selected, changes, findings, path_mapping


async def _run_in_workspace(
    request: JobRequest,
    settings: Settings,
    work: Path,
    report: Report,
    runner: ToolRunner,
    budget: ResourceBudget,
    emit: Callable[[str], None],
) -> tuple[Path, Path, Path] | None:
    if request.command == "pdf":
        report.scope = "standalone PDF inspection; source and submission bundle were not verified"
        report.findings.extend(
            await inspect_standalone_pdf(
                request.source,
                work / "standalone",
                runner,
                settings,
                budget,
                request.reference_pdf,
            )
        )
        report.outcome = _outcome(report, settings)
        return None
    limits = ImportLimits(
        max_files=settings.max_files, max_bytes=settings.max_bytes, max_depth=settings.max_depth
    )
    emit("Importing a separate project snapshot")
    imported = await _thread_operation(
        budget, "import project", import_project, request.source, work / "import", limits
    )
    snapshot = imported.root
    report.findings.extend(imported.findings)
    report.changes.extend(imported.changes)
    reference = None
    if request.reference_pdf:
        reference = await _thread_operation(
            budget,
            "snapshot supplied PDF",
            runner.freeze_input,
            request.reference_pdf,
            work / "reference-input",
        )
    if request.command == "bib":
        report.scope = "selected bibliography checks; no build or bundle verification"
        roots = await _thread_operation(budget, "discover roots", discover_roots, snapshot)
        selected = settings.main or (roots[0] if len(roots) == 1 else None)
        if selected and (
            Path(selected).is_absolute()
            or ".." in Path(selected).parts
            or not (snapshot / selected).is_file()
        ):
            raise PreparationError("main must name a source file inside the project")
        selected = Path(selected).as_posix() if selected else None
        report.main = selected
        report.findings.extend(
            await _thread_operation(budget, "bibliography checks", check_bibliography, snapshot)
        )
        report.findings.extend(
            await _thread_operation(
                budget,
                "bibliography coverage and fields",
                check_bibliography_details,
                snapshot,
                selected,
                settings.bibliography_checks,
            )
        )
        report.findings.extend(
            await check_online_references(
                snapshot, selected, options=settings.online_checks, budget=budget
            )
        )
        report.outcome = _outcome(report, settings)
        return None
    if request.command == "fmt":
        report.scope = "formatting check; no build or bundle verification"
        formatted = await format_project(
            snapshot, work / "format-check", runner, settings, budget=budget
        )
        report.findings.extend(formatted.findings)
        report.tools.update(formatted.tools)
        report.changes.extend(formatted.changes)
        report.outcome = _outcome(report, settings)
        return None

    roots = (
        []
        if settings.main
        else await _thread_operation(budget, "discover roots", discover_roots, snapshot)
    )
    selected = settings.main
    if selected is None and len(roots) == 1:
        selected = roots[0]
    if selected is None:
        report.findings.append(
            Finding(
                "project.main_selection",
                f"Select --main explicitly; found {len(roots)} candidate roots: {', '.join(roots)}",
                "error",
                "inconclusive",
            )
        )
    elif (
        Path(selected).is_absolute()
        or ".." in Path(selected).parts
        or not (snapshot / selected).is_file()
    ):
        raise PreparationError(f"main must name a source file inside the project: {selected}")
    if selected is not None:
        selected = Path(selected).as_posix()
    report.main = selected
    baseline_source = snapshot
    retained = {
        name
        for name, _ in (
            *request.settings.submission_checks.required_deliverables,
            *request.settings.submission_checks.template_references,
        )
    }
    completed: dict[str, TaskResult] = {}

    def collect_task(result: TaskResult) -> None:
        completed[result.name] = result
        if result.name == "baseline build" and result.value is not None:
            _collect_build(report, result.value, "baseline", settings)
            report.stages[-1].update(
                elapsed_seconds=result.elapsed_seconds, queue_seconds=result.queue_seconds
            )
            return
        report.stages.append(
            {
                "name": result.name,
                "status": result.status,
                "elapsed_seconds": result.elapsed_seconds,
                "queue_seconds": result.queue_seconds,
            }
        )
        if result.name == "select preparation inputs" and result.value is not None:
            _record_findings(report, result.value, result.name)
            return
        if result.status != "succeeded":
            report.findings.append(
                Finding("execution.task", f"{result.name}: {result.error}", "error", "inconclusive")
            )
        elif result.name == "source checks":
            report.findings.extend(result.value.findings)
        elif result.name in {"bibliography checks", "manuscript checks"}:
            report.findings.extend(result.value)
        else:
            _record_findings(report, result.value, result.name)

    scheduler = Scheduler(settings.jobs, settings.memory_mb, emit, collect_task, budget=budget)
    tasks = [
        Task("source checks", lambda: run_in_thread(analyze_sources, snapshot, selected)),
        Task("bibliography checks", lambda: run_in_thread(check_bibliography, snapshot)),
        Task(
            "bibliography coverage and fields",
            lambda: run_in_thread(
                check_bibliography_details, snapshot, selected, settings.bibliography_checks
            ),
        ),
        Task(
            "manuscript details",
            lambda: run_in_thread(
                check_manuscript_details, snapshot, selected, settings.manuscript_checks
            ),
        ),
        Task(
            "image metadata privacy",
            lambda: run_in_thread(check_image_metadata, snapshot, settings.metadata_privacy),
        ),
        Task(
            "per-author fields",
            lambda: run_in_thread(
                check_author_records, snapshot, selected, settings.structure_checks
            ),
        ),
    ]
    submission_options = _source_submission_options(settings)
    if request.command == "prepare":
        # These constraints target the transformed output, which may rename or remove files.
        submission_options = replace(
            submission_options,
            filename_max_length=None,
            filename_allowed_characters=None,
            allowed_extensions=(),
            required_deliverables=(),
        )
    tasks.append(
        Task(
            "source privacy and bundle checks",
            lambda: run_in_thread(check_submission, snapshot, selected, submission_options),
        )
    )
    # A released package needs evidence about its transformed references. Run
    # online checks once per document, on the final extraction for preparation,
    # without spending a second request allowance on the original snapshot.
    if request.command != "prepare" or request.dry_run:
        tasks.append(
            Task(
                "online reference checks",
                lambda: check_online_references(
                    snapshot, selected, options=settings.online_checks, budget=budget
                ),
                cpu=0,
                memory_mb=0,
            )
        )
    if selected:
        tasks.append(
            Task(
                "manuscript checks",
                lambda: run_in_thread(
                    check_manuscript, snapshot, selected, settings.manuscript_options()
                ),
            )
        )
    if selected and request.command != "inspect":
        tasks.append(
            Task(
                "baseline configured PDF checks",
                lambda: inspect_additional_pdf_checks(
                    completed["baseline build"].value.pdf,
                    snapshot,
                    selected,
                    work / "additional-baseline",
                    runner,
                    settings,
                    budget,
                ),
                requires=("baseline build",),
                cpu=0,
                memory_mb=0,
            )
        )
        if settings.build_checks.check_local_package_shadows:
            tasks.append(
                Task(
                    "baseline local package shadows",
                    lambda: check_local_package_shadows(
                        completed["baseline build"].value,
                        work / "shadows-baseline",
                        runner,
                        budget=budget,
                    ),
                    requires=("baseline build",),
                    cpu=0,
                    memory_mb=0,
                )
            )
        tasks.insert(
            0,
            Task(
                "baseline build",
                lambda: _build(baseline_source, selected, work / "baseline", settings, runner),
                memory_mb=settings.build_memory_mb,
                kind="build",
                is_success=lambda value: value.success and value.pdf is not None,
            ),
        )
        tasks.append(
            Task(
                "baseline PDF inspection",
                lambda: inspect_pdf(
                    completed["baseline build"].value.pdf,
                    work / "inspect-baseline",
                    runner,
                    settings.max_pages,
                    options=settings.pdf_options(),
                    budget=budget,
                ),
                requires=("baseline build",),
                cpu=0,
                memory_mb=0,
            )
        )
        if reference:
            tasks.append(
                Task(
                    "supplied PDF versus baseline",
                    lambda: compare_pdfs(
                        reference,
                        completed["baseline build"].value.pdf,
                        work / "reference-baseline",
                        runner,
                        budget=budget,
                    ),
                    requires=("baseline build",),
                    cpu=0,
                    memory_mb=0,
                )
            )
    if request.command == "prepare" and selected:

        async def select_initial_inputs() -> list[Finding]:
            nonlocal snapshot
            baseline_build = completed["baseline build"].value
            destination = work / "selected-inputs"
            explicit = retained | (
                {settings.source_transforms.inline_bibliography}
                if settings.source_transforms.inline_bibliography
                else set()
            )
            changes, findings = await _thread_operation(
                budget,
                "select preparation inputs",
                select_package_inputs,
                baseline_source,
                destination,
                selected,
                getattr(baseline_build, "submission_inputs", None),
                explicit,
            )
            report.changes.extend(changes)
            if not has_blockers(findings):
                snapshot = destination
            return findings

        tasks = [
            task
            if task.name == "baseline build"
            else replace(task, requires=(*task.requires, "select preparation inputs"))
            for task in tasks
        ]
        tasks.append(
            Task(
                "select preparation inputs",
                select_initial_inputs,
                requires=("baseline build",),
                cpu=0,
                memory_mb=0,
                is_success=lambda findings: not has_blockers(findings),
            )
        )
    results = await scheduler.execute(tasks)
    if request.command == "inspect":
        report.scope = "source and bibliography checks; compilation and PDF checks not run"
        report.outcome = _outcome(report, settings)
        return None
    report.scope = "selected source, bibliography, privacy, isolated build and PDF checks"
    baseline_result = results.get("baseline build")
    baseline = baseline_result.value if baseline_result else None
    if baseline is None or not baseline.success or baseline.pdf is None or selected is None:
        report.outcome = "blocked"
        return None
    if settings.workflow.baseline_runs > 1 and not _blocked(report, settings):
        stable = True
        for attempt in range(2, settings.workflow.baseline_runs + 1):
            async with budget.lease(
                f"repeat baseline {attempt}", memory_mb=settings.build_memory_mb, kind="build"
            ):
                repeated = await _build(
                    baseline_source, selected, work / f"baseline-{attempt}", settings, runner
                )
            _collect_build(report, repeated, f"baseline repeat {attempt}", settings)
            if not repeated.success or repeated.pdf is None:
                stable = False
                break
            compared = await compare_pdfs(
                baseline.pdf,
                repeated.pdf,
                work / f"compare-baseline-{attempt}",
                runner,
                budget=budget,
            )
            _record_findings(report, compared, f"baseline repeat {attempt}")
            if has_blockers(compared):
                stable = False
                break
        report.findings.append(
            Finding(
                "compare.baseline_stability",
                f"{settings.workflow.baseline_runs} independent baseline builds match."
                if stable
                else "Baseline builds are inconsistent or could not be fully compared.",
                "info" if stable else "error",
                "passed" if stable else "failed",
                details={
                    "requested_builds": settings.workflow.baseline_runs,
                    "comparison": "exact page count, normalized extracted text and 144-DPI pixels",
                },
            )
        )
    if request.command == "check" or _blocked(report, settings):
        report.outcome = _outcome(report, settings)
        return None

    emit("Planning and applying selected transformations in staging")
    dependencies = set(baseline.dependencies)
    analysis = results["source checks"].value
    if analysis:
        dependencies.update(analysis.dependencies)
    prepared, selected, changes, findings, path_mapping = await _thread_operation(
        budget,
        "transform sources",
        _transform_sources,
        snapshot,
        work,
        selected,
        settings,
        dependencies,
        retained,
    )
    report.changes.extend(changes)
    report.findings.extend(findings)
    if settings.format and not _blocked(report, settings):
        formatted = await format_project(
            prepared, work / "format-prepare", runner, settings, budget=budget
        )
        report.tools.update(formatted.tools)
        report.findings.extend(
            finding for finding in formatted.findings if finding.rule != "source.formatting"
        )
        report.changes.extend(formatted.changes)
        if not has_blockers(formatted.findings):
            await _thread_operation(
                budget,
                "apply formatting",
                _apply_contents,
                prepared,
                formatted.contents,
                originals=formatted.originals,
            )
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None
    if request.dry_run:
        report.scope = (
            "preparation plan; baseline inputs selected and transformations previewed; "
            "final input selection, layout and bundle not verified"
        )
        report.outcome = "planned"
        return None

    async def build_stage(tree: Path, directory: str, stage: str) -> BuildResult:
        emit(f"Rebuilding {stage} sources")
        async with budget.lease(f"{stage} build", memory_mb=settings.build_memory_mb, kind="build"):
            result = await _build(tree, selected, work / directory, settings, runner)
        _collect_build(report, result, stage, settings)
        if result.success and getattr(result, "submission_inputs", None) is None:
            _record_findings(
                report,
                [
                    Finding(
                        "package.dependencies",
                        "Complete build dependency evidence is unavailable.",
                        "error",
                        "inconclusive",
                        suggestion="Resolve the submission dependency diagnostic and retry.",
                    )
                ],
                stage,
            )
        if settings.build_checks.check_local_package_shadows:
            _record_findings(
                report,
                await check_local_package_shadows(
                    result, work / f"shadows-{stage}", runner, budget=budget
                ),
                stage,
            )
        return result

    staged = await build_stage(prepared, "transformed-build", "transformed")
    if not staged.success or staged.pdf is None:
        report.outcome = "blocked"
        return None
    emit("Selecting the transformed document's inputs for submission")
    shipment = work / "shipment"
    changes, findings = await _thread_operation(
        budget,
        "select submission inputs",
        select_package_inputs,
        prepared,
        shipment,
        selected,
        getattr(staged, "submission_inputs", None),
        retained,
    )
    report.changes.extend(changes)
    _record_findings(report, findings, "select submission inputs")
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None
    prepared, selected, changes, findings, path_mapping = await _thread_operation(
        budget, "flatten selected inputs", _flatten_sources, shipment, work, selected, settings
    )
    report.changes.extend(changes)
    report.findings.extend(findings)
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None
    prepared_submission = replace(
        _source_submission_options(settings),
        required_deliverables=tuple(
            (path_mapping.get(name, name), kind)
            for name, kind in settings.submission_checks.required_deliverables
        ),
        template_references=tuple(
            (path_mapping.get(name, name), reference)
            for name, reference in settings.submission_checks.template_references
        ),
    )
    _record_findings(
        report,
        await _thread_operation(
            budget,
            "prepared submission checks",
            check_submission,
            prepared,
            selected,
            prepared_submission,
        ),
        "prepared submission checks",
    )
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None
    if settings.layout == "flat":
        staged = await build_stage(prepared, "staged-build", "prepared")
        if not staged.success or staged.pdf is None:
            report.outcome = "blocked"
            return None
    _record_findings(
        report,
        await compare_pdfs(
            baseline.pdf,
            staged.pdf,
            work / "compare-prepared",
            runner,
            budget=budget,
            options=settings.workflow.comparison,
        ),
        "baseline versus prepared",
    )
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None

    emit("Packaging sources and rebuilding a fresh extraction of the exact ZIP")
    archive = work / "submission.zip"
    await _thread_operation(budget, "create archive", create_archive, prepared, archive)
    _record_findings(
        report,
        await _thread_operation(
            budget,
            "compressed archive policy",
            check_submission_archive,
            archive,
            settings.submission_checks,
        ),
        "archive",
    )
    if _blocked(report, settings):
        report.outcome = "blocked"
        return None
    if archive.stat().st_size > settings.max_bytes:
        raise PreparationError("Prepared archive exceeds max_bytes")
    # The archive is our own bounded output; preserve byte/count limits without
    # rejecting legitimate highly compressible sources on ratio alone.
    final_limits = ImportLimits(
        max_files=settings.max_files,
        max_bytes=settings.max_bytes,
        max_depth=settings.max_depth,
        max_ratio=settings.max_bytes,
    )
    extracted = await _thread_operation(
        budget,
        "extract archive",
        import_project,
        archive,
        work / "extracted",
        final_limits,
        unwrap=False,
        exclude_metadata=False,
    )
    final = await build_stage(extracted.root, "archive-build", "archive")
    if not final.success or final.pdf is None:
        report.outcome = "blocked"
        return None
    staged_pdf, final_pdf = staged.pdf, final.pdf
    final_tasks = [
        Task(
            "final source checks",
            lambda: run_in_thread(_source_policies, extracted.root, selected, settings),
        ),
        Task(
            "final online reference checks",
            lambda: check_online_references(
                extracted.root, selected, options=settings.online_checks, budget=budget
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            "prepared versus archive rebuild",
            lambda: compare_pdfs(
                staged_pdf, final_pdf, work / "compare-archive", runner, budget=budget
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            "final PDF inspection",
            lambda: inspect_pdf(
                final_pdf,
                work / "inspect-final",
                runner,
                settings.max_pages,
                options=settings.pdf_options(),
                budget=budget,
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            "final configured PDF checks",
            lambda: inspect_additional_pdf_checks(
                final_pdf,
                extracted.root,
                selected,
                work / "additional-final",
                runner,
                settings,
                budget,
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            "final submission checks",
            lambda: run_in_thread(check_submission, extracted.root, selected, prepared_submission),
        ),
    ]
    if reference:
        final_tasks.append(
            Task(
                "supplied PDF versus archive rebuild",
                lambda: compare_pdfs(
                    reference, final_pdf, work / "reference-final", runner, budget=budget
                ),
                cpu=0,
                memory_mb=0,
            )
        )
    await scheduler.execute(final_tasks)
    final_pdf_check = completed.get("final PDF inspection")
    _record_findings(
        report,
        check_sanitized_pdf_metadata(
            final_pdf_check.value
            if final_pdf_check and final_pdf_check.status == "succeeded"
            else [],
            settings.metadata_privacy,
        ),
        "final metadata sanitization",
    )
    report.scope = "selected preparation checks, visual/text preservation and fresh ZIP rebuild"
    report.outcome = _outcome(report, settings)
    return None if _blocked(report, settings) else (prepared, archive, final_pdf)


async def _run_documents(
    request: JobRequest,
    settings: Settings,
    work: Path,
    report: Report,
    runner: ToolRunner,
    budget: ResourceBudget,
    emit: Callable[[str], None],
) -> list[DocumentResult]:
    if request.command not in {"inspect", "check", "prepare"}:
        raise PreparationError("workflow.documents is supported by inspect, check and prepare")
    if request.reference_pdf is not None:
        raise PreparationError("Configure reference_pdf separately for each document")
    imported = await _thread_operation(
        budget,
        "import shared document snapshot",
        import_project,
        request.source,
        work / "shared-import",
        ImportLimits(settings.max_files, settings.max_bytes, settings.max_depth),
    )
    report.findings.extend(imported.findings)
    report.changes.extend(imported.changes)
    if _blocked(report, settings):
        report.outcome = "blocked"
        return []

    async def document_job(document: DocumentOptions) -> DocumentResult:
        child = Report(request.command)
        child_settings = replace(
            settings,
            main=document.main,
            engine=document.engine or settings.engine,
            workflow=replace(settings.workflow, documents=()),
        )
        child.settings = child_settings.to_dict()
        child_settings = replace(
            child_settings,
            reporting=replace(
                settings.reporting,
                accepted_exceptions=tuple(
                    replace(rule, document=None)
                    for rule in settings.reporting.accepted_exceptions
                    if rule.document in {None, document.name}
                ),
                suppressions=tuple(
                    replace(rule, document=None)
                    for rule in settings.reporting.suppressions
                    if rule.document in {None, document.name}
                ),
            ),
        )
        try:
            source = await _thread_operation(
                budget,
                f"select {document.name} files",
                stage_document,
                imported.root,
                work / "selected" / document.name,
                document,
            )
            child_request = replace(
                request,
                source=source,
                settings=replace(
                    request.settings,
                    main=document.main,
                    engine=document.engine or request.settings.engine,
                    workflow=replace(request.settings.workflow, documents=()),
                ),
                reference_pdf=Path(document.reference_pdf) if document.reference_pdf else None,
            )
            child_work = work / "documents" / document.name
            child_work.mkdir(parents=True)
            pending = await _run_in_workspace(
                child_request,
                child_settings,
                child_work,
                child,
                runner,
                budget,
                lambda message: emit(f"{document.name}: {message}"),
            )
        except (PreparationError, OSError, ValueError) as error:
            child.findings.append(Finding("package.document", str(error), "error", "inconclusive"))
            child.outcome = "blocked"
            pending = None
        child.findings = selected_findings(child.findings, child_settings.checks)
        child = apply_reviews(child, child_settings.reporting)
        return DocumentResult(document.name, child, pending)

    documents = await bounded_map(settings.workflow.documents, document_job, limit=settings.jobs)
    for document in documents:
        child = document.report
        report.findings.extend(
            replace(item, details={**item.details, "document": document.name})
            for item in child.findings
        )
        report.changes.extend(
            replace(
                change,
                path=f"{document.name}/{change.path}",
                destination=f"{document.name}/{change.destination}" if change.destination else None,
            )
            for change in child.changes
        )
        report.stages.extend(
            {**stage, "name": f"{document.name}: {stage['name']}", "document": document.name}
            for stage in child.stages
        )
        report.tools.update(child.tools)
    verified = all(document.pending is not None for document in documents)
    if request.command == "prepare" and not request.dry_run:
        report.findings.append(
            Finding(
                "package.independent_documents",
                f"{len(documents)} selected document packages independently verified."
                if verified
                else "At least one selected document package failed verification; "
                "no package will be released.",
                "info" if verified else "error",
                "passed" if verified else "failed",
                details={
                    "documents": [
                        {
                            "name": item.name,
                            "outcome": item.report.outcome,
                            "verified": item.pending is not None,
                        }
                        for item in documents
                    ]
                },
            )
        )
    report.scope = (
        "all explicitly selected independent document packages; "
        "each has its own checks and archive rebuild"
    )
    report.outcome = (
        "planned"
        if request.dry_run and not _blocked(report, settings)
        else _outcome(report, settings)
    )
    return documents


def _publish_documents(
    output: Path,
    documents: list[DocumentResult],
    report: Report,
    check_deadline: Callable[[], None],
    *,
    previews: tuple[tuple[Path, Path], ...] = (),
) -> None:
    artifacts = {
        **report.artifacts,
        **{
            f"{document.name}.{kind}": str(output / document.name / filename)
            for document in documents
            for kind, filename in (
                ("sources", "sources"),
                ("archive", "submission.zip"),
                ("pdf", "manuscript.pdf"),
                ("report", "report.json"),
            )
        },
    }
    artifacts["report"] = str(output / "report.json")
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error":
        raise PreparationError(
            "Final multi-document report cannot be safely represented; no output published"
        )
    encoded = json.dumps(public, indent=2, ensure_ascii=False) + "\n"
    check_deadline()
    output.mkdir()
    try:
        for document in documents:
            assert document.pending is not None
            _publish(output / document.name, *document.pending, document.report, check_deadline)
            if document.report.outcome == "error":
                raise PreparationError(f"Cannot safely publish document {document.name}")
        _copy_previews(previews, check_deadline)
        (output / "report.json").write_text(encoded)
        check_deadline()
        report.artifacts.update(artifacts)
    except BaseException:
        shutil.rmtree(output)
        report.artifacts.clear()
        raise


async def run_job(
    request: JobRequest,
    progress: Callable[[str], None] | None = None,
    report: Report | None = None,
) -> Report:
    emit = progress or (lambda _message: None)
    settings = load_settings(None, request.settings.to_dict())
    report = report if report is not None else Report(request.command)
    report.settings = settings.to_dict()
    settings = effective_settings(settings)
    report.execution["check_selection"] = {
        "select": list(settings.checks.select),
        "ignore": list(settings.checks.ignore),
        "always_enforced_when_applicable": sorted(MANDATORY_CODES),
    }
    report.scope = "source analysis"
    if request.command not in {"inspect", "check", "prepare", "bib", "fmt", "pdf"}:
        raise PreparationError(f"Unknown job command: {request.command}")
    output = (
        validate_destination(request.source, request.output) if request.output is not None else None
    )
    preview_output = (
        validate_destination(request.source, request.preview_output)
        if request.preview_output
        else None
    )
    if (
        preview_output is not None
        and output is not None
        and (
            preview_output == output
            or preview_output.is_relative_to(output)
            or output.is_relative_to(preview_output)
        )
    ):
        raise PreparationError("Explicit preview output must be separate from the bundle output")
    if (
        (settings.pdf_artwork.grayscale_preview or settings.figure_artwork.grayscale_preview)
        and request.command in {"check", "pdf", "prepare"}
        and preview_output is None
        and (output is None or request.dry_run)
    ):
        raise PreparationError(
            "Grayscale previews require --preview-output, or a verified prepare --output"
        )
    if request.command == "prepare" and not request.dry_run and output is None:
        raise PreparationError("prepare requires --output (or --dry-run)")
    if request.reference_pdf and not request.reference_pdf.is_file():
        raise PreparationError(f"Reference PDF does not exist: {request.reference_pdf}")
    budget = ResourceBudget(
        settings.jobs,
        settings.memory_mb,
        build_jobs=settings.build_jobs,
        render_jobs=settings.render_jobs,
        parent_memory_mb=PARENT_MEMORY_MB,
    )
    runner = ToolRunner(
        RuntimeLimits(
            timeout_seconds=settings.timeout_seconds,
            memory_mb=settings.build_memory_mb,
            max_file_bytes=settings.max_bytes,
        ),
        budget=budget,
        source_date_epoch=settings.source_date_epoch,
    )
    started = time.monotonic()
    deadline = started + settings.job_timeout_seconds
    job_task = asyncio.current_task()

    def check_deadline() -> None:
        if job_task is not None and job_task.cancelling():
            raise asyncio.CancelledError
        if time.monotonic() >= deadline:
            raise TimeoutError

    try:
        with tempfile.TemporaryDirectory(prefix="latex-prep-") as temporary:
            work = Path(temporary).resolve()
            pending = None
            documents = None
            try:
                async with runner.job_scope(work, max_temp_bytes=settings.max_temporary_bytes):
                    async with asyncio.timeout(settings.job_timeout_seconds):
                        if settings.workflow.documents:
                            documents = await _run_documents(
                                request, settings, work, report, runner, budget, emit
                            )
                        else:
                            pending = await _run_in_workspace(
                                request, settings, work, report, runner, budget, emit
                            )
            finally:
                report.execution = {
                    **report.execution,
                    "resources": budget.statistics(),
                    "observed": runner.statistics(),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                }
                report.tools.update(runner.tool_versions)
                report.stages.sort(key=lambda item: str(item["name"]))
            resource_failure = runner.statistics().get("resource_exceeded")
            if resource_failure:
                report.findings.append(
                    Finding(
                        "execution.resources",
                        str(resource_failure),
                        "error",
                        "inconclusive",
                        suggestion="Increase the relevant limit or reduce parallelism and retry.",
                    )
                )
                report.outcome = "blocked"
                pending = None
                documents = None
            check_deadline()
            previews: tuple[tuple[Path, Path], ...] = ()
            if settings.pdf_artwork.grayscale_preview or settings.figure_artwork.grayscale_preview:
                if preview_output is not None:
                    previews = _preview_plan(report, work, preview_output)
                elif output is not None and (
                    pending is not None or (documents and not _blocked(report, settings))
                ):
                    previews = _preview_plan(report, work, output / "previews")
                _locate_previews(report, previews)
                for document in documents or []:
                    _locate_previews(document.report, previews)
            # The same redacted report is used by API callers and saved output. A
            # sanitization limit must be resolved before publishing a verified bundle.
            report.findings = selected_findings(report.findings, settings.checks)
            reviewed = apply_reviews(report, settings.reporting)
            report.findings = reviewed.findings
            report.outcome = reviewed.outcome
            public = report.redacted_copy()
            if public.outcome == "error":
                report = public
                pending = None
                documents = None
                previews = ()
            # All asynchronous readers, tool children and the monitor are drained.
            # Publication is the final synchronous critical section, never an early branch.
            published_paths: list[Path] = []
            try:
                if pending is not None:
                    assert output is not None
                    emit("Publishing the verified local output")
                    _publish(
                        output,
                        *pending,
                        report,
                        check_deadline,
                        previews=previews if preview_output is None else (),
                    )
                    if report.outcome != "error":
                        published_paths.append(output)
                elif (
                    documents
                    and request.command == "prepare"
                    and not request.dry_run
                    and not _blocked(report, settings)
                ):
                    assert output is not None
                    _publish_documents(
                        output,
                        documents,
                        report,
                        check_deadline,
                        previews=previews if preview_output is None else (),
                    )
                    published_paths.append(output)
                if preview_output is not None and previews and report.outcome != "error":
                    preview_output.mkdir()
                    published_paths.append(preview_output)
                    _copy_previews(previews, check_deadline)
                    (preview_output / "report.json").write_text(
                        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
                    )
                check_deadline()
            except BaseException:
                # Track only directories created successfully by this transaction.
                # A concurrent/pre-existing destination must never be removed.
                for published in reversed(published_paths):
                    shutil.rmtree(published)
                report.artifacts.clear()
                raise
    except TimeoutError:
        report.findings.append(
            Finding(
                "execution.deadline",
                f"Job exceeded its {settings.job_timeout_seconds}-second deadline; "
                "no bundle was published",
                "error",
                "inconclusive",
                suggestion="Increase --job-timeout-seconds or reduce the input workload and retry.",
            )
        )
        report.outcome = "blocked"
    except asyncio.CancelledError:
        report.outcome = "cancelled"
        raise
    return report.redacted_copy()
