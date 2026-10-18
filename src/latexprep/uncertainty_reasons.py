"""Why a literal declaration may not take effect where it is written.

The checks read declarations (document class, headings, metadata, captions) from the
source text without executing TeX. A command inside a group or macro argument, or
inside a conditional, is not known to run, so a check that finds it records a reason
instead of a verdict. This module is the one place that words those reasons.
"""

from __future__ import annotations

from collections.abc import Sequence

from .tex_lexer import Command, in_conditional

CONDITIONAL_FLOW = "conditional source control flow"


def grouped_reason(noun: str) -> str:
    """The reason for a ``noun`` written in a group or a macro argument."""
    return f"{noun} inside a group or macro argument"


def context_reasons(
    command: Command, conditionals: Sequence[tuple[int, int]], noun: str
) -> list[str]:
    """Reasons ``command``, a declaration of ``noun``, may not execute: grouped, conditional."""
    reasons = []
    if command.depth:
        reasons.append(grouped_reason(noun))
    if in_conditional(conditionals, command.start):
        reasons.append(CONDITIONAL_FLOW)
    return reasons
