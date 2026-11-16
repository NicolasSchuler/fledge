r"""Hard-coded figure, table, section and equation numbers in prose (``MAN007``).

An advisory scan for text such as "Figure 2", "Table 3", "Section 4" or "Equation (5)",
which stops matching when the numbering changes. Command invocations and their
arguments are blanked first, so a ``\ref{...}`` or a literal example never counts. Text
in either branch of a conditional is typed text too, so it is matched. Whether the text
refers to a manuscript object is left to the author; nothing is inferred. Gaps in the
source graph and uses of the project's own macros that the reading could not interpret
leave the scan incomplete; a command from a class or package does not type the author's
prose.
"""

from __future__ import annotations

import re

from .finding_text import plural
from .finding_weight import ADVISORY
from .findings import Finding
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, policy_finding
from .tex_spans import command_end

_HARDCODED = Policy(
    "manuscript.hardcoded_references", "Checked hard-coded reference candidates.", ADVISORY
)
_HARDCODED_REFERENCE = re.compile(
    r"""
    \b(?:Figures?|Figs?\.?|Tables?|Sections?|Secs?\.?|Equations?|Eqs?\.?)  # object kind
    \s*[~ ]\s*                                                          # space or tie
    \(?\d+(?:\.\d+)*\)?                                                 # number, as in (5)
    """,
    re.VERBOSE,
)


def reference_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Find the hard-coded reference candidates when that advisory is switched on.

    Args:
        reading: The expanded document and its document-wide interpretation gaps.
        options: The manuscript check options; ``check_hardcoded_references`` switches
            the advisory on.

    Returns:
        One ``manuscript.hardcoded_references`` finding, or an empty list when the
        advisory is off.
    """
    if not options.check_hardcoded_references:
        return []
    document = reading.document
    # Blank command invocations and their arguments, preserving source offsets.
    buffer = list(document.text)
    for command in document.commands:
        start, stop = command.start, command_end(command)
        buffer[start:stop] = [" "] * (stop - start)
    matches = [
        document.located(match.start(), text=match.group())
        for match in _HARDCODED_REFERENCE.finditer("".join(buffer))
    ]
    failure = plural(len(matches), "hard-coded reference candidate")
    outcome = CheckOutcome(matches, [*document.graph_uncertainty, *reading.project_macro_reasons()])
    return [policy_finding(_HARDCODED, outcome, failure=failure)]
