"""Publish verified packages, previews and reports into new directories outside the input.

Publication is one transaction: it creates its directories exclusively and
removes exactly those again when any later step fails. A report that cannot be
represented safely is refused up front, before any directory exists: the refusal is
recorded in the report as a ``report.redaction_limit`` error, never raised. A step
that raises leaves ``report.artifacts`` to the caller, which clears it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import NamedTuple, Self

from .errors import PreparationError, RequestError
from .findings import Finding
from .path_safety import is_contained
from .redaction import scrub_local_paths
from .report import Outcome, Report

# Workspace preview images and the published path of each.
PreviewPlan = tuple[tuple[Path, Path], ...]
# Largest single preview image exported, in decimal megabytes (16 MB), and the most
# images one job exports: together they bound what a hostile or runaway renderer can
# make the publication step copy.
_MAX_PREVIEW_IMAGE_BYTES = 16_000_000
_MAX_PREVIEW_IMAGES = 10_000
# Each package directory holds these files; the keys name them in report artifacts.
_PACKAGE_FILES = (
    ("sources", "sources"),
    ("archive", "submission.zip"),
    ("pdf", "manuscript.pdf"),
    ("report", "report.json"),
)
_REFUSAL_SUGGESTION = "Use an output path without credential values or reduce report size."


@dataclass(frozen=True, slots=True)
class VerifiedPackage:
    """The verified sources, archive and PDF of one package, ready to publish."""

    sources: Path
    archive: Path
    pdf: Path


@dataclass(frozen=True, slots=True)
class DocumentResult:
    """One selected document's report and its package when verification passed."""

    name: str
    report: Report
    package: VerifiedPackage | None


@dataclass(frozen=True, slots=True)
class DocumentPackages:
    """The results of every selected document; all must have a package to be published."""

    results: tuple[DocumentResult, ...]


# What a finished job offers for publication: one package, the packages of several
# selected documents, or nothing because the job did not produce a verified package.
JobOutput = VerifiedPackage | DocumentPackages | None


def validate_destination(source: Path, destination: Path, option: str) -> Path:
    """Resolve a new output path outside the input; an error names the ``option`` to change."""
    resolved_source = source.resolve()
    resolved = destination.resolve()
    if destination.exists() or destination.is_symlink():
        raise RequestError(
            f"Output already exists: {destination}",
            f"Choose a new {option} path, or remove {destination} if it is no longer needed.",
        )
    if resolved_source == resolved or (
        resolved_source.is_dir() and resolved.is_relative_to(resolved_source)
    ):
        raise RequestError(
            "Output must be outside the original project",
            f"Choose a different {option} path outside the input project; originals are "
            "never written.",
        )
    if not resolved.parent.is_dir():
        raise RequestError(
            f"Output parent directory does not exist: {resolved.parent}",
            "Create the parent directory first, or choose a different "
            f"{option} path in an existing one.",
        )
    return resolved


class OutputTransaction:
    """Directories one publication creates; a failure removes every one of them.

    Only directories created by ``create`` are removed, never previous or concurrent data.
    ``progress`` receives a message when a failure starts the removal and another for each
    directory that cannot be removed. Such a leftover is also recorded in ``report`` as an
    ``execution.rollback`` finding that names it, because nothing else shows it: the error
    that caused the rollback is the one that propagates.
    """

    def __init__(
        self, progress: Callable[[str], None] | None = None, report: Report | None = None
    ) -> None:
        self._created: list[Path] = []
        self._progress = progress or (lambda _message: None)
        self._report = report

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            return
        if self._created:
            self._progress("Publication failed; removing the partial output")
        # Try every directory: one that cannot be removed must neither hide the error
        # that caused the rollback nor leave the remaining directories behind.
        for path in reversed(self._created):
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                self._report_leftover(path, error, exc_value)

    def _report_leftover(self, path: Path, error: OSError, cause: BaseException | None) -> None:
        """Say that ``path`` stayed behind: in the progress, on the error and in the report."""
        advice = (
            f"Could not remove the partial output {path} ({error}); "
            "remove it manually before running again."
        )
        self._progress(advice)
        if cause is not None:
            cause.add_note(advice)
        if self._report is not None:
            shown = scrub_local_paths(str(path))
            reason = error.strerror or type(error).__name__
            self._report.findings.append(
                Finding(
                    "execution.rollback",
                    f"Publication failed and the partial output {shown} could not be "
                    f"removed ({reason}).",
                    severity="error",
                    status="failed",
                    suggestion=f"Remove {shown} manually before running again; it holds "
                    "the unfinished output of this run.",
                )
            )

    def create(self, path: Path, label: str = "Output") -> Path:
        # mkdir is exclusive, so a concurrent creation cannot be overwritten by a rename.
        try:
            path.mkdir()
        except FileExistsError as error:
            raise PreparationError(f"{label} appeared while the job ran: {path}") from error
        self._created.append(path)
        return path


def _json_text(values: dict[str, object]) -> str:
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _write_new_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def _copy_previews(previews: PreviewPlan, check_deadline: Callable[[], None]) -> None:
    for source, destination in previews:
        check_deadline()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as origin, destination.open("xb") as stream:
            shutil.copyfileobj(origin, stream)


class _RenderedReport(NamedTuple):
    """A report as it will be published: the artifact paths it names and its JSON text."""

    artifacts: dict[str, str]
    text: str


def _render_report(report: Report, artifacts: dict[str, str]) -> _RenderedReport | None:
    """The report with its final artifact paths, or None when it cannot be represented safely.

    Final paths are included before the first publication mutation: paths themselves
    can contain credentials or push the complete report beyond redaction limits.
    """
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error":
        return None
    return _RenderedReport(artifacts, _json_text(public))


def _refuse(report: Report, message: str) -> None:
    """Record that nothing is published because the report cannot be represented safely."""
    report.outcome = Outcome.ERROR
    report.artifacts.clear()
    # No preview was published: findings must not point at exported files.
    locate_previews(report, ())
    report.findings.append(
        Finding(
            "report.redaction_limit",
            f"{message} no output was published.",
            severity="error",
            status="inconclusive",
            suggestion=_REFUSAL_SUGGESTION,
        )
    )


def _package_artifacts(report: Report, output: Path) -> dict[str, str]:
    return {
        **report.artifacts,
        **{kind: str(output / filename) for kind, filename in _PACKAGE_FILES},
    }


def _write_package(
    transaction: OutputTransaction,
    output: Path,
    package: VerifiedPackage,
    rendered: _RenderedReport,
    previews: PreviewPlan,
    check_deadline: Callable[[], None],
) -> None:
    """Create ``output`` and write one package, its previews and its rendered report."""
    check_deadline()
    transaction.create(output)
    shutil.copytree(package.sources, output / "sources")
    check_deadline()
    shutil.copyfile(package.archive, output / "submission.zip")
    shutil.copyfile(package.pdf, output / "manuscript.pdf")
    _copy_previews(previews, check_deadline)
    check_deadline()
    _write_new_text(output / "report.json", rendered.text)


def publish_package(
    transaction: OutputTransaction,
    output: Path,
    package: VerifiedPackage,
    report: Report,
    check_deadline: Callable[[], None],
    *,
    previews: PreviewPlan = (),
) -> bool:
    """Write the package, its previews and the final report into the new ``output``.

    Returns False, recording why in ``report``, when the final report cannot be
    represented safely; nothing is written then.
    """
    check_deadline()
    rendered = _render_report(report, _package_artifacts(report, output))
    if rendered is None:
        _refuse(report, "The final report could not be safely represented;")
        return False
    _write_package(transaction, output, package, rendered, previews, check_deadline)
    report.artifacts.update(rendered.artifacts)
    check_deadline()
    return True


def publish_documents(
    transaction: OutputTransaction,
    output: Path,
    documents: Sequence[DocumentResult],
    report: Report,
    check_deadline: Callable[[], None],
    *,
    previews: PreviewPlan = (),
) -> bool:
    """Publish each verified document package beside the combined report.

    Returns False, recording why in ``report``, when the combined report or the report
    of any document cannot be represented safely; nothing is written then. Every
    document must have a verified package: the caller publishes only when none is
    missing, so a missing one is a defect and raises.
    """
    verified: list[tuple[DocumentResult, VerifiedPackage]] = []
    unverified: list[str] = []
    for document in documents:
        if document.package is None:
            unverified.append(document.name)
        else:
            verified.append((document, document.package))
    if unverified:
        raise PreparationError(
            f"No package was published because {', '.join(unverified)} did not verify; "
            "resolve the blocking findings reported for the unverified documents and "
            "prepare again"
        )
    artifacts = {
        **report.artifacts,
        **{
            f"{document.name}.{kind}": str(output / document.name / filename)
            for document in documents
            for kind, filename in _PACKAGE_FILES
        },
        "report": str(output / "report.json"),
    }
    rendered = _render_report(report, artifacts)
    if rendered is None:
        _refuse(report, "The final multi-document report could not be safely represented;")
        return False
    # Render every document's report before creating anything, so a refusal leaves no output.
    rendered_documents = []
    for document, _ in verified:
        rendered_document = _render_report(
            document.report, _package_artifacts(document.report, output / document.name)
        )
        if rendered_document is None:
            _refuse(
                report,
                f"The report of document {document.name} could not be safely represented;",
            )
            return False
        rendered_documents.append(rendered_document)
    check_deadline()
    transaction.create(output)
    for (document, package), rendered_document in zip(verified, rendered_documents, strict=True):
        _write_package(
            transaction, output / document.name, package, rendered_document, (), check_deadline
        )
        document.report.artifacts.update(rendered_document.artifacts)
        check_deadline()
    _copy_previews(previews, check_deadline)
    _write_new_text(output / "report.json", rendered.text)
    check_deadline()
    report.artifacts.update(rendered.artifacts)
    return True


def publish_previews(
    transaction: OutputTransaction,
    destination: Path,
    previews: PreviewPlan,
    report: Report,
    check_deadline: Callable[[], None],
) -> bool:
    """Write the previews and the final report into the new explicit preview directory.

    Returns False, recording why in ``report``, when the final report cannot be
    represented safely; nothing is written then.
    """
    rendered = _render_report(report, report.artifacts)
    if rendered is None:
        _refuse(report, "The final report could not be safely represented;")
        return False
    transaction.create(destination, "Preview output")
    _copy_previews(previews, check_deadline)
    _write_new_text(destination / "report.json", rendered.text)
    return True


def _check_exportable_preview(work: Path, path: Path, *, exported: int) -> None:
    """Refuse a preview that is not a PNG file of the workspace, or one beyond the limits."""
    if path.suffix != ".png" or not is_contained(work, path, regular_file=True):
        raise PreparationError(
            f"A page preview is not a PNG image inside the job's workspace: {path.name}. "
            "This is a defect in a preview renderer; report it with the JSON report."
        )
    if path.stat().st_size > _MAX_PREVIEW_IMAGE_BYTES:
        raise PreparationError(
            f"The page preview {path.name} is larger than the "
            f"{_MAX_PREVIEW_IMAGE_BYTES // 1_000_000} MB limit and cannot be exported. "
            "Turn off the grayscale preview setting, or check fewer pages."
        )
    if exported >= _MAX_PREVIEW_IMAGES:
        raise PreparationError(
            f"More than {_MAX_PREVIEW_IMAGES} page previews would be exported. "
            "Turn off the grayscale preview setting, or check fewer documents at a time."
        )


def plan_previews(report: Report, work: Path, destination: Path) -> PreviewPlan:
    """Number every preview image the findings reference for export to ``destination``."""
    paths: dict[Path, None] = {}
    for finding in report.sorted_findings():
        artifacts = finding.details.get("artifacts", [])
        if not isinstance(artifacts, list):
            continue
        for name in artifacts:
            if not isinstance(name, str):
                continue
            path = Path(name)
            if path not in paths:
                _check_exportable_preview(work, path, exported=len(paths))
                paths[path] = None
    return tuple(
        (source, destination / f"page-{index:04d}.png") for index, source in enumerate(paths, 1)
    )


def locate_previews(report: Report, previews: PreviewPlan) -> None:
    """Point findings and report artifacts at the exported preview paths."""
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
