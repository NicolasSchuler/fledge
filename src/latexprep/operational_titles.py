"""The titles reports show for operational results, which have no catalogue entry.

Operational results (``rules.OPERATIONAL_RULE_NAMES``) describe the run or the tool
environment rather than the manuscript, so they have no check code and no
:class:`~latexprep.rule_definition.Rule` that would carry a title. Reports name them by
these titles instead of spelling out the dotted rule name.
"""

from __future__ import annotations

from types import MappingProxyType

# The title of every operational rule name; a test keeps it complete.
OPERATIONAL_TITLES = MappingProxyType(
    {
        # The run itself.
        "execution.cancelled": "Run cancelled",
        "execution.deadline": "Run time limit reached",
        "execution.internal": "Internal error in Fledge",
        "execution.request": "Input or setup problem",
        "execution.resources": "Resource limit reached",
        "execution.rollback": "Partial output left behind",
        "execution.task": "Check did not finish",
        "package.document": "Document could not be prepared",
        "report.redaction_limit": "Report withheld",
        "report.write": "Report not written",
        # Builds.
        "build.completed": "Build completed",
        "build.dependency_trace": "Build dependency trace",
        "build.execution_unavailable": "TeX toolchain unavailable",
        "build.failed": "Build failed",
        "build.log_unavailable": "Build log unavailable",
        "build.missing_pdf": "No PDF produced",
        "build.output_limit": "Build output limit reached",
        "build.package_inventory": "Loaded package inventory",
        "build.resource_limit": "Build resource limit reached",
        "build.submission_dependencies": "Build dependency evidence incomplete",
        "build.timeout": "Build timed out",
        # Tools and services.
        "compare.tools": "Comparison tools",
        "compare.unavailable": "PDF comparison unavailable",
        "online.not_reached": "Online checks not reached",
        "online.requests": "Online requests",
        "source.format_execution": "Formatter run failed",
        "source.formatter_available": "Formatter unavailable",
        # Setup diagnostics.
        "doctor.build_failed": "Setup build failed",
        "doctor.execution_unavailable": "Setup check could not run",
        "doctor.fixture_failed": "Setup fixture failed",
        "doctor.fixture_passed": "Setup fixture passed",
        "doctor.format_evidence": "Formatter check",
        "doctor.pdf_evidence": "PDF tool check",
        "doctor.sandbox": "Restricted execution",
        "doctor.sandbox_unavailable": "Restricted execution unavailable",
        "doctor.tool_missing": "Tool missing",
        "doctor.tool_unavailable": "Tool unusable",
        # Inventories of what was read.
        "manuscript.inventory": "Manuscript inventory",
        "manuscript.package_inventory": "Package inventory",
        # Observations of a PDF, and why a measurement could not run.
        "pdf.annotation_media_scope": "PDF annotation and media scope",
        "pdf.artwork_input_scope": "PDF artwork input scope",
        "pdf.attachments": "PDF attachments",
        "pdf.attachments_unavailable": "PDF attachment inspection unavailable",
        "pdf.encrypted": "PDF encryption",
        "pdf.fonts": "PDF fonts",
        "pdf.fonts_unavailable": "PDF font inspection unavailable",
        "pdf.form": "PDF forms",
        "pdf.geometry_unavailable": "PDF page geometry unavailable",
        "pdf.images": "PDF images",
        "pdf.images_unavailable": "PDF image inspection unavailable",
        "pdf.inspection_unavailable": "PDF inspection unavailable",
        "pdf.javascript": "PDF JavaScript",
        "pdf.metadata": "PDF metadata",
        "pdf.page_geometry": "PDF page geometry",
        "pdf.pages": "PDF pages",
        "pdf.text": "PDF text",
        "pdf.text_unavailable": "PDF text extraction unavailable",
        "pdf.tools": "PDF tools",
    }
)


def build_stage_title(stage: str) -> str:
    """Title the finding of a rebuild stage that did not succeed, ``build.<stage>``.

    Args:
        stage: The stage name after ``build.``, such as ``baseline``.

    Returns:
        The title, such as "Baseline build did not succeed".
    """
    return f"{stage[:1].upper()}{stage[1:]} build did not succeed"
