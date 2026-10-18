"""Bounded setup diagnostics using application-owned sources and normal isolation.

Owns the ``fledge doctor`` job: it checks which tools are installed, starts a command in
the sandbox, and builds and inspects a tiny built-in document with each selected engine
and bibliography backend. It never reads or compiles a researcher's manuscript, and a
pass says nothing about the manuscript. The command-line adapter is ``doctor_command``.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from .build_result import BuildResult
from .command_runner import ToolVersion
from .config import Settings
from .errors import PreparationError
from .findings import Finding, has_blockers
from .formatting import FormattingOptions, format_project
from .job_execution import JobDeadlineExceeded, JobExecution
from .latexmk_build import build_project
from .pdf_artwork_options import required_artwork_tools
from .pdf_check_options import required_pdf_check_tools
from .pdf_comparison import COMPARISON_TOOLS, compare_pdfs
from .pdf_inspection import inspect_pdf, required_inspection_tools
from .pdf_structure_checks import required_structure_tools
from .pdf_tools import PDF_TOOLS, pdf_tool_version_argv
from .report import Outcome, Report, StageStatus, outcome_from_findings
from .resource_budget import ResourceBudget
from .runtime import ToolRunner
from .setting_choices import BibliographyBackend, Engine

# The built-in PDF stage inspects and compares one document, so these tools are required.
_PDF_PIPELINE_TOOLS = frozenset(required_inspection_tools() | COMPARISON_TOOLS)
_BIBLIOGRAPHY_BACKENDS: tuple[BibliographyBackend, ...] = ("bibtex", "biber")
_DOCUMENT = "\\documentclass{article}\n\\begin{document}\nFledge setup test.\n"
_REFERENCE = "@article{doctor,author={Example, Alice},title={Setup test},year={2026}}\n"
_SANDBOX_BACKENDS = {"darwin": "macOS sandbox-exec", "linux": "Linux Bubblewrap"}
# The PDF findings that show the built-in PDF was inspected, extracted and rendered.
_PDF_EVIDENCE_RULES = frozenset({"pdf.pages", "pdf.text", "compare.text", "compare.rendering"})


class ToolInventory(TypedDict):
    """What doctor learned about one tool; this is the entry of ``Report.tools``."""

    installed: bool
    executable: str | None
    required: bool
    version_verified: bool
    exercised: bool
    version: NotRequired[list[str]]
    version_arguments: NotRequired[list[str]]


@dataclass
class _DoctorRun:
    """Everything one diagnostic run shares: where it writes, what it runs with, what it saw."""

    report: Report
    root: Path
    settings: Settings
    runner: ToolRunner
    budget: ResourceBudget
    emit: Callable[[str], None]
    required: frozenset[str]
    tools: dict[str, ToolInventory]

    def ready(self, names: set[str]) -> bool:
        """Whether every tool is installed and, apart from biber, has a verified version."""
        return all(
            self.tools[name]["installed"]
            and (name == "biber" or self.tools[name]["version_verified"])
            for name in names
        )

    def mark_exercised(self, names: set[str]) -> None:
        for name in names:
            self.tools[name]["exercised"] = True

    def add_stage(self, name: str, status: StageStatus) -> None:
        self.report.stages.append({"name": name, "status": status})

    def skip_stage(self, name: str) -> None:
        self.add_stage(name, "skipped")

    def record_stage(self, name: str, findings: list[Finding], *, required: bool = True) -> bool:
        """Treat absent setup evidence as a blocker, or as an advisory for optional stages."""
        bad = [
            item
            for item in findings
            if item.status in {"failed", "inconclusive", "skipped"} and not item.is_scope_disclaimer
        ]
        self.add_stage(name, "failed" if bad else "passed")
        if bad:
            self.report.findings.extend(
                _failed_fixture_finding(name, item, required=required) for item in bad
            )
            return False
        self.report.findings.append(
            Finding(
                "doctor.fixture_passed",
                f"Built-in setup fixture: {name} passed.",
                severity="info",
                status="passed",
                details={"stage": name},
            )
        )
        return True


def _failed_fixture_finding(stage: str, item: Finding, *, required: bool) -> Finding:
    """The doctor finding for a failing result of a built-in fixture stage."""
    advisory = "" if required else " Only papers that need this stage's tools are affected."
    return Finding(
        "doctor.fixture_failed",
        f"Built-in setup fixture, {stage}: {item.message}",
        severity="error" if required else "warning",
        status=item.status,
        suggestion="Repair the local toolchain or execution limits and rerun "
        "fledge doctor. This result does not diagnose your manuscript." + advisory,
        details={"stage": stage, "diagnostic_rule": item.rule, **item.details},
    )


def diagnostic_engines(settings: Settings) -> tuple[Engine, ...]:
    """Exercise every distinct engine selected for configured documents."""
    return tuple(
        sorted(
            {document.engine or settings.engine for document in settings.workflow.documents}
            or {settings.engine}
        )
    )


def _build_tool_names(engine: Engine, backend: str | None = None) -> set[str]:
    """The tools one build with ``engine`` runs, and the bibliography ``backend`` if any."""
    names = {"latexmk", engine}
    if engine == "xelatex":
        names.add("xdvipdfmx")
    if backend is not None:
        names.add(backend)
    return names


def selected_tools(settings: Settings) -> tuple[set[str], set[str]]:
    """The tools a diagnostic run must find, and the optional ones the settings select."""
    required = set(_PDF_PIPELINE_TOOLS)
    for engine in diagnostic_engines(settings):
        required |= _build_tool_names(engine)
    # Optional measurement adapters are advisory here; a version probe cannot
    # establish that a configured manuscript measurement is supported.
    optional: set[str] = set()
    if settings.bibliography_backend != "auto":
        required.add(settings.bibliography_backend)
    else:
        # The automatic strategy runs whichever backend a paper needs, so each
        # installed backend is exercised; a missing one is only an advisory.
        optional.update(_BIBLIOGRAPHY_BACKENDS)
    if settings.apply_formatting:
        required.add("tex-fmt")
    # Each adapter names the tools its configured checks run; those beyond the pipeline
    # (qpdf, MuPDF, pdftohtml) are advisory.
    optional.update(
        required_pdf_check_tools(settings.pdf_checks)
        | required_structure_tools(settings.structure_checks)
        | required_artwork_tools(settings.rendered_pdf_artwork)
        | required_artwork_tools(settings.included_figure_artwork)
    )
    optional -= required
    return required, optional


def _version_command(name: str) -> list[str]:
    if name == "latexmk":
        return [name, "-norc", "-v"]
    if name in PDF_TOOLS:
        return pdf_tool_version_argv(name)
    return [name, "--version"]


def _fixture(source: Path, backend: str) -> None:
    source.mkdir()
    document = _DOCUMENT
    if backend == "bibtex":
        document += "\\cite{doctor}\\bibliographystyle{plain}\\bibliography{references}\n"
    elif backend == "biber":
        document = document.replace(
            "\\begin{document}",
            "\\usepackage[backend=biber]{biblatex}\n"
            "\\addbibresource{references.bib}\n\\begin{document}",
        )
        document += "\\cite{doctor}\\printbibliography\n"
    (source / "main.tex").write_text(document + "\\end{document}\n", encoding="utf-8")
    if backend != "auto":
        (source / "references.bib").write_text(_REFERENCE, encoding="utf-8")


def _execution_metadata(settings: Settings) -> dict[str, object]:
    """What this run is, for ``Report.execution``; the run adds its statistics at the end."""
    return {
        "engines": list(diagnostic_engines(settings)),
        "manuscript_compiled": False,
        "bibliography_backend": settings.bibliography_backend,
        "bibliography_exercised": False,
        "optional_adapters": "installation/version only; configured measurements not exercised",
        "sandbox_backend": _SANDBOX_BACKENDS.get(sys.platform, "unavailable"),
    }


def _survey_tools(
    report: Report, required: set[str], optional: set[str]
) -> dict[str, ToolInventory]:
    """Which tools are installed, with a finding for each missing one."""
    tools: dict[str, ToolInventory] = {}
    for name in sorted(required | optional):
        executable = shutil.which(name)
        tools[name] = {
            "installed": executable is not None,
            "executable": executable,
            "required": name in required,
            "version_verified": False,
            "exercised": False,
        }
        report.tools[name] = tools[name]
        if executable is None:
            report.findings.append(
                Finding(
                    "doctor.tool_missing",
                    f"{'Required' if name in required else 'Optional'} "
                    f"tool is unavailable: {name}.",
                    severity="error" if name in required else "warning",
                    status="inconclusive",
                    suggestion=f"Install {name} explicitly and ensure it is on PATH, then rerun "
                    "fledge doctor.",
                    details={"tool": name, "required": name in required},
                )
            )
    return tools


def _record_version(inventory: ToolInventory, version: ToolVersion) -> None:
    """Add the version a tool printed under restricted execution to its inventory."""
    inventory["version"] = list(version.version)
    inventory["executable"] = version.executable
    inventory["version_arguments"] = list(version.version_arguments)
    inventory["version_verified"] = True


def _deadline_finding(settings: Settings) -> Finding:
    return Finding(
        "execution.deadline",
        f"Setup diagnostics exceeded the {settings.job_timeout_seconds}-second job deadline.",
        severity="error",
        status="inconclusive",
        suggestion="Adjust job_timeout_seconds in your config and rerun fledge doctor.",
    )


def _execution_unavailable_finding(error: Exception) -> Finding:
    return Finding(
        "doctor.execution_unavailable",
        f"Setup diagnostics could not complete: {error}",
        severity="error",
        status="inconclusive",
        suggestion="Resolve the local execution failure and rerun "
        "fledge doctor. No manuscript was compiled.",
    )


def _resource_failure_finding(failure: str) -> Finding:
    return Finding(
        "execution.resources",
        f"Setup diagnostics could not satisfy the runtime resource checks: {failure}",
        severity="error",
        status="inconclusive",
        suggestion="Resolve the stated resource or accounting failure, adjust "
        "the relevant memory or temporary-space limit if needed, and rerun "
        "fledge doctor.",
    )


async def run_doctor(
    settings: Settings | None = None, *, progress: Callable[[str], None] | None = None
) -> Report:
    """Diagnose selected setup; never read or compile a user's manuscript."""
    settings = settings or Settings()
    report = Report("doctor", scope="setup diagnostics with a built-in fixture")
    report.settings = settings.to_dict()
    report.execution.update(_execution_metadata(settings))
    required, optional = selected_tools(settings)
    tools = _survey_tools(report, required, optional)
    execution = JobExecution(settings)
    try:
        with execution.workspace("fledge-doctor-") as root:
            async with execution.bounded(root):
                run = _DoctorRun(
                    report=report,
                    root=root,
                    settings=settings,
                    runner=execution.runner,
                    budget=execution.budget,
                    emit=progress or (lambda _message: None),
                    required=frozenset(required),
                    tools=tools,
                )
                await _diagnose(run)
    except JobDeadlineExceeded:
        report.findings.append(_deadline_finding(settings))
    except asyncio.CancelledError:
        report.outcome = Outcome.CANCELLED
        raise
    except (PreparationError, OSError) as error:
        report.findings.append(_execution_unavailable_finding(error))
    finally:
        for name, version in execution.runner.tool_versions.items():
            if name in tools:
                _record_version(tools[name], version)
        report.execution.update(execution.statistics())
        # job_scope takes a final sample on exit. A late limit or accounting
        # failure invalidates setup evidence even if every fixture operation passed.
        if resource_failure := execution.runner.statistics().resource_exceeded:
            report.findings.append(_resource_failure_finding(resource_failure))
    report.outcome = outcome_from_findings(report.findings, blocked=has_blockers(report.findings))
    return report.redacted_copy()


async def _probe_sandbox(run: _DoctorRun) -> bool:
    """Start a built-in command under restricted execution; False if the sandbox refuses."""
    run.emit("Testing restricted execution with a built-in command")
    work = run.root / "sandbox"
    work.mkdir()
    try:
        result = await run.runner.run(["true"], cwd=work, workspace=work)
        if (reason := result.failure_reason("the sandbox probe")) is not None:
            raise PreparationError(reason)
    except PreparationError as error:
        run.report.findings.append(
            Finding(
                "doctor.sandbox_unavailable",
                f"Restricted execution is unavailable: {error}",
                severity="error",
                status="inconclusive",
                suggestion="Run on a host where macOS sandbox-exec or Linux Bubblewrap with user "
                "namespaces can start. Fledge never falls back to unrestricted execution.",
            )
        )
        run.add_stage("sandbox", "inconclusive")
        return False
    run.add_stage("sandbox", "passed")
    run.report.findings.append(
        Finding(
            "doctor.sandbox",
            "Restricted execution started successfully.",
            severity="info",
            status="passed",
        )
    )
    return True


async def _verify_tool_versions(run: _DoctorRun) -> None:
    """Ask each installed tool for its version under restricted execution."""
    for name, inventory in run.tools.items():
        if not inventory["installed"] or name == "biber":
            # Biber needs the runtime's sealed toolchain preparation during build.
            continue
        work = run.root / f"version-{name}"
        work.mkdir()
        try:
            _record_version(inventory, await run.runner.tool_version(_version_command(name), work))
        except PreparationError as error:
            run.report.findings.append(
                Finding(
                    "doctor.tool_unavailable",
                    f"Installed tool {name} could not report its "
                    f"version under restricted execution: {error}",
                    severity="error" if name in run.required else "warning",
                    status="inconclusive",
                    suggestion=f"Repair {name} or its sandbox-accessible resources and rerun "
                    "fledge doctor.",
                    details={"tool": name, "required": name in run.required},
                )
            )


async def _diagnose_formatting(run: _DoctorRun, source: Path) -> None:
    """Format the built-in source with tex-fmt, when formatting is configured and available."""
    if not (run.settings.apply_formatting and run.ready({"tex-fmt"})):
        return
    run.emit("Testing formatting on the built-in source")
    # The configured line format, without project exclusions or blank-line policy.
    formatted = await format_project(
        source,
        run.root / "format",
        run.runner,
        FormattingOptions(
            wrap_length=run.settings.formatting_options.wrap_length,
            tab_size=run.settings.formatting_options.tab_size,
        ),
        budget=run.budget,
    )
    if not formatted.contents and not has_blockers(formatted.findings):
        formatted.findings.append(
            Finding(
                "doctor.format_evidence",
                "Formatter produced no source output.",
                severity="error",
                status="inconclusive",
            )
        )
    if run.record_stage("formatting", formatted.findings):
        run.mark_exercised({"tex-fmt"})


async def _build_fixture(
    run: _DoctorRun, source: Path, engine: Engine, backend: BibliographyBackend, label: str
) -> tuple[BuildResult, list[Finding]]:
    """Build the fixture in ``source`` under a build lease; returns the result and its findings.

    A build that produced no PDF always carries a blocking finding, even if the build
    reported none.
    """
    async with run.budget.lease(
        f"doctor {label}", memory_mb=run.settings.build_memory_mb, kind="build"
    ):
        built = await build_project(
            source,
            "main.tex",
            run.root / f"build-{label}",
            engine,
            run.runner,
            bibliography_backend=backend,
        )
    findings = built.findings
    if (not built.success or built.pdf is None) and not has_blockers(findings):
        findings = [
            *findings,
            Finding(
                "doctor.build_failed",
                "Build did not produce a successful PDF result.",
                severity="error",
            ),
        ]
    return built, findings


async def _compile_stage(run: _DoctorRun, source: Path, engine: Engine) -> Path | None:
    """Compile the fixture with ``engine``; returns the PDF if the stage passed."""
    backend = run.settings.bibliography_backend
    tools = _build_tool_names(engine, None if backend == "auto" else backend)
    stage = f"{engine}: compilation"
    if not run.ready(tools):
        run.skip_stage(stage)
        return None
    run.emit(f"Compiling the built-in setup fixture with {engine}")
    # No bibliography in auto's fixture. Select BibTeX isolation so the normal builder
    # does not warm up an unused installed Biber.
    built, findings = await _build_fixture(
        run, source, engine, "bibtex" if backend == "auto" else backend, engine
    )
    passed = run.record_stage(stage, findings)
    if not built.success or not passed or built.pdf is None:
        return None
    run.mark_exercised(tools)
    run.report.execution["bibliography_exercised"] = backend != "auto"
    return built.pdf


async def _pdf_pipeline_stage(run: _DoctorRun, engine: Engine, pdf: Path) -> None:
    """Inspect, extract text from and render the built PDF, and compare it with itself."""
    stage = f"{engine}: PDF pipeline"
    if not run.ready(set(_PDF_PIPELINE_TOOLS)):
        run.skip_stage(stage)
        return
    run.emit(f"Inspecting, extracting text and rendering the built-in {engine} PDF")
    inspected = await inspect_pdf(
        pdf, run.root / f"inspect-{engine}", run.runner, budget=run.budget
    )
    compared = await compare_pdfs(
        pdf, pdf, run.root / f"compare-{engine}", run.runner, budget=run.budget
    )
    seen = {item.rule for item in [*inspected, *compared] if item.status == "passed"}
    if not _PDF_EVIDENCE_RULES.issubset(seen):
        compared.append(
            Finding(
                "doctor.pdf_evidence",
                "Required page, text or rendered evidence is incomplete.",
                severity="error",
                status="inconclusive",
            )
        )
    if run.record_stage(stage, [*inspected, *compared]):
        run.mark_exercised(set(_PDF_PIPELINE_TOOLS))


async def _diagnose_bibliographies(run: _DoctorRun) -> None:
    """Compile a tiny cited document through each installed backend as an optional stage.

    Biber on macOS runs from a prepared toolchain copy, the most host-specific
    part of a build, so this is where a broken installation shows up first.
    """
    engine = diagnostic_engines(run.settings)[0]
    for backend in _BIBLIOGRAPHY_BACKENDS:
        stage = f"{engine}: {backend} bibliography"
        tools = _build_tool_names(engine, backend)
        if not run.ready(tools):
            run.skip_stage(stage)
            continue
        source = run.root / f"source-{backend}"
        _fixture(source, backend)
        run.emit(f"Compiling the built-in {backend} bibliography fixture with {engine}")
        built, findings = await _build_fixture(run, source, engine, backend, backend)
        if run.record_stage(stage, findings, required=False) and built.success:
            run.mark_exercised(tools)
            run.report.execution["bibliography_exercised"] = True


async def _diagnose(run: _DoctorRun) -> None:
    """Run every diagnostic stage; without a working sandbox nothing else can be tried."""
    if not await _probe_sandbox(run):
        return
    await _verify_tool_versions(run)
    source = run.root / "source"
    _fixture(source, run.settings.bibliography_backend)
    await _diagnose_formatting(run, source)
    for engine in diagnostic_engines(run.settings):
        if (pdf := await _compile_stage(run, source, engine)) is not None:
            await _pdf_pipeline_stage(run, engine, pdf)
    if run.settings.bibliography_backend == "auto":
        await _diagnose_bibliographies(run)
