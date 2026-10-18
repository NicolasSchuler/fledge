"""Where the source graph records what it could not establish, and the wording it shares.

A graph keeps two lists: every finding in the order it was found, and the inconclusive
subset (the uncertainties) that says the lexical subset could not follow something.
``GraphFindings`` writes to both, so the modules that load, check, resolve and walk
sources report through one object and a repeated finding is recorded once.
"""

from __future__ import annotations

from collections.abc import Sequence

from .findings import Finding

# Most paths named in one message; the rest are counted.
MAX_LISTED_PATHS = 5
# Appended to advice for constructs the scan cannot follow, to say when they matter.
NEEDED_FOR_RESTRUCTURING = (
    'Flat layout (layout = "flat") and source transformations need this; '
    'layout = "preserve" does not.'
)


def listed_names(names: Sequence[str]) -> str:
    """Comma-separated ``names``; many of them are cut short and counted."""
    shown = ", ".join(names[:MAX_LISTED_PATHS])
    hidden = len(names) - MAX_LISTED_PATHS
    return f"{shown} and {hidden} more" if hidden > 0 else shown


class GraphFindings:
    """The findings of one source graph, written to the lists the graph exposes."""

    def __init__(self, findings: list[Finding], uncertainties: list[Finding]) -> None:
        self.findings = findings
        self.uncertainties = uncertainties

    def uncertain(
        self,
        rule: str,
        message: str,
        path: str,
        line: int | None = None,
        suggestion: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        """Record something the scan could not follow; an identical finding is kept once."""
        finding = Finding(
            rule,
            message,
            status="inconclusive",
            path=path,
            line=line,
            suggestion=suggestion,
            details=details or {},
        )
        if finding not in self.uncertainties:
            self.uncertainties.append(finding)
            self.findings.append(finding)

    def error(
        self,
        rule: str,
        message: str,
        path: str,
        line: int,
        details: dict[str, object] | None = None,
        *,
        suggestion: str | None = None,
    ) -> None:
        """Record a reference that cannot be satisfied; an identical finding is kept once."""
        finding = Finding(
            rule,
            message,
            severity="error",
            path=path,
            line=line,
            suggestion=suggestion,
            details=details or {},
        )
        if finding not in self.findings:
            self.findings.append(finding)

    def add(self, finding: Finding) -> None:
        """Record a finding of another kind, such as a lookup-order note."""
        self.findings.append(finding)
