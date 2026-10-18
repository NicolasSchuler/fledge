"""The last steps of preparation: archive the sources, rebuild from the ZIP, run final checks.

``PackageVerification`` takes the shipped sources, writes the exact archive that will be
published, extracts it afresh, rebuilds the PDF from that extraction and runs the final
source and PDF checks on it. It returns a ``VerifiedPackage`` only when nothing blocks.
It does not select inputs, flatten sources or decide what is published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .document_builds import ARCHIVE, build_stage
from .document_tasks import (
    FinalExtraction,
    TaskName,
    final_verification_tasks,
    prepared_submission_options,
)
from .findings import Finding
from .import_budget import ImportLimits
from .job_context import JobContext
from .job_report import is_blocked, record_findings
from .metadata_source_edits import check_sanitized_pdf_metadata
from .project import ImportedProject, import_project
from .project_archive import create_archive
from .publication import VerifiedPackage
from .scheduler import Scheduler, TaskResult
from .submission_checks import SubmissionOptions, check_submission, check_submission_archive
from .task_reporting import PackageLayout, TaskReporter

_PREPARED_SUBMISSION_STAGE = "prepared submission checks"
_ARCHIVE_POLICY_STAGE = "archive"
_METADATA_STAGE = "final metadata sanitization"


@dataclass(frozen=True, slots=True)
class ShippedSources:
    """Selected and possibly flattened sources, rebuilt and matched against the baseline."""

    root: Path
    main: str
    # The PDF of the build that was matched against the baseline.
    pdf: Path
    # Source-to-package names of a flattening; empty for the preserved layout.
    path_mapping: Mapping[str, str]

    @property
    def layout(self) -> PackageLayout:
        return PackageLayout(self.root, self.path_mapping)


@dataclass(frozen=True, slots=True)
class PackageVerification:
    """Collaborators of the final steps; ``results`` are the scheduler's finished tasks."""

    context: JobContext
    reporter: TaskReporter
    scheduler: Scheduler
    results: Mapping[str, TaskResult]
    reference: Path | None

    def _blocked(self) -> bool:
        return is_blocked(self.reporter.report, self.context.settings)

    async def package_and_verify(self, shipped: ShippedSources) -> VerifiedPackage | None:
        """Write the archive, rebuild a fresh extraction and run the final checks."""
        submission = prepared_submission_options(self.context.settings, shipped.path_mapping)
        if not await self._check_prepared_submission(shipped, submission):
            return None
        archive = await self._create_archive_and_gate(shipped)
        if archive is None:
            return None
        extracted = await self._extract_archive(archive)
        return await self._rebuild_and_verify_extraction(shipped, archive, extracted, submission)

    async def _check_prepared_submission(
        self, shipped: ShippedSources, submission: SubmissionOptions
    ) -> bool:
        """Run the submission checks on the shipped sources; False when they block."""
        record_findings(
            self.reporter.report,
            await self.context.thread_operation(
                "prepared submission checks",
                check_submission,
                shipped.root,
                shipped.main,
                submission,
            ),
            _PREPARED_SUBMISSION_STAGE,
        )
        return not self._blocked()

    async def _create_archive_and_gate(self, shipped: ShippedSources) -> Path | None:
        """Write the ZIP and check its policy and size; None when either blocks."""
        context, settings, report = self.context, self.context.settings, self.reporter.report
        context.emit("Packaging sources and rebuilding a fresh extraction of the exact ZIP")
        archive = context.work / "submission.zip"
        await context.thread_operation("create archive", create_archive, shipped.root, archive)
        self.reporter.record_inventory("prepared", shipped.layout, archive)
        record_findings(
            report,
            await context.thread_operation(
                "compressed archive policy",
                check_submission_archive,
                archive,
                settings.submission_checks,
            ),
            _ARCHIVE_POLICY_STAGE,
        )
        if self._blocked():
            return None
        archive_bytes = archive.stat().st_size
        if archive_bytes > settings.import_byte_limit:
            report.findings.append(
                Finding(
                    "package.archive_limit",
                    f"Prepared archive has {archive_bytes} bytes; the import size limit "
                    f"(max_import_bytes, or max_bytes when that is not set) is "
                    f"{settings.import_byte_limit}. It cannot be re-extracted for verification.",
                    severity="error",
                    status="failed",
                    suggestion="Reduce the shipped files or raise max_import_bytes explicitly.",
                )
            )
            return None
        return archive

    async def _extract_archive(self, archive: Path) -> ImportedProject:
        """Extract the archive into a fresh directory, as a recipient would."""
        context = self.context
        # The archive is our own bounded output; preserve byte/count limits without
        # rejecting legitimate highly compressible sources on ratio alone.
        limits = ImportLimits.from_settings(
            context.settings, max_ratio=context.settings.import_byte_limit
        )
        return await context.thread_operation(
            "extract archive",
            import_project,
            archive,
            context.work / "extracted",
            limits,
            unwrap=False,
            exclude_metadata=False,
        )

    async def _rebuild_and_verify_extraction(
        self,
        shipped: ShippedSources,
        archive: Path,
        extracted: ImportedProject,
        submission: SubmissionOptions,
    ) -> VerifiedPackage | None:
        """Rebuild from the extraction and run the final checks; None when anything blocks."""
        context, settings, report = self.context, self.context.settings, self.reporter.report
        final = await build_stage(context, self.reporter, extracted.root, shipped.main, ARCHIVE)
        final_pdf = final.pdf
        if not final.success or final_pdf is None:
            return None
        extraction = FinalExtraction(
            extracted.root, shipped.main, shipped.pdf, final_pdf, submission, self.reference
        )
        await self.scheduler.execute(final_verification_tasks(context, extraction))
        # The extraction's source analysis is not needed again; free its graph.
        context.sources.clear()
        inspection = self.results.get(TaskName.FINAL_PDF_INSPECTION)
        record_findings(
            report,
            check_sanitized_pdf_metadata(
                inspection.value if inspection and inspection.succeeded else [],
                settings.metadata_privacy,
            ),
            _METADATA_STAGE,
        )
        report.scope = "selected preparation checks, visual/text preservation and fresh ZIP rebuild"
        blocked = self._blocked()
        self.reporter.record_inventory(
            "prepared" if blocked else "verified", shipped.layout, archive
        )
        return None if blocked else VerifiedPackage(shipped.root, archive, final_pdf)
