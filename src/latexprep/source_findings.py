"""Where the source graph records what it could not establish, and the wording it shares.

A graph keeps two lists: every finding in the order it was found, and the inconclusive
subset (the uncertainties) that says the lexical subset could not follow something.
``GraphFindings`` writes to both, so the modules that load, check, resolve and walk
sources report through one object and a repeated finding is recorded once.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .finding_text import more_suffix
from .findings import Finding
from .source_file_commands import (
    BIBLIOGRAPHY_RESOURCE_COMMANDS,
    OPAQUE_FILE_COMMANDS,
)
from .tex_commands import TEX_SOURCE_SUFFIXES

# Most paths named in one message; the rest are counted.
MAX_LISTED_PATHS = 5
# Most lines a merged finding lists in its details.
MAX_LISTED_LINES = 10
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

    def merge_per_rule(self, merged: Callable[[str], bool]) -> None:
        r"""Merge the uncertainties of each file that ``merged`` selects into one per rule.

        Class and package files are macro code: ``\csname``, conditional loads and computed
        file names are routine there, and none of them hides manuscript text. One finding
        per rule and file says what flat layout cannot follow, at its first line, with the
        commands, lines and count in its details. Errors are never merged, and neither are
        findings that differ in what they can hide (:func:`_hiding_class`): a figure in a
        macro and an ``\input`` in a macro stay two findings, so that the ``\input`` is
        still a gap in the manuscript.

        Args:
            merged: Whether the uncertainties of a project file are merged.
        """
        groups: dict[tuple[str, str, str], list[Finding]] = {}
        for item in self.uncertainties:
            if item.path and merged(item.path):
                key = (item.path, item.rule, _hiding_class(item))
                groups.setdefault(key, []).append(item)
        replacements = {id(items[0]): _merged(items) for items in groups.values() if len(items) > 1}
        dropped = {id(item) for items in groups.values() if len(items) > 1 for item in items[1:]}
        for target in (self.findings, self.uncertainties):
            target[:] = [
                replacements.get(id(item), item) for item in target if id(item) not in dropped
            ]

    def add(self, finding: Finding) -> None:
        """Record a finding of another kind, such as a lookup-order note."""
        self.findings.append(finding)


# The finding for a source that is not valid UTF-8 and was read all the same
# (``source_loading``). It keeps the graph from being complete, so nothing rewrites the
# file, but it hides no command, citation or text from a check.
FOREIGN_ENCODING_RULE = "source.foreign_encoding"


def _reads_no_source(finding: Finding) -> bool:
    r"""Return whether the command a finding concerns reads no manuscript source.

    That is a command of ``OPAQUE_FILE_COMMANDS``, or an unknown command each of whose
    possible file names has a suffix outside ``TEX_SOURCE_SUFFIXES``, as in
    ``\plot{data.csv}``; ``\loadparts{abstract.tex, logo.png}`` may read source. A
    finding that names no command, such as a cycle or a parse limit, may hide anything,
    except the note that a source is not UTF-8: that source was read.
    """
    if finding.rule == FOREIGN_ENCODING_RULE:
        return True
    command = finding.details.get("command")
    if not isinstance(command, str):
        return False
    if command in OPAQUE_FILE_COMMANDS:
        return True
    argument = finding.details.get("argument")
    if finding.rule != "source.custom_path" or not isinstance(argument, str):
        return False
    suffixes = [PurePosixPath(item.strip()).suffix.lower() for item in argument.split(",")]
    return all(suffix and suffix not in TEX_SOURCE_SUFFIXES for suffix in suffixes)


def _hiding_class(finding: Finding) -> str:
    """Return what a source finding can hide, so that only alike findings are merged."""
    if _reads_no_source(finding):
        return "nothing"
    if finding.details.get("command") in BIBLIOGRAPHY_RESOURCE_COMMANDS:
        return "bibliography"
    return "text"


def can_hide_citations(finding: Finding) -> bool:
    r"""Return whether a source finding can hide a citation or a bibliography declaration.

    Only a finding about a file that holds no TeX source, such as a figure inside
    ``\subfloat{…}``, cannot; ``\InputIfFileExists`` and ``\includefrom`` read source.
    """
    return not _reads_no_source(finding)


def can_hide_manuscript_text(finding: Finding) -> bool:
    """Return whether a source finding can hide manuscript text, a float or a declaration.

    As :func:`can_hide_citations`, and a bibliography resource holds no manuscript text
    either.
    """
    return (
        can_hide_citations(finding)
        and finding.details.get("command") not in BIBLIOGRAPHY_RESOURCE_COMMANDS
    )


def _merged(items: list[Finding]) -> Finding:
    """Return one finding that stands for several of one rule in one file.

    Args:
        items: The findings, at least two, in the order they were found.

    Returns:
        The first finding by line, its message followed by the count and the commands. It
        keeps the first finding's ``command`` only when every merged finding names one
        command: a merged group of several commands names none, and so may hide anything
        that :func:`_hiding_class` allowed for each of them.
    """
    first = min(items, key=lambda item: item.line or 0)
    names = sorted({str(item.details["command"]) for item in items if "command" in item.details})
    details = {key: value for key, value in first.details.items() if key != "command"}
    if len(names) == 1 and all("command" in item.details for item in items):
        details["command"] = names[0]
    commands = ", ".join("\\" + name for name in names)
    listed = f" ({commands})" if names else ""
    return replace(
        first,
        message=f"{first.message} This class or package file has {len(items)} "
        f"such findings{listed}.",
        details={
            **details,
            "commands": names,
            "occurrences": len(items),
            "lines": sorted({item.line for item in items if item.line})[:MAX_LISTED_LINES],
        },
    )
