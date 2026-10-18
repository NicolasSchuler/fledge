"""How the tasks, builds and input selections of one document enter its report.

``TaskReporter`` turns each finished scheduler task, each rebuild and each input
selection into findings, stage records, tool versions and package inventory rows of
one ``Report``. It does not decide what runs, whether the job continues, or what the
outcome is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .build_checks import (
    check_build_details,
    check_tex_distribution,
    qualify_citation_findings,
)
from .build_result import BuildResult
from .config import Settings
from .document_tasks import BASELINE_STAGE, TaskName
from .findings import Finding, has_blockers
from .job_report import record_findings
from .package_inputs import InputSelectionResult
from .package_inventory import (
    InventoryRow,
    PackageObservation,
    PackageStatus,
    assemble_package_inventory,
)
from .report import Change, Report
from .scheduler import TaskResult, TaskStatus
from .source_graph import ROOT_AMBIGUOUS_RULE, ROOT_MISSING_RULE
from .task_findings import blocked_task_finding, failed_task_finding
from .toolchain_help import explain_missing_toolchain

# Tasks whose findings enter the report without a stage tag, as do those of the source
# analysis. An untagged copy is the initial analysis: ``finding_view.merge_stage_copies``
# lets it stand for the identical copies that the final source checks repeat, and omits
# them. Every other task tags its findings with its own name.
_UNTAGGED_FINDING_TASKS = frozenset({TaskName.BIBLIOGRAPHY_CHECKS, TaskName.MANUSCRIPT_CHECKS})
# Root-selection findings of the source analysis, which repeat what main selection
# reports when it found no single main file, with its candidates.
_ROOT_SELECTION_RULES = frozenset({ROOT_AMBIGUOUS_RULE, ROOT_MISSING_RULE})


class InputSelection(NamedTuple):
    """The inputs copied for preparation, with the selection's changes and evidence."""

    root: Path
    changes: list[Change]
    findings: list[Finding]
    inventory: list[InventoryRow]


@dataclass(frozen=True, slots=True)
class PackageLayout:
    """The files that make up a package: their root, and how flattening renamed them."""

    root: Path
    # Source-to-package names of a flattening; empty for the preserved layout.
    path_mapping: Mapping[str, str]


class TaskReporter:
    """Writes the results of one document's run into its report."""

    def __init__(
        self,
        report: Report,
        settings: Settings,
        *,
        source_root: Path,
        main: str | None,
        document_name: str | None,
    ) -> None:
        self.report = report
        self._settings = settings
        self._source_root = source_root
        self._main = main
        # The inventory names the document by its configured name, else by its main file.
        self._inventory_document = document_name or main
        self._selections: list[list[InventoryRow]] = []

    def collect(self, result: TaskResult) -> None:
        """Report a finished task: its stage record, and its findings or its failure."""
        if result.name == TaskName.BASELINE_BUILD and result.value is not None:
            self.record_build(
                result.value,
                BASELINE_STAGE,
                elapsed_seconds=result.elapsed_seconds,
                queue_seconds=result.queue_seconds,
            )
            return
        self._record_stage(result)
        if result.name == TaskName.SELECT_PREPARATION_INPUTS and result.value is not None:
            self._collect_input_selection(result.value)
        elif result.status is TaskStatus.BLOCKED:
            # The failed prerequisite already reports its own blocking finding.
            self.report.findings.append(blocked_task_finding(result))
        elif not result.succeeded:
            self.report.findings.append(failed_task_finding(result))
        else:
            self._collect_findings(result)

    def _record_stage(self, result: TaskResult) -> None:
        # The baseline build has one stage name whether it finished or crashed.
        name = BASELINE_STAGE if result.name == TaskName.BASELINE_BUILD else result.name
        self.report.stages.append(
            {
                "name": name,
                "status": result.status.value,
                "elapsed_seconds": result.elapsed_seconds,
                "queue_seconds": result.queue_seconds,
            }
        )

    def _collect_findings(self, result: TaskResult) -> None:
        if result.name == TaskName.SOURCE_CHECKS:
            findings = result.value.findings
            if self._main is None:
                # Main selection already reported the same ambiguity with its candidates.
                findings = [item for item in findings if item.rule not in _ROOT_SELECTION_RULES]
            self.report.findings.extend(findings)
        elif result.name in _UNTAGGED_FINDING_TASKS:
            self.report.findings.extend(result.value)
        else:
            record_findings(self.report, result.value, result.name)

    def _collect_input_selection(self, selection: InputSelection) -> None:
        self.report.changes.extend(selection.changes)
        self._selections.append(selection.inventory)
        # Record the inventory from this selection's evidence, without duplicating findings.
        self.record_inventory("incomplete" if has_blockers(selection.findings) else "proposed")
        record_findings(self.report, selection.findings, TaskName.SELECT_PREPARATION_INPUTS.value)

    def record_selection(self, selected: InputSelectionResult, stage: str) -> None:
        """Record a selection's changes and findings under ``stage``; keep its inventory."""
        self.report.changes.extend(selected.changes)
        self._selections.append(selected.inventory)
        record_findings(self.report, selected.findings, stage)

    def record_inventory(
        self,
        status: PackageStatus,
        layout: PackageLayout | None = None,
        archive: Path | None = None,
    ) -> None:
        """Describe the package contents so far; ``layout`` and ``archive`` once they exist."""
        self.report.package_contents = [
            assemble_package_inventory(
                selections=self._selections,
                changes=self.report.changes,
                source_root=self._source_root,
                observation=PackageObservation(
                    layout.root if layout else None,
                    archive,
                    layout.path_mapping if layout else None,
                ),
                document=self._inventory_document,
                status=status,
            ).to_report()
        ]

    def record_build(
        self, result: BuildResult, stage: str, *, elapsed_seconds: float, queue_seconds: float
    ) -> bool:
        """Record a build's findings, tools, timing and stage; return whether it built a PDF."""
        report, settings = self.report, self._settings
        findings = explain_missing_toolchain(
            qualify_citation_findings(result.findings, build_succeeded=result.success),
            settings.engine,
        )
        toolchain_missing = any("missing_tools" in finding.details for finding in findings)
        record_findings(report, findings, stage)
        record_findings(report, check_build_details(result, settings.build_checks), stage)
        if stage == BASELINE_STAGE and not toolchain_missing:
            # One observation per document: later builds use the same toolchain. Without
            # TeX the missing-toolchain finding already explains the absent version.
            record_findings(report, [check_tex_distribution(result, settings.build_checks)], stage)
        report.tools.update(result.tools.to_report())
        built = result.success and result.pdf is not None
        report.stages.append(
            {
                "name": stage,
                "status": "succeeded" if built else "failed",
                "command": result.command,
                "elapsed_seconds": round(elapsed_seconds, 6),
                "queue_seconds": round(queue_seconds, 6),
            }
        )
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
        return built
