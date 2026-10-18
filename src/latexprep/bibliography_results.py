"""How the offline bibliography checks word and order their findings.

This module owns the finding builders and the summary logic that the integrity checks,
the coverage and field checks and the duplicate search share: a finding located at an
entry or an offset, a per-check summary that never reports "passed" for an empty
inspection, and the collapse of summaries that only restate a source error already
reported elsewhere. It decides nothing about what is a problem; the check modules do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bibliography_index import LocatedEntry
from .bibtex_syntax import BibDocument
from .citation_graph import Uncertainty, bounded_uncertainties, uncertainty_messages
from .findings import EvidenceKind, Finding, Severity, Status

# The uncertainty that follows from other causes; a note names a cause instead.
SELECTION_UNAVAILABLE = "No complete bibliography resource selection is available"


def located_finding(
    document: BibDocument,
    rule: str,
    message: str,
    offset: int,
    *,
    severity: Severity = "warning",
    status: Status = "failed",
    suggestion: str | None = None,
    details: dict[str, object] | None = None,
) -> Finding:
    """A finding at the line of ``offset`` in ``document``."""
    return Finding(
        rule,
        message,
        severity=severity,
        status=status,
        path=document.path,
        line=document.line(offset),
        suggestion=suggestion,
        details=details or {},
    )


def syntax_finding(document: BibDocument) -> Finding | None:
    """The error finding for the syntax problem that stopped ``document``, if any."""
    problem = document.syntax_error
    if problem is None:
        return None
    return located_finding(
        document,
        "bibliography.syntax",
        problem.message,
        problem.offset,
        severity="error",
        suggestion="Repair this entry; the remaining file was not parsed or normalized.",
    )


def item_finding(
    rule: str,
    message: str,
    status: Status,
    *,
    reference: LocatedEntry | None = None,
    severity: Severity = "warning",
    evidence: EvidenceKind = EvidenceKind.DERIVED,
    incomplete: bool = False,
    details: dict[str, object] | None = None,
) -> Finding:
    """A ``bibliography.<rule>`` finding about ``reference``; a passed one is only info."""
    data = dict(details or {})
    if reference:
        data.update({"entry": reference.entry.key, "entry_type": reference.entry.entry_type})
    return Finding(
        f"bibliography.{rule}",
        message,
        severity="info" if status == "passed" else severity,
        status=status,
        path=reference.document.path if reference else None,
        line=reference.document.line(reference.entry.start) if reference else None,
        evidence_kind=evidence,
        incomplete=incomplete,
        details=data,
    )


@dataclass(frozen=True, slots=True)
class ReportScope:
    """What the coverage and field findings describe; repeated in each summary's details."""

    description: str
    documents: tuple[str, ...]
    resources: tuple[str, ...]

    def details(self, **extra: object) -> dict[str, object]:
        """The scope details; ``extra`` adds keys or replaces values in place."""
        return {
            "scope": self.description,
            "documents": list(self.documents),
            "resources": list(self.resources),
            "source_expansion": "not performed",
            "network": "not used",
            **extra,
        }


@dataclass(frozen=True, slots=True)
class SummarySpec:
    """The rule and wording of one check's summary; ``details`` replaces the scope's."""

    rule: str
    description: str
    severity: Severity = "warning"
    details: dict[str, object] | None = None


@dataclass(slots=True)
class ResultCollector:
    """Findings in report order; summaries that only restate reported source errors wait.

    A missing input or an ambiguous main file makes every bibliography summary
    inconclusive with the same reason. The source checks report that error;
    repeating it once per bibliography check buries it, so such summaries become
    one note. Summaries with any other uncertainty, and every per-item finding, stay.
    """

    scope: ReportScope
    findings: list[Finding] = field(default_factory=list)
    # Held-back summaries with the uncertainty messages each lists.
    restated: list[tuple[Finding, list[str]]] = field(default_factory=list)

    def summarize(
        self,
        spec: SummarySpec,
        items: list[Finding],
        checked: int,
        uncertainties: list[Uncertainty],
    ) -> None:
        """Add a check's item findings and its summary for ``checked`` supported items."""
        base = self.scope.details() if spec.details is None else spec.details
        self.findings.extend(items)
        if uncertainties:
            bounded = bounded_uncertainties(uncertainties)
            messages = uncertainty_messages(bounded)
            summary = item_finding(
                spec.rule,
                f"{spec.description} is incomplete: {uncertainties[0].message}.",
                "inconclusive",
                severity=spec.severity,
                incomplete=True,
                details={**base, "checked": checked, "uncertainty": messages},
            )
            if all(item.already_reported for item in bounded):
                self.restated.append((summary, messages))
            else:
                self.findings.append(summary)
        elif items:
            return
        elif not checked:
            # Reporting "passed" for an empty inspection would overstate the evidence.
            self.findings.append(
                item_finding(
                    spec.rule,
                    "No supported items to check.",
                    "not_applicable",
                    severity="info",
                    details={**base, "checked": 0},
                )
            )
        else:
            self.findings.append(
                item_finding(
                    spec.rule,
                    f"{spec.description}: no issue found in {checked} supported item(s).",
                    "passed",
                    details={**base, "checked": checked},
                )
            )

    def report(self) -> list[Finding]:
        """The findings, with the restated summaries replaced by one note."""
        if not self.restated:
            return self.findings
        codes = list(dict.fromkeys(summary.code or summary.rule for summary, _ in self.restated))
        reported = sorted({message for _, messages in self.restated for message in messages})
        # An unavailable selection only follows from the causes; name a cause instead.
        reported = [reason for reason in reported if reason != SELECTION_UNAVAILABLE] or reported
        note = Finding(
            "bibliography.source_error",
            f"Bibliography checks {', '.join(codes)} were inconclusive because of a source "
            f"error reported above: {reported[0].rstrip('.')}.",
            severity="warning",
            status="inconclusive",
            evidence_kind=EvidenceKind.DERIVED,
            suggestion="Fix the reported source error; these bibliography checks then run in full.",
            details=self.scope.details(inconclusive_checks=codes, uncertainty=reported),
        )
        return [*self.findings, note]
