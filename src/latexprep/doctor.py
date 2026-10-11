"""Bounded setup diagnostics using application-owned sources and normal isolation."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import PARENT_MEMORY_MB, Settings, load_settings
from .formatting import FormattingOptions, format_project
from .models import Finding, PreparationError, Report, has_blockers
from .pdf import compare_pdfs, inspect_pdf
from .pdf_artwork import _configured as configured_artwork
from .pdf_checks import _configured as configured_pdf
from .reporting import is_scope_disclaimer
from .runtime import RuntimeLimits, ToolRunner, build_project
from .scheduler import ResourceBudget

_PDF_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfdetach", "pdfimages")
_BIBLIOGRAPHY_BACKENDS = ("bibtex", "biber")
_DOCUMENT = "\\documentclass{article}\n\\begin{document}\nFledge setup test.\n"
_REFERENCE = "@article{doctor,author={Example, Alice},title={Setup test},year={2026}}\n"


def diagnostic_engines(settings: Settings) -> tuple[str, ...]:
    """Exercise every distinct engine selected for configured documents."""
    return tuple(
        sorted(
            {document.engine or settings.engine for document in settings.workflow.documents}
            or {settings.engine}
        )
    )


def _selected_tools(settings: Settings) -> tuple[set[str], set[str]]:
    required = {"latexmk", *diagnostic_engines(settings), *_PDF_TOOLS}
    if "xelatex" in required:
        required.add("xdvipdfmx")
    # Optional measurement adapters are advisory here; a version probe cannot
    # establish that a configured manuscript measurement is supported.
    optional: set[str] = set()
    if settings.bibliography_backend != "auto":
        required.add(settings.bibliography_backend)
    else:
        # The automatic strategy runs whichever backend a paper needs, so each
        # installed backend is exercised; a missing one is only an advisory.
        optional.update(_BIBLIOGRAPHY_BACKENDS)
    if settings.format:
        required.add("tex-fmt")
    pdf_rules = set(configured_pdf(settings.pdf_checks))
    if pdf_rules & {"pdf.document_language", "pdf.annotation_policy", "pdf.action_policy"}:
        optional.add("qpdf")
    if pdf_rules & {
        "pdf.stroke_width",
        "pdf.color_space_policy",
        "pdf.assumed_text_contrast",
        "pdf.grayscale_separation",
    }:
        optional.add("mutool")
    if pdf_rules & {"pdf.rendered_text_size", "pdf.printable_text_bounds", "pdf.text_overlap"}:
        optional.add("pdftohtml")
    raster_only = {
        "pdf.figure_whitespace",
        "pdf.figure_edge_ink",
        "pdf.grayscale_preview",
        "pdf.type3_glyph_programs",
        "pdf.rendered_sample_contrast",
        "figure.color_space",
    }
    for options in (settings.pdf_artwork, settings.figure_artwork):
        rules = set(configured_artwork(options))
        if "pdf.type3_glyph_programs" in rules:
            optional.add("qpdf")
        if rules - raster_only:
            optional.add("mutool")
    return required, optional


def _version_command(name: str) -> list[str]:
    if name == "latexmk":
        return [name, "-norc", "-v"]
    if name in {*_PDF_TOOLS, "pdftohtml", "mutool"}:
        return [name, "-v"]
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


def _record_stage(
    report: Report, name: str, findings: list[Finding], *, required: bool = True
) -> bool:
    """Treat absent setup evidence as a blocker, or as an advisory for optional stages."""
    bad = [
        item
        for item in findings
        if item.status in {"failed", "inconclusive", "skipped"} and not is_scope_disclaimer(item)
    ]
    report.stages.append({"name": name, "status": "failed" if bad else "passed"})
    if bad:
        for item in bad:
            report.findings.append(
                Finding(
                    "doctor.fixture_failed",
                    f"Built-in setup fixture, {name}: {item.message}",
                    "error" if required else "warning",
                    item.status,
                    suggestion="Repair the local toolchain or execution limits and rerun "
                    "fledge doctor. This result does not diagnose your manuscript."
                    + (
                        ""
                        if required
                        else " Only papers that need this stage's tools are affected."
                    ),
                    details={"stage": name, "diagnostic_rule": item.rule, **item.details},
                )
            )
        return False
    report.findings.append(
        Finding(
            "doctor.fixture_passed",
            f"Built-in setup fixture: {name} passed.",
            "info",
            "passed",
            details={"stage": name},
        )
    )
    return True


async def run_doctor(
    settings: Settings | None = None, *, progress: Callable[[str], None] | None = None
) -> Report:
    """Diagnose selected setup; never read or compile a user's manuscript."""
    settings = load_settings(None, (settings or Settings()).to_dict())
    report = Report("doctor", scope="setup diagnostics with a built-in fixture")
    report.settings = settings.to_dict()
    required, optional = _selected_tools(settings)
    engines = diagnostic_engines(settings)
    report.execution.update(
        {
            "engines": list(engines),
            "manuscript_compiled": False,
            "bibliography_backend": settings.bibliography_backend,
            "bibliography_exercised": False,
            "optional_adapters": "installation/version only; configured measurements not exercised",
            "sandbox_backend": {"darwin": "macOS sandbox-exec", "linux": "Linux Bubblewrap"}.get(
                sys.platform, "unavailable"
            ),
        }
    )
    emit = progress or (lambda _message: None)
    for name in sorted(required | optional):
        executable = shutil.which(name)
        report.tools[name] = {
            "installed": executable is not None,
            "executable": executable,
            "required": name in required,
            "version_verified": False,
            "exercised": False,
        }
        if executable is None:
            report.findings.append(
                Finding(
                    "doctor.tool_missing",
                    f"{'Required' if name in required else 'Optional'} "
                    f"tool is unavailable: {name}.",
                    "error" if name in required else "warning",
                    "inconclusive",
                    suggestion=f"Install {name} explicitly and ensure it is on PATH, then rerun "
                    "fledge doctor.",
                    details={"tool": name, "required": name in required},
                )
            )
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
    try:
        with tempfile.TemporaryDirectory(prefix="fledge-doctor-") as temporary:
            root = Path(temporary).resolve()
            async with runner.job_scope(root, max_temp_bytes=settings.max_temporary_bytes):
                async with asyncio.timeout(settings.job_timeout_seconds):
                    await _diagnose(
                        report, root, settings, runner, budget, required, optional, emit
                    )
    except TimeoutError:
        report.findings.append(
            Finding(
                "execution.deadline",
                f"Setup diagnostics exceeded the "
                f"{settings.job_timeout_seconds}-second job deadline.",
                "error",
                "inconclusive",
                suggestion="Adjust job_timeout_seconds in your config and rerun fledge doctor.",
            )
        )
    except asyncio.CancelledError:
        report.outcome = "cancelled"
        raise
    except (PreparationError, OSError) as error:
        report.findings.append(
            Finding(
                "doctor.execution_unavailable",
                f"Setup diagnostics could not complete: {error}",
                "error",
                "inconclusive",
                suggestion="Resolve the local execution failure and rerun "
                "fledge doctor. No manuscript was compiled.",
            )
        )
    finally:
        for name, version in runner.tool_versions.items():
            if name in report.tools and isinstance(version, dict):
                inventory = report.tools[name]
                assert isinstance(inventory, dict)
                inventory.update(version)
                inventory["version_verified"] = True
        observed = runner.statistics()
        report.execution.update(
            {
                "resources": budget.statistics(),
                "observed": observed,
                "elapsed_seconds": round(time.monotonic() - started, 6),
            }
        )
        # job_scope takes a final sample on exit. A late limit or accounting
        # failure invalidates setup evidence even if every fixture operation passed.
        resource_failure = observed.get("resource_exceeded")
        if resource_failure:
            report.findings.append(
                Finding(
                    "execution.resources",
                    f"Setup diagnostics could not satisfy the runtime resource checks: "
                    f"{resource_failure}",
                    "error",
                    "inconclusive",
                    suggestion="Resolve the stated resource or accounting failure, adjust "
                    "the relevant memory or temporary-space limit if needed, and rerun "
                    "fledge doctor.",
                )
            )
    report.outcome = (
        "blocked"
        if has_blockers(report.findings)
        else (
            "passed_with_advisories"
            if any(
                item.severity == "warning" and item.status != "passed" for item in report.findings
            )
            else "passed"
        )
    )
    return report.redacted_copy()


async def _diagnose(
    report: Report,
    root: Path,
    settings: Settings,
    runner: ToolRunner,
    budget: ResourceBudget,
    required: set[str],
    optional: set[str],
    emit: Callable[[str], None],
) -> None:
    emit("Testing restricted execution with a built-in command")
    sandbox_work = root / "sandbox"
    sandbox_work.mkdir()
    try:
        result = await runner.run(["true"], cwd=sandbox_work, workspace=sandbox_work)
        if (
            result.returncode
            or result.timed_out
            or result.output_limited
            or result.resource_exceeded
        ):
            raise PreparationError(
                "Sandbox probe did not complete "
                f"(exit={result.returncode}, timeout={result.timed_out}, "
                f"output_limit={result.output_limited}, resource={result.resource_exceeded})."
            )
    except PreparationError as error:
        report.findings.append(
            Finding(
                "doctor.sandbox_unavailable",
                f"Restricted execution is unavailable: {error}",
                "error",
                "inconclusive",
                suggestion="Run on a host where macOS sandbox-exec or Linux Bubblewrap with user "
                "namespaces can start. Fledge never falls back to unrestricted execution.",
            )
        )
        report.stages.append({"name": "sandbox", "status": "inconclusive"})
        return
    report.stages.append({"name": "sandbox", "status": "passed"})
    report.findings.append(
        Finding(
            "doctor.sandbox",
            "Restricted execution started successfully.",
            "info",
            "passed",
        )
    )
    for name in sorted(required | optional):
        inventory = report.tools[name]
        assert isinstance(inventory, dict)
        if not inventory["installed"] or name == "biber":
            # Biber needs the runtime's sealed toolchain preparation during build.
            continue
        work = root / f"version-{name}"
        work.mkdir()
        try:
            inventory.update(await runner.tool_version(_version_command(name), work))
            inventory["version_verified"] = True
        except PreparationError as error:
            report.findings.append(
                Finding(
                    "doctor.tool_unavailable",
                    f"Installed tool {name} could not report its "
                    f"version under restricted execution: {error}",
                    "error" if name in required else "warning",
                    "inconclusive",
                    suggestion=f"Repair {name} or its sandbox-accessible resources and rerun "
                    "fledge doctor.",
                    details={"tool": name, "required": name in required},
                )
            )
    source = root / "source"
    _fixture(source, settings.bibliography_backend)
    if settings.format and _ready(report, {"tex-fmt"}):
        emit("Testing formatting on the built-in source")
        formatted = await format_project(
            source,
            root / "format",
            runner,
            replace(settings, formatting_options=FormattingOptions()),
            budget=budget,
        )
        if not formatted.contents and not has_blockers(formatted.findings):
            formatted.findings.append(
                Finding(
                    "doctor.format_evidence",
                    "Formatter produced no source output.",
                    "error",
                    "inconclusive",
                )
            )
        if _record_stage(report, "formatting", formatted.findings):
            _exercised(report, {"tex-fmt"})
    for engine in diagnostic_engines(settings):
        build_tools = {"latexmk", engine}
        if engine == "xelatex":
            build_tools.add("xdvipdfmx")
        if settings.bibliography_backend != "auto":
            build_tools.add(settings.bibliography_backend)
        if not _ready(report, build_tools):
            report.stages.append({"name": f"{engine}: compilation", "status": "skipped"})
            continue
        emit(f"Compiling the built-in setup fixture with {engine}")
        async with budget.lease(
            f"doctor {engine}", memory_mb=settings.build_memory_mb, kind="build"
        ):
            built = await build_project(
                source,
                "main.tex",
                root / f"build-{engine}",
                engine,
                runner,
                # No bibliography in auto's fixture. Select BibTeX isolation so
                # the normal builder does not warm up an unused installed Biber.
                bibliography_backend=(
                    "bibtex"
                    if settings.bibliography_backend == "auto"
                    else settings.bibliography_backend
                ),
            )
        findings = built.findings
        if (not built.success or built.pdf is None) and not has_blockers(findings):
            findings = [
                *findings,
                Finding(
                    "doctor.build_failed",
                    "Build did not produce a successful PDF result.",
                    "error",
                ),
            ]
        passed = _record_stage(report, f"{engine}: compilation", findings)
        if not built.success or not passed or built.pdf is None:
            continue
        _exercised(report, build_tools)
        report.execution["bibliography_exercised"] = settings.bibliography_backend != "auto"
        if not _ready(report, set(_PDF_TOOLS)):
            report.stages.append({"name": f"{engine}: PDF pipeline", "status": "skipped"})
            continue
        emit(f"Inspecting, extracting text and rendering the built-in {engine} PDF")
        inspected = await inspect_pdf(built.pdf, root / f"inspect-{engine}", runner, budget=budget)
        compared = await compare_pdfs(
            built.pdf,
            built.pdf,
            root / f"compare-{engine}",
            runner,
            budget=budget,
        )
        expected = {"pdf.pages", "pdf.text", "compare.text", "compare.rendering"}
        if not expected <= {
            item.rule for item in [*inspected, *compared] if item.status == "passed"
        }:
            compared.append(
                Finding(
                    "doctor.pdf_evidence",
                    "Required page, text or rendered evidence is incomplete.",
                    "error",
                    "inconclusive",
                )
            )
        if _record_stage(report, f"{engine}: PDF pipeline", [*inspected, *compared]):
            _exercised(report, set(_PDF_TOOLS))
    if settings.bibliography_backend == "auto":
        await _diagnose_bibliographies(report, root, settings, runner, budget, emit)


async def _diagnose_bibliographies(
    report: Report,
    root: Path,
    settings: Settings,
    runner: ToolRunner,
    budget: ResourceBudget,
    emit: Callable[[str], None],
) -> None:
    """Compile a tiny cited document through each installed backend as an optional stage.

    Biber on macOS runs from a prepared toolchain copy, the most host-specific
    part of a build, so this is where a broken installation shows up first.
    """
    engine = diagnostic_engines(settings)[0]
    for backend in _BIBLIOGRAPHY_BACKENDS:
        stage = f"{engine}: {backend} bibliography"
        tools = {"latexmk", engine, backend}
        if engine == "xelatex":
            tools.add("xdvipdfmx")
        if not _ready(report, tools):
            report.stages.append({"name": stage, "status": "skipped"})
            continue
        source = root / f"source-{backend}"
        _fixture(source, backend)
        emit(f"Compiling the built-in {backend} bibliography fixture with {engine}")
        async with budget.lease(
            f"doctor {backend}", memory_mb=settings.build_memory_mb, kind="build"
        ):
            built = await build_project(
                source,
                "main.tex",
                root / f"build-{backend}",
                engine,
                runner,
                bibliography_backend=backend,
            )
        findings = built.findings
        if (not built.success or built.pdf is None) and not has_blockers(findings):
            findings = [
                *findings,
                Finding(
                    "doctor.build_failed",
                    "Build did not produce a successful PDF result.",
                    "error",
                ),
            ]
        if _record_stage(report, stage, findings, required=False) and built.success:
            _exercised(report, tools)
            report.execution["bibliography_exercised"] = True


def _ready(report: Report, names: set[str]) -> bool:
    for name in names:
        item = report.tools[name]
        assert isinstance(item, dict)
        if not item["installed"] or (name != "biber" and not item["version_verified"]):
            return False
    return True


def _exercised(report: Report, names: set[str]) -> None:
    for name in names:
        item = report.tools[name]
        assert isinstance(item, dict)
        item["exercised"] = True
