"""Publish verified packages, previews and reports into directories outside the input.

Publication is one transaction. A new directory is created exclusively, an existing empty
one is filled, and a directory that Fledge published earlier is replaced only when the
caller allows it: the new output is written into a sibling directory and swapped into
place once everything is written. When any step fails, exactly what the transaction
created is removed again and a replaced directory stays as it was. Every published
directory holds ``OUTPUT_MARKER``, which is how a later run recognises it.

A report that cannot be represented safely is refused up front, before any directory
exists: the refusal is recorded in the report as a ``report.redaction_limit`` error, never
raised. A step that raises leaves ``report.artifacts`` to the caller, which clears it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import NamedTuple, Self

from . import __version__
from .errors import PreparationError, RequestError
from .file_safety import read_file_prefix
from .findings import Finding
from .path_safety import is_contained
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
# The file in every directory publication writes; a later run may replace only such a
# directory, and only when asked to.
OUTPUT_MARKER = ".fledge-output"
_MARKER_TEXT = (
    "Fledge {version} wrote this folder. A later run with --force may replace it once the "
    "new run has succeeded; Fledge refuses to replace a folder without this file.\n"
)
# How much of an existing report file is read to recognise it as a Fledge report, and the
# beginnings that recognise one: the JSON report and the HTML page, as Fledge writes them.
_REPORT_PREFIX_BYTES = 4096
_REPORT_BEGINNINGS = (
    re.compile(rb'\A\s*\{\s*"schema_version"\s*:\s*\d+\s*,\s*"command"\s*:\s*"'),
    re.compile(rb"\A<!doctype html><html[^>]*><head>.*?<title>Fledge [^<]*report", re.DOTALL),
)
# The names of the sibling directories a replacement writes into and moves the old output to.
_STAGING_PREFIX = ".fledge-staging-"
_REPLACED_PREFIX = ".fledge-replaced-"


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


def is_fledge_output(path: Path) -> bool:
    """Return whether ``path`` is a real directory that publication wrote: it holds the marker."""
    marker = path / OUTPUT_MARKER
    return path.is_dir() and not path.is_symlink() and marker.is_file() and not marker.is_symlink()


def is_fledge_report(path: Path) -> bool:
    """Return whether ``path`` is a regular file that begins like a Fledge JSON or HTML report."""
    try:
        beginning = read_file_prefix(path, _REPORT_PREFIX_BYTES)
    except OSError:
        return False
    return any(pattern.match(beginning) for pattern in _REPORT_BEGINNINGS)


def _is_empty_directory(path: Path) -> bool:
    """Return whether ``path`` is a real, readable directory with nothing in it."""
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def _existing_destination_error(destination: Path, option: str, *, folder: bool) -> RequestError:
    """Build the refusal of an existing ``destination`` that may not be written to.

    Args:
        destination: The existing output path.
        option: The option that named it.
        folder: Whether ``destination`` names a directory rather than a file.

    Returns:
        The error; it suggests ``--force`` for a directory that Fledge published earlier.
    """
    if folder and is_fledge_output(destination):
        return RequestError(
            f"Output already exists: {destination}",
            f"Choose a new {option} path, remove {destination}, or rerun with --force to "
            "replace this earlier Fledge output once the new run has succeeded.",
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

    The path must be new, except that a ``folder`` may be an existing empty directory. With
    ``force``, an earlier Fledge output may be named too: for a ``folder``, a directory
    that publication wrote (one holding ``OUTPUT_MARKER``); for a file, one that begins
    like a Fledge report. Such an output is replaced only once the new run has succeeded.

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
            acceptable = _is_empty_directory(destination) or (
                force and is_fledge_output(destination)
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


class _Replacement(NamedTuple):
    """An earlier output being replaced: where the new one is written, and where it goes.

    Attributes:
        staging: The new sibling directory that receives the new output.
        destination: The earlier output that the new one replaces once it is complete.
    """

    staging: Path
    destination: Path


class OutputTransaction:
    """Directories one publication writes; a failure removes everything it wrote.

    ``create`` makes a new directory, fills an existing empty one, or, with
    ``replace_previous``, writes the replacement of an earlier Fledge output into a sibling
    directory. Leaving the block without an error swaps each replacement into place; a
    failure removes the new directories, the contents written into empty ones and every
    sibling, never previous or concurrent data.

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
        self._filled: list[Path] = []
        self._replacements: list[_Replacement] = []
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
            self._swap_replacements()
            return
        self._roll_back(exc_value)

    def _roll_back(self, cause: BaseException | None) -> None:
        """Remove what the transaction wrote; announce it and name what stays behind.

        Args:
            cause: The error that caused the rollback, which receives a note for each
                directory that stays behind.
        """
        if self._created or self._filled or self._replacements:
            self._progress("Publication failed; removing the partial output")
        # Try every directory: one that cannot be removed must neither hide the error
        # that caused the rollback nor leave the remaining directories behind.
        targets = [
            *reversed(self._created),
            *(replacement.staging for replacement in self._replacements),
            *(child for path in self._filled for child in _children(path)),
        ]
        for path in targets:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                self._report_leftover(path, error, cause)

    def _swap_replacements(self) -> None:
        """Move every replacement into place; on a failure, restore the earlier outputs.

        Raises:
            PreparationError: A replacement could not be moved into place; every earlier
                output is back where it was, and nothing new stays published.
        """
        swapped: list[tuple[_Replacement, Path]] = []
        for replacement in self._replacements:
            try:
                swapped.append((replacement, _swap(replacement)))
            except OSError as error:
                for done, earlier in reversed(swapped):
                    if not _unswap(done, earlier):
                        self._progress(
                            f"Could not restore {done.destination}; the earlier output is in "
                            f"{earlier}."
                        )
                self._roll_back(error)
                if self._report is not None:
                    self._report.artifacts.clear()
                    locate_previews(self._report, ())
                raise PreparationError(
                    f"Could not replace the earlier output {replacement.destination} "
                    f"({error.strerror or type(error).__name__}); it was kept and nothing "
                    "new was published"
                ) from error
        for _, previous in swapped:
            try:
                shutil.rmtree(previous)
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
        """Make ``path`` ready for writing and remember it for the rollback.

        Inside a directory the transaction already writes, a subdirectory is made there.
        Otherwise a new directory is created, an existing empty one is filled, or the
        replacement of an earlier Fledge output is begun in a sibling directory; each of
        those top-level directories receives ``OUTPUT_MARKER``.

        Args:
            path: The directory to write.
            label: Names the output in the error message.

        Returns:
            Where to write what belongs in ``path``: ``path`` itself, or the sibling
            directory of a replacement.

        Raises:
            PreparationError: Something else appeared at ``path`` while the job ran.
        """
        written = self.physical(path)
        if self._inside_output(path):
            written.mkdir()
            return written
        if path.exists() or path.is_symlink():
            written = self._reuse(path, label)
        else:
            # mkdir is exclusive, so a concurrent creation is never written into.
            try:
                path.mkdir()
            except FileExistsError as error:
                raise PreparationError(f"{label} appeared while the job ran: {path}") from error
            self._created.append(path)
        _write_new_text(written / OUTPUT_MARKER, _MARKER_TEXT.format(version=__version__))
        return written

    def _reuse(self, path: Path, label: str) -> Path:
        """Begin writing into the existing directory ``path``: fill it, or stage its replacement.

        Args:
            path: The existing directory.
            label: Names the output in the error message.

        Returns:
            ``path`` when it is empty, or a new sibling directory for its replacement.

        Raises:
            PreparationError: ``path`` is neither empty nor a replaceable earlier output.
        """
        if _is_empty_directory(path):
            self._filled.append(path)
            return path
        if not (self._replace_previous and is_fledge_output(path)):
            raise PreparationError(f"{label} changed while the job ran: {path}")
        staging = _new_sibling(path, _STAGING_PREFIX)
        self._replacements.append(_Replacement(staging, path))
        return staging

    def _inside_output(self, path: Path) -> bool:
        """Return whether ``path`` lies below a top-level directory this transaction writes."""
        roots = [
            *self._created,
            *self._filled,
            *(replacement.destination for replacement in self._replacements),
        ]
        return any(path != root and path.is_relative_to(root) for root in roots)

    def physical(self, path: Path) -> Path:
        """Return where to write ``path``: inside the sibling of a replacement, or ``path``.

        Args:
            path: A path at or below an output of this transaction.

        Returns:
            The corresponding path inside the replacement's sibling directory when
            ``path`` lies in an output being replaced, otherwise ``path`` unchanged.
        """
        for staging, destination in self._replacements:
            if path.is_relative_to(destination):
                return staging / path.relative_to(destination)
        return path


def _children(path: Path) -> list[Path]:
    """List what the directory ``path`` holds; an unreadable directory holds nothing."""
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _new_sibling(path: Path, prefix: str) -> Path:
    """Create a new hidden directory beside ``path`` whose name starts with ``prefix``.

    Returns:
        The new, empty directory.
    """
    while True:
        sibling = path.parent / f"{prefix}{secrets.token_hex(4)}"
        try:
            sibling.mkdir()
        except FileExistsError:
            continue
        return sibling


def _swap(replacement: _Replacement) -> Path:
    """Move an earlier output aside and its complete replacement into its place.

    Args:
        replacement: The replacement and the earlier output.

    Returns:
        Where the earlier output now is, a hidden sibling directory.

    Raises:
        OSError: A move failed; the earlier output is back in place when it can be.
    """
    previous = _new_sibling(replacement.destination, _REPLACED_PREFIX)
    # Renaming a directory onto an empty one replaces it in one step.
    replacement.destination.replace(previous)
    try:
        replacement.staging.rename(replacement.destination)
    except OSError as error:
        try:
            previous.rename(replacement.destination)
        except OSError:
            error.add_note(f"The earlier output is now in {previous}.")
        raise
    return previous


def _unswap(replacement: _Replacement, previous: Path) -> bool:
    """Undo a completed swap: the replacement goes back beside, the earlier output returns.

    The rollback that follows removes the replacement's sibling, never the earlier output.

    Args:
        replacement: The replacement that was swapped into place.
        previous: Where the earlier output was moved.

    Returns:
        Whether the earlier output is back in place; when a move fails, the directories
        stay where they are.
    """
    try:
        replacement.destination.rename(replacement.staging)
        previous.rename(replacement.destination)
    except OSError:
        return False
    return True


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
