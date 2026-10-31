"""Choosing the main file of a project: the configured one, else the only candidate root.

Both the document workflow and the bibliography command start from this choice. The
workflow reports no choice as a finding and stops; the bibliography command can still
check every ``.bib`` file without a main file. This module does not read the sources
beyond finding candidate roots, and it skips that scan when a main file is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from .errors import RequestError
from .findings import Finding
from .job_context import JobContext
from .path_safety import is_relative_posix_path
from .report import Report
from .source_analysis import discover_roots

# The rule of the finding that explains why no main file was selected.
MAIN_SELECTION_RULE = "project.main_selection"


class MainChoice(NamedTuple):
    """The selected main file, and the candidate roots found while selecting it.

    Attributes:
        main: A project-relative POSIX path, or ``None`` when no main file could be chosen.
        roots: The candidate roots; empty when a main file was configured, because no scan
            was needed.
    """

    main: str | None
    roots: list[str]


def names_project_file(snapshot: Path, main: str) -> bool:
    """Return whether ``main`` is a relative path to a file inside the imported project.

    Args:
        snapshot: The folder of the imported project.
        main: The configured main file.

    Returns:
        ``True`` when ``main`` is a relative POSIX path to a file under ``snapshot``.
    """
    return is_relative_posix_path(main, normalized=False) and (snapshot / main).is_file()


def main_selection_error(main: str, roots: list[str]) -> RequestError:
    r"""Build the error for a configured main file that is not a file of the project.

    Args:
        main: The configured main file.
        roots: The candidate roots, named in the hint; when empty, the hint says that no
            file with ``\documentclass`` was found.

    Returns:
        The request error, with a hint on how to set the main file.
    """
    found = (
        f"documents found in the input: {', '.join(roots)}"
        if roots
        else "no file with \\documentclass was found in the input"
    )
    return RequestError(
        f"main must name a source file inside the project: {main}",
        "Set --main (or main in the configuration) to a .tex path relative to the input "
        f"folder or ZIP root; {found}.",
    )


async def resolve_main(context: JobContext, snapshot: Path) -> MainChoice:
    """Choose the configured main file or the only candidate root, checked against ``snapshot``.

    No choice (several roots, or none) is not an error: the caller decides what it means.

    Args:
        context: The job context, whose settings name the configured main file and which
            runs the root discovery in a thread.
        snapshot: The folder of the imported project.

    Returns:
        The main file, or ``None`` when no choice was made, and the candidate roots.

    Raises:
        RequestError: A main file is configured but is not a project file.
    """
    selected = context.settings.main
    roots = (
        []
        if selected
        else await context.thread_operation("discover roots", discover_roots, snapshot)
    )
    if selected is None and len(roots) == 1:
        selected = roots[0]
    if selected is None:
        return MainChoice(None, roots)
    if not names_project_file(snapshot, selected):
        roots = roots or await context.thread_operation("discover roots", discover_roots, snapshot)
        raise main_selection_error(selected, roots)
    return MainChoice(Path(selected).as_posix(), roots)


def _main_selection_finding(roots: list[str]) -> Finding:
    """Explain why no main file was selected: several candidate roots, or none.

    Args:
        roots: The candidate roots that were found.

    Returns:
        An inconclusive error finding with a suggestion for choosing the main file.
    """
    if roots:
        message = (
            f"Select --main explicitly; found {len(roots)} candidate roots: {', '.join(roots)}"
        )
        suggestion = (
            "Rerun with --main set to one of the candidate roots, or set main in the configuration."
        )
    else:
        message = "No main file could be selected: no file with \\documentclass was found"
        suggestion = (
            "Check that INPUT is the paper's source folder or ZIP; no file with "
            "\\documentclass was found, so name the main file with --main."
        )
    return Finding(
        MAIN_SELECTION_RULE, message, severity="error", status="inconclusive", suggestion=suggestion
    )


async def select_main(context: JobContext, report: Report, snapshot: Path) -> str | None:
    """Select the main file of the document workflow.

    Args:
        context: The job context, as for :func:`resolve_main`.
        report: The report that receives a finding when no main file could be chosen.
        snapshot: The folder of the imported project.

    Returns:
        The project-relative main file, or ``None`` when none could be chosen.

    Raises:
        RequestError: A main file is configured but is not a project file.
    """
    choice = await resolve_main(context, snapshot)
    if choice.main is None:
        report.findings.append(_main_selection_finding(choice.roots))
    return choice.main
