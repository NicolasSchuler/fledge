"""Expected failures that carry an actionable message, and the kinds callers tell apart.

Every error here is a problem with the input, a setting or the machine, as opposed to
a defect in Fledge; see ``defects.is_expected_failure``. This module imports nothing
else from the package, so the lowest-level modules can raise and catch these errors.
"""

from __future__ import annotations


class PreparationError(Exception):
    """An actionable input, configuration, or execution error."""


class RequestError(PreparationError):
    """An invalid request or setting that names its source and a concrete next step.

    Args:
        message: What is wrong.

    Attributes:
        suggestion: The concrete next step.
    """

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


class ToolUnavailableError(PreparationError):
    """A required external program is not installed or not on ``PATH``.

    Args:
        message: What is wrong.

    Attributes:
        tool: The program that is missing.
    """

    def __init__(self, message: str, tool: str) -> None:
        super().__init__(message)
        self.tool = tool


class SandboxRefusedError(PreparationError):
    """The restricted-execution backend cannot or will not isolate a command.

    A command is never run without isolation instead.
    """


class SettingsError(PreparationError):
    """An invalid settings value; ``settings`` names the top-level settings at fault.

    Args:
        message: What is wrong.

    Attributes:
        settings: The top-level settings at fault, in the spelling a researcher uses in a
            settings file or on the command line and in the order the message mentions
            them. A message that no single setting explains carries none.
    """

    def __init__(self, message: str, settings: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.settings = settings
