"""What one isolated build returns: its findings, evidence and the report entries.

``BuildResult`` is the value the build orchestration hands to the checks that judge
it; ``BuildTools`` gathers what the build recorded about its toolchain, and each part
of it has a ``to_report`` for the report boundary. Producing the result is
``latexmk_build``'s work, and checks that read it import this module, not the
orchestrator.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path

from .bibliography_controls import BibliographyEvidence
from .build_dependencies import SubmissionDependencies
from .command_runner import RuntimeLimits, ToolVersion
from .findings import Finding
from .loaded_options import LoadedOptionsEvidence
from .package_headers import LoadedPackage
from .setting_choices import BibliographyBackend


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """The isolation and limits a build ran under, and how long its command took.

    Attributes:
        backend: The isolation backend that ran the build.
        limits: The runtime limits of the build.
        source_date_epoch: The ``SOURCE_DATE_EPOCH`` the build ran with.
        duration_seconds: How long the build command took, in seconds, if known.
    """

    backend: str
    limits: RuntimeLimits
    source_date_epoch: str
    duration_seconds: float | None = None

    def to_report(self) -> dict[str, object]:
        """Describe the execution policy for the report.

        Network access, shell escape and imported configuration are always reported as
        denied or disabled; the duration, rounded to milliseconds, only when it is known.

        Returns:
            The report entries.
        """
        report: dict[str, object] = {
            "backend": self.backend,
            "limits": self.limits.to_report(),
            "network": "denied",
            "shell_escape": "disabled",
            "imported_configuration": "disabled",
            "source_date_epoch": self.source_date_epoch,
        }
        if self.duration_seconds is not None:
            report["duration_seconds"] = round(self.duration_seconds, 3)
        return report


@dataclass(frozen=True, slots=True)
class BibliographyRecord:
    """The requested backend, the fresh control-file evidence and how Biber was prepared.

    Attributes:
        requested_backend: The bibliography backend that was requested.
        evidence: The evidence from the fresh bibliography control file.
        biber_preparation: How Biber was prepared, if it was.
    """

    requested_backend: BibliographyBackend
    evidence: BibliographyEvidence
    biber_preparation: str | None = None

    def to_report(self) -> dict[str, object]:
        """Describe the bibliography record for the report.

        Returns:
            The requested backend, the details of the evidence and, when known, how
            Biber was prepared.
        """
        report: dict[str, object] = {
            "requested_backend": self.requested_backend,
            **self.evidence.to_details(),
        }
        if self.biber_preparation is not None:
            report["biber_preparation"] = self.biber_preparation
        return report


@dataclass(slots=True)
class BuildTools:
    """What one build records about its toolchain; ``to_report`` gives the report entries.

    Attributes:
        execution: The isolation, limits and duration of the build command.
        versions: The version of each tool, by tool name.
        resource_roots: The directories the toolchain may read from, sorted.
        submission_dependencies: The dependency evidence of the submission.
        bibliography: The bibliography backend and its evidence.
        loaded_options: The evidence about the options of the loaded packages.
    """

    execution: ExecutionPolicy | None = None
    versions: dict[str, ToolVersion] = field(default_factory=dict)
    resource_roots: tuple[str, ...] | None = None
    submission_dependencies: SubmissionDependencies | None = None
    bibliography: BibliographyRecord | None = None
    loaded_options: LoadedOptionsEvidence | None = None

    def to_report(self) -> dict[str, object]:
        """Describe the toolchain record for the report.

        Returns:
            The report entries: each tool version under its tool's name, and every other
            part under its own name. A part that was not recorded is left out.
        """
        report: dict[str, object] = {}
        if self.execution is not None:
            report["execution"] = self.execution.to_report()
        report.update({name: version.to_report() for name, version in self.versions.items()})
        if self.resource_roots is not None:
            report["resource_roots"] = list(self.resource_roots)
        if self.submission_dependencies is not None:
            report["submission_dependencies"] = self.submission_dependencies.to_details()
        if self.bibliography is not None:
            report["bibliography"] = self.bibliography.to_report()
        if self.loaded_options is not None:
            report["loaded_options"] = self.loaded_options.details()
        return report


@dataclass(slots=True)
class BuildResult:
    """The outcome of one build: whether it succeeded, why not, and the evidence it left.

    Attributes:
        success: Whether the build succeeded.
        pdf: The PDF the build produced, if any.
        findings: The findings of the build.
        recorder_project_inputs: The project files the TeX recorder shows the build read.
        tools: What the build recorded about its toolchain.
        command: The command that ran the build.
        log: The text of the build log.
        loaded_packages: The packages the build loaded.
        recorder_complete: Whether the build's checks accepted the recorder trace as
            complete.
        submission_inputs: The inputs of the submission, or ``None`` when complete
            dependency evidence is unavailable.
        generated_reads: The generated files the build read, relative to the output or
            the project folder, such as ``main.bbl``.
    """

    success: bool
    _: KW_ONLY
    pdf: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    recorder_project_inputs: set[str] = field(default_factory=set)
    tools: BuildTools = field(default_factory=BuildTools)
    command: list[str] = field(default_factory=list)
    log: str = ""
    loaded_packages: list[LoadedPackage] = field(default_factory=list)
    recorder_complete: bool = False
    submission_inputs: set[str] | None = None
    generated_reads: frozenset[str] = frozenset()
