"""How hard a check hits when it does not pass: one mapping from outcome to severity.

A check the user configured is a policy: a violation, or a result that cannot be
decided, blocks, so both are errors. A heuristic or default-on check is advisory and
only warns. A note never blocks. A pass is always ``info``.

Every check family states its weight once, with these constants or its own
:class:`Weight`, and asks :meth:`Weight.severity` instead of choosing a severity
beside the code that builds the finding. The evidence kind belongs to the weight
because it follows the same split: policy checks derive their verdict from the
literal source, advisories are heuristics.
"""

from __future__ import annotations

from typing import NamedTuple

from .findings import EvidenceKind, Severity, Status


class Weight(NamedTuple):
    """The severity a check reports for each way it can fail, and how direct its evidence is."""

    failed: Severity
    inconclusive: Severity
    evidence_kind: EvidenceKind

    def severity(self, status: Status) -> Severity:
        """``info`` for a pass, otherwise the severity this weight gives that outcome."""
        if status == "passed":
            return "info"
        return self.inconclusive if status == "inconclusive" else self.failed


# A configured constraint judged from literal source: it blocks, failed or undecidable.
POLICY = Weight("error", "error", EvidenceKind.DERIVED)
# A configured constraint judged from file contents or bytes that were read directly.
DIRECT_POLICY = Weight("error", "error", EvidenceKind.DIRECT)
# A heuristic observation: it warns and never blocks.
ADVISORY = Weight("warning", "warning", EvidenceKind.HEURISTIC)
# Information the user should see although nothing needs to change.
NOTE = Weight("info", "info", EvidenceKind.DIRECT)
