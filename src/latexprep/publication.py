"""Publish verified packages, previews and reports into directories outside the input.

Publication is one transaction, and it writes only into directories it created itself
with an exclusive ``mkdir``. A new output directory is created in place. An existing empty
directory of the user's, and, when the caller allows it, a directory that Fledge published
earlier, are never written into: the new output goes into a new sibling directory, which
takes their place once everything is written and the existing directory has been checked
to be unchanged. When any step fails, exactly what the transaction created is removed
again, and an existing directory stays as it was. Trees are removed only where the
platform's removal never follows a link. Every published directory holds
``OUTPUT_MARKER``, which is how a later run recognises it.

A report that cannot be represented safely is refused up front, before any directory
exists: the refusal is recorded in the report as a ``report.redaction_limit`` error, never
raised. A step that raises leaves ``report.artifacts`` to the caller, which clears it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import NamedTuple, Self

from . import __version__
from .errors import PreparationError, RequestError
from .findings import Finding
from .path_safety import is_contained
from .publication_folders import (
    MARKER_TEXT,
    OUTPUT_MARKER,
    STAGING_PREFIX,
    StagedOutput,
    is_empty_directory,
    is_fledge_report,
    is_own_directory,
    is_replaceable_output,
    move_back,
    move_into_place,
    new_sibling,
    remove_tree,
)
from .redaction import scrub_local_paths
from .report import WITHHELD_SCOPE, Outcome, Report

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
    """The verified sources, archive and PDF of one package, ready to publish.

    Attributes:
        sources: The verified source folder.
        archive: The verified submission ZIP.
        pdf: The verified PDF.
    """

    sources: Path
    archive: Path
    pdf: Path


@dataclass(frozen=True, slots=True)
class DocumentResult:
    """One selected document's report and its package when verification passed.

    Attributes:
        name: The document's name.
        report: The document's report.
        package: The verified package, or ``None`` when verification did not pass.
    """

    name: str
    report: Report
    package: VerifiedPackage | None


@dataclass(frozen=True, slots=True)
class DocumentPackages:
    """The results of every selected document; all must have a package to be published.

    Attributes:
        results: One result per selected document.
    """

    results: tuple[DocumentResult, ...]


# What a finished job offers for publication: one package, the packages of several
# selected documents, or nothing because the job did not produce a verified package.
JobOutput = VerifiedPackage | DocumentPackages | None


def _existing_destination_error(destination: Path, option: str, *, folder: bool) -> RequestError:
    """Build the refusal of an existing ``destination`` that may not be written to.

    Args:
        destination: The existing output path.
        option: The option that named it.
        folder: Whether ``destination`` names a directory rather than a file.

    Returns:
        The error; it suggests ``--force`` for a directory that Fledge published earlier.
    """
    if folder and is_replaceable_output(destination):
        return RequestError(
            f"Output already exists: {destination}",
            f"Choose a new {option} path, remove {destination}, or rerun with --force to "
            "replace this earlier Fledge output once the new run has succeeded.",
        )
    if folder and destination.is_dir() and not is_own_directory(destination):
        return RequestError(
            f"Output folder belongs to another user, or is a link: {destination}",
            f"Choose a new {option} path or an empty folder of your own; Fledge never writes "
            "into a folder that someone else controls.",
        )
    alternative = " or an empty folder" if folder else ""
    return RequestError(
        f"Output already exists: {destination}",
        f"Choose a new {option} path{alternative}, or remove {destination} if it is no longer "
        "needed.",
    )


def validate_destination(
    source: Path, destination: Path, option: str, *, folder: bool = False, force: bool = False
) -> Path:
    """Resolve an output path outside the input; an error names the ``option`` to change.

    The path must be new, except that a ``folder`` may be an existing empty directory that
    the current user owns. With ``force``, an earlier Fledge output may be named too: for a
    ``folder``, a directory of the user's that publication wrote (one holding
    ``OUTPUT_MARKER``); for a file, one that begins like a Fledge report. Such an output is
    replaced only once the new run has succeeded.

    Args:
        source: The input project.
        destination: The requested output path.
        option: The option that named ``destination``.
        folder: Whether ``destination`` names a directory rather than a file.
        force: Whether an earlier Fledge output at ``destination`` may be replaced.

    Returns:
        The resolved destination.

    Raises:
        RequestError: ``destination`` exists and may not be written to, is the input, lies
            inside it or contains it, or its parent folder does not exist.
    """
    resolved_source = source.resolve()
    resolved = destination.resolve()
    if destination.exists() or destination.is_symlink():
        if folder:
            acceptable = is_empty_directory(destination) or (
                force and is_replaceable_output(destination)
            )
        else:
            acceptable = force and is_fledge_report(destination)
        if not acceptable:
            raise _existing_destination_error(destination, option, folder=folder)
    if resolved_source == resolved or (
        resolved_source.is_dir() and resolved.is_relative_to(resolved_source)
    ):
        raise RequestError(
            "Output must be outside the original project",
            f"Choose a different {option} path outside the input project; originals are "
            "never written.",
        )
    if resolved_source.is_relative_to(resolved):
        raise RequestError(
            f"Output contains the input: {destination}",
            f"Choose a different {option} path; replacing it would remove the input project.",
        )
    if not resolved.parent.is_dir():
        raise RequestError(
            f"Output parent directory does not exist: {resolved.parent}",
            "Create the parent directory first, or choose a different "
            f"{option} path in an existing one.",
        )
    return resolved


class OutputTransaction:
    """Directories one publication writes; a failure removes everything it wrote.

    ``create`` makes a new directory in place. For an existing empty directory, and, with
    ``replace_previous``, for an earlier Fledge output, it makes a new sibling directory
    instead, and leaving the block without an error moves each sibling into place. A
    failure removes the directories the transaction created and nothing else, so previous
    and concurrent data stay as they were.

    Args:
        progress: Receives a message when a failure starts the removal and another for
            each directory that cannot be removed.
        report: Receives an ``execution.rollback`` finding that names each directory that
            could not be removed, because nothing else shows it: the error that caused
            the rollback is the one that propagates.
        replace_previous: Whether a directory that publication wrote earlier may be
            replaced.
    """

    def __init__(
        self,
        progress: Callable[[str], None] | None = None,
        report: Report | None = None,
        *,
        replace_previous: bool = False,
    ) -> None:
        self._created: list[Path] = []
        self._staged: list[StagedOutput] = []
        self._progress = progress or (lambda _message: None)
        self._report = report
        self._replace_previous = replace_previous

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self._commit()
            return
        self._roll_back(exc_value)

    def _roll_back(self, cause: BaseException | None) -> None:
        """Remove the directories the transaction created; announce it and name what stays.

        Args:
            cause: The error that caused the rollback, which receives a note for each
                directory that stays behind.
        """
        if self._created or self._staged:
            self._progress("Publication failed; removing the partial output")
        # Try every directory: one that cannot be removed must neither hide the error
        # that caused the rollback nor leave the remaining directories behind.
        for path in [*reversed(self._created), *(item.staging for item in self._staged)]:
            try:
                remove_tree(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                self._report_leftover(path, error, cause)

    def _commit(self) -> None:
        """Move every staged output into place; on a failure, put the existing ones back.

        Raises:
            PreparationError: An existing directory changed, or a staged output could not
                be moved into place; every existing directory is back where it was, and
                nothing new stays published.
        """
        moved: list[tuple[StagedOutput, Path | None]] = []
        for staged in self._staged:
            try:
                moved.append((staged, move_into_place(staged)))
            except OSError as error:
                for done, previous in reversed(moved):
                    if not move_back(done, previous):
                        self._progress(
                            f"Could not restore {done.destination}; its former contents are "
                            f"in {previous or 'no folder'}."
                        )
                self._roll_back(error)
                if self._report is not None:
                    self._report.artifacts.clear()
                    locate_previews(self._report, ())
                raise PreparationError(
                    f"Could not move the new output into {staged.destination} "
                    f"({error.strerror or error}); it was left as it was and nothing new "
                    "was published"
                ) from error
        for _, previous in moved:
            if previous is None:
                continue
            try:
                remove_tree(previous)
            except OSError as error:
                self._progress(
                    f"Could not remove the replaced output {previous} ({error}); remove it "
                    "manually."
                )

    def _report_leftover(self, path: Path, error: OSError, cause: BaseException | None) -> None:
        """Say that ``path`` stayed behind: in the progress, on the error and in the report.

        Args:
            path: The directory that could not be removed.
            error: Why removing it failed.
            cause: The error that caused the rollback, which receives a note, if any.
        """
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
        """Make a directory for what belongs in ``path`` and remember it for the rollback.

        Inside a directory the transaction writes, a subdirectory is made there. Otherwise
        a new ``path`` is created exclusively; for an existing empty directory, or an
        earlier Fledge output that may be replaced, a new sibling directory is created
        instead, to take its place on success. Each such top-level directory receives
        ``OUTPUT_MARKER``.

        Args:
            path: The directory to write.
            label: Names the output in the error message.

        Returns:
            Where to write what belongs in ``path``: ``path`` itself, or the sibling
            directory that takes its place.

        Raises:
            PreparationError: Something else appeared at ``path`` while the job ran.
        """
        written = self.physical(path)
        if self._inside_output(path):
            written.mkdir()
            return written
        if path.exists() or path.is_symlink():
            written = self._stage(path, label)
        else:
            # mkdir is exclusive, so a concurrent creation is never written into.
            try:
                path.mkdir()
            except FileExistsError as error:
                raise PreparationError(f"{label} appeared while the job ran: {path}") from error
            self._created.append(path)
        _write_new_text(written / OUTPUT_MARKER, MARKER_TEXT.format(version=__version__))
        return written

    def _stage(self, path: Path, label: str) -> Path:
        """Create the sibling directory that takes the place of the existing ``path``.

        Args:
            path: The existing directory: empty, or an earlier output that may be replaced.
            label: Names the output in the error message.

        Returns:
            The new sibling directory.

        Raises:
            PreparationError: ``path`` is neither an empty directory of the user's nor a
                replaceable earlier output.
        """
        if is_empty_directory(path):
            replaces = False
        elif self._replace_previous and is_replaceable_output(path):
            replaces = True
        else:
            raise PreparationError(f"{label} changed while the job ran: {path}")
        staging = new_sibling(path, STAGING_PREFIX)
        self._staged.append(StagedOutput(staging, path, replaces))
        return staging

    def _inside_output(self, path: Path) -> bool:
        """Return whether ``path`` lies below a top-level directory this transaction writes."""
        roots = [*self._created, *(staged.destination for staged in self._staged)]
        return any(path != root and path.is_relative_to(root) for root in roots)

    def physical(self, path: Path) -> Path:
        """Return where to write ``path``: inside the sibling that takes its place, or ``path``.

        Args:
            path: A path at or below an output of this transaction.

        Returns:
            The corresponding path inside the sibling directory when ``path`` lies in an
            existing directory whose place a staged output takes, otherwise ``path``.
        """
        for staged in self._staged:
            if path.is_relative_to(staged.destination):
                return staged.staging / path.relative_to(staged.destination)
        return path


def _json_text(values: dict[str, object]) -> str:
    """Return ``values`` as indented UTF-8 JSON text ending in a newline."""
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _write_new_text(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 to the new file ``path``, which must not exist yet."""
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def _written_previews(transaction: OutputTransaction, previews: PreviewPlan) -> PreviewPlan:
    """Return the preview plan with each published path replaced by where it is written.

    Args:
        transaction: The publication, which says where each published path is written.
        previews: Each preview image with its published path.

    Returns:
        Each preview image with the path it is copied to now.
    """
    return tuple((source, transaction.physical(published)) for source, published in previews)


def _copy_previews(previews: PreviewPlan, check_deadline: Callable[[], None]) -> None:
    """Copy each preview image to its path, which must not exist yet.

    Args:
        previews: Each preview image with the path it is copied to.
        check_deadline: Called before each copy to enforce the job's deadline.
    """
    for source, destination in previews:
        check_deadline()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as origin, destination.open("xb") as stream:
            shutil.copyfileobj(origin, stream)


class _RenderedReport(NamedTuple):
    """A report as it will be published: the artifact paths it names and its JSON text.

    Attributes:
        artifacts: The artifact paths the report names.
        text: The JSON text.
    """

    artifacts: dict[str, str]
    text: str


def _render_report(report: Report, artifacts: dict[str, str]) -> _RenderedReport | None:
    """Render the report with its final artifact paths.

    Final paths are included before the first publication mutation: paths themselves
    can contain credentials or push the complete report beyond redaction limits.
    Sanitizing withholds such a report and puts an error in its place. Only that is
    refused: a run that ended in a defect also has the error outcome, and its report is
    rendered like any other.

    Args:
        report: The final report.
        artifacts: The artifact paths the published report names.

    Returns:
        The rendered report, or ``None`` when it cannot be represented safely.
    """
    public = replace(report, artifacts=artifacts).to_dict()
    if public["outcome"] == "error" and public["scope"] == WITHHELD_SCOPE:
        return None
    return _RenderedReport(artifacts, _json_text(public))


def _refuse(report: Report, message: str) -> None:
    """Record that nothing is published because the report cannot be represented safely.

    Args:
        report: The report that receives the error outcome and the finding.
        message: The finding's message before the closing words "no output was published.".
    """
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
    """Return the report's artifacts together with the package files in ``output``."""
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
    """Create ``output`` and write one package, its previews and its rendered report.

    Args:
        transaction: Creates ``output`` and removes it if publication fails.
        output: The package directory.
        package: The verified package.
        rendered: The report to write as ``report.json``.
        previews: The preview images to copy.
        check_deadline: Called between the steps to enforce the job's deadline.
    """
    check_deadline()
    directory = transaction.create(output)
    shutil.copytree(package.sources, directory / "sources")
    check_deadline()
    shutil.copyfile(package.archive, directory / "submission.zip")
    shutil.copyfile(package.pdf, directory / "manuscript.pdf")
    _copy_previews(_written_previews(transaction, previews), check_deadline)
    check_deadline()
    _write_new_text(directory / "report.json", rendered.text)


def publish_package(
    transaction: OutputTransaction,
    output: Path,
    package: VerifiedPackage,
    report: Report,
    check_deadline: Callable[[], None],
    *,
    previews: PreviewPlan = (),
) -> bool:
    """Write the package, its previews and the final report into ``output``.

    Args:
        transaction: Creates ``output`` and removes it if publication fails.
        output: The package directory: new, empty, or an earlier output the transaction
            may replace.
        package: The verified package.
        report: The final report; its artifacts receive the published paths.
        check_deadline: Called between the steps to enforce the job's deadline.
        previews: The preview images to copy.

    Returns:
        ``True`` once published, or ``False``, recording why in ``report``, when the final
        report cannot be represented safely; nothing is written then.
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

    Every document must have a verified package: the caller publishes only when none is
    missing, so a missing one is a defect and raises. Every report is rendered before
    anything is created, so a refusal leaves no output.

    Args:
        transaction: Creates the directories and removes them if publication fails.
        output: The directory that holds one package directory per document: new, empty,
            or an earlier output the transaction may replace.
        documents: The selected documents.
        report: The combined report; its artifacts receive the published paths.
        check_deadline: Called between the steps to enforce the job's deadline.
        previews: The preview images to copy.

    Returns:
        ``True`` once published, or ``False``, recording why in ``report``, when the
        combined report or the report of any document cannot be represented safely;
        nothing is written then.

    Raises:
        PreparationError: A document has no verified package.
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
    directory = transaction.create(output)
    for (document, package), rendered_document in zip(verified, rendered_documents, strict=True):
        _write_package(
            transaction, output / document.name, package, rendered_document, (), check_deadline
        )
        document.report.artifacts.update(rendered_document.artifacts)
        check_deadline()
    _copy_previews(_written_previews(transaction, previews), check_deadline)
    _write_new_text(directory / "report.json", rendered.text)
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
    """Write the previews and the final report into the explicit preview directory.

    Args:
        transaction: Creates ``destination`` and removes it if publication fails.
        destination: The preview directory: new, empty, or an earlier output the
            transaction may replace.
        previews: The preview images to copy.
        report: The final report.
        check_deadline: Called before each copy to enforce the job's deadline.

    Returns:
        ``True`` once published, or ``False``, recording why in ``report``, when the final
        report cannot be represented safely; nothing is written then.
    """
    rendered = _render_report(report, report.artifacts)
    if rendered is None:
        _refuse(report, "The final report could not be safely represented;")
        return False
    directory = transaction.create(destination, "Preview output")
    _copy_previews(_written_previews(transaction, previews), check_deadline)
    _write_new_text(directory / "report.json", rendered.text)
    return True


def _check_exportable_preview(work: Path, path: Path, *, exported: int) -> None:
    """Refuse a preview that is not a PNG file of the workspace, or one beyond the limits.

    Args:
        work: The job's workspace.
        path: The preview image.
        exported: How many previews are already planned for export.

    Raises:
        PreparationError: The preview is not a PNG file inside ``work``, is too large, or
            would exceed the number of previews one job exports.
    """
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
    """Give every preview image the findings reference a numbered path in ``destination``.

    Images are numbered once each, in the order of :meth:`Report.sorted_findings`.

    Args:
        report: The report whose findings reference preview images.
        work: The job's workspace, which must contain every image.
        destination: The preview directory.

    Returns:
        Each image with its published path, from ``page-0001.png`` on.

    Raises:
        PreparationError: An image is not an exportable PNG file in ``work`` or exceeds
            a limit.
    """
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
    """Point findings and report artifacts at the exported preview paths.

    A finding keeps only its exported previews and records how many there are.

    Args:
        report: The report to update in place.
        previews: Each exported image with its published path.
    """
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
