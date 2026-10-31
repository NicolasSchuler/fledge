"""Where the source graph records what it could not establish, and the wording it shares.

A graph keeps two lists: every finding in the order it was found, and the inconclusive
subset (the uncertainties) that says the lexical subset could not follow something.
``GraphFindings`` writes to both, so the modules that load, check, resolve and walk
sources report through one object and a repeated finding is recorded once.
"""

from __future__ import annotations

from collections.abc import Sequence

from .finding_text import more_suffix
from .findings import Finding

# Most paths named in one message; the rest are counted.
MAX_LISTED_PATHS = 5
# Appended to advice for constructs the scan cannot follow, to say when they matter.
NEEDED_FOR_RESTRUCTURING = (
    'Flat layout (layout = "flat") and source transformations need this; '
    'layout = "preserve" does not.'
)


def listed_names(names: Sequence[str]) -> str:
    """Join ``names`` with commas; many of them are cut short and counted as ``(+N more)``.

    At most ``MAX_LISTED_PATHS`` names are listed.

    Args:
        names: The names to list, in the order to show them.

    Returns:
        The comma-separated names, with the ``(+N more)`` suffix when some are left out.
    """
    return ", ".join(names[:MAX_LISTED_PATHS]) + more_suffix(len(names) - MAX_LISTED_PATHS)


class GraphFindings:
    """The findings of one source graph, written to the lists the graph exposes.

    Attributes:
        findings: Every finding, in the order it was found.
        uncertainties: The inconclusive subset of ``findings``: what the scan could not
            follow.
    """

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
        """Record something the scan could not follow; an identical finding is kept once.

        The finding is inconclusive and goes to both ``uncertainties`` and ``findings``.

        Args:
            rule: The descriptive dotted rule name of the finding.
            message: The message of the finding.
            path: The project file the finding concerns.
            line: The line the finding concerns, if known.
            suggestion: The fix for this occurrence, if any.
            details: Evidence for renderers and JSON; ``None`` records none.
        """
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
        """Record a reference that cannot be satisfied; an identical finding is kept once.

        The finding has error severity and goes to ``findings`` only.

        Args:
            rule: The descriptive dotted rule name of the finding.
            message: The message of the finding.
            path: The project file the finding concerns.
            line: The line of the reference.
            details: Evidence for renderers and JSON; ``None`` records none.
            suggestion: The fix for this occurrence, if any.
        """
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
