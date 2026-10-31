"""Telling expected failures from defects, and describing a defect without any paths.

A ``PreparationError`` or ``OSError`` is an expected failure: the input, a setting or the
machine is at fault and the message says how to fix it. Any other exception is a
defect in Fledge. It is still reported as a finding, with the frames of its
traceback (file name, line and function only) so that a bug report can name the
failing code without revealing where the researcher's files live.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from .errors import PreparationError

# The innermost frames name the failing code; older ones only show how it was reached.
_TRACEBACK_FRAME_LIMIT = 8

REPORT_DEFECT_SUGGESTION = (
    "This is a defect in Fledge, not a problem with your files. Report it with the command "
    "you ran and the JSON report; the finding's traceback summary names the failing code."
)


def is_expected_failure(error: BaseException) -> bool:
    """Return whether ``error`` is a problem with the input, a setting or the machine."""
    return isinstance(error, PreparationError | OSError)


def summarize_traceback(error: BaseException) -> tuple[str, ...]:
    """Summarize the innermost traceback frames of ``error`` without their directories.

    Args:
        error: The exception whose traceback is summarized.

    Returns:
        One ``file.py:line in function`` entry per frame, for at most the innermost
        ``_TRACEBACK_FRAME_LIMIT`` frames.
    """
    frames = traceback.extract_tb(error.__traceback__)[-_TRACEBACK_FRAME_LIMIT:]
    return tuple(f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in frames)
