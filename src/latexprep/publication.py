"""Publish verified packages, previews and reports into new directories outside the input.

Publication is one transaction: it creates its directories exclusively and
removes exactly those again when any later step fails.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Self

from .file_safety import is_contained_regular_file
from .models import Finding, Outcome, PreparationError, Report, RequestError

# Workspace preview images and the published path of each.
PreviewPlan = tuple[tuple[Path, Path], ...]
_MAX_PREVIEW_BYTES = 16_000_000
_MAX_PREVIEWS = 10000
# Each package directory holds these files; the keys name them in report artifacts.
_PACKAGE_FILES = (
    ("sources", "sources"),
    ("archive", "submission.zip"),
    ("pdf", "manuscript.pdf"),
    ("report", "report.json"),
)


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
            f"Choose a {option} path outside the input project; originals are never written.",
        )
    if not resolved.parent.is_dir():
        raise RequestError(
            f"Output parent directory does not exist: {resolved.parent}",
            f"Create the parent directory first, or choose a {option} path in an existing one.",
        )
    return resolved


class OutputTransaction:
    """Directories one publication creates; a failure removes every one of them.

    Only directories created by ``create`` are removed, never previous or concurrent data.
    """

    def __init__(self) -> None:
        self._created: list[Path] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            for path in reversed(self._created):
                shutil.rmtree(path)

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
        with destination.open("xb") as stream:
            stream.write(source.read_bytes())


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
    artifacts = {
        **report.artifacts,
        **{kind: str(output / filename) for kind, filename in _PACKAGE_FILES},
    }
    # Include final paths before the first publication mutation: paths themselves
    # can contain credentials or push the complete report beyond redaction limits.
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error":
        report.outcome = Outcome.ERROR
        report.artifacts.clear()
        report.findings.append(
            Finding(
                "report.redaction_limit",
                "The final report could not be safely represented; no output was published.",
                severity="error",
                status="inconclusive",
                suggestion="Use an output path without credential values or reduce report size.",
            )
        )
        return False
    text = _json_text(public)
    check_deadline()
    transaction.create(output)
    try:
        shutil.copytree(package.sources, output / "sources")
        check_deadline()
        shutil.copyfile(package.archive, output / "submission.zip")
        shutil.copyfile(package.pdf, output / "manuscript.pdf")
        _copy_previews(previews, check_deadline)
        check_deadline()
        _write_new_text(output / "report.json", text)
        report.artifacts.update(artifacts)
        check_deadline()
    except BaseException:
        report.artifacts.clear()
        raise
    return True


def publish_documents(
    transaction: OutputTransaction,
    output: Path,
    documents: Sequence[DocumentResult],
    report: Report,
    check_deadline: Callable[[], None],
    *,
    previews: PreviewPlan = (),
) -> None:
    """Publish each verified document package beside the combined report."""
    artifacts = {
        **report.artifacts,
        **{
            f"{document.name}.{kind}": str(output / document.name / filename)
            for document in documents
            for kind, filename in _PACKAGE_FILES
        },
    }
    artifacts["report"] = str(output / "report.json")
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error":
        raise PreparationError(
            "Final multi-document report cannot be safely represented; no output published"
        )
    text = _json_text(public)
    check_deadline()
    transaction.create(output)
    try:
        for document in documents:
            assert document.package is not None
            if not publish_package(
                transaction,
                output / document.name,
                document.package,
                document.report,
                check_deadline,
            ):
                raise PreparationError(f"Cannot safely publish document {document.name}")
        _copy_previews(previews, check_deadline)
        _write_new_text(output / "report.json", text)
        check_deadline()
        report.artifacts.update(artifacts)
    except BaseException:
        report.artifacts.clear()
        raise


def publish_previews(
    transaction: OutputTransaction,
    destination: Path,
    previews: PreviewPlan,
    report: Report,
    check_deadline: Callable[[], None],
) -> None:
    """Write the previews and the final report into the new explicit preview directory."""
    transaction.create(destination, "Preview output")
    _copy_previews(previews, check_deadline)
    _write_new_text(destination / "report.json", report.to_json())


def plan_previews(report: Report, work: Path, destination: Path) -> PreviewPlan:
    """Number every preview image the findings reference for export to ``destination``."""
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
                if path.suffix != ".png" or not is_contained_regular_file(work, path):
                    raise PreparationError("Preview adapter returned an invalid artifact")
                if path.stat().st_size > _MAX_PREVIEW_BYTES or len(paths) >= _MAX_PREVIEWS:
                    raise PreparationError("Preview artifacts exceed bounded export limits")
                paths.append(path)
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
