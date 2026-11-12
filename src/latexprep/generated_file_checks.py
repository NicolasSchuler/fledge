r"""Shipped generated index, glossary and nomenclature files, and lists a build never got.

``\printindex``, ``\printglossary`` and ``\printnomenclature`` print files that makeindex
or makeglossaries write (``generated_files``). A service that runs neither, arXiv among
them, needs those files shipped beside the main file. Preparation ships the ones its
verified build generated, so this check mostly informs ``check`` runs and confirms the
prepared package: a missing file is a scope note unless the settings say that the
receiving service runs neither tool. The same rule reports a list that a build looked
for although no tool wrote it, which the build evidence (``build_dependencies``) names.
No source is modified.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .expanded_document import commands_before_end
from .finding_text import more_suffix
from .findings import EvidenceKind, Finding
from .generated_files import generated_kind, production_hint
from .path_safety import is_relative_posix_path
from .source_graph import SourceGraph
from .tex_lexer import Command

_RULE = "source.generated_lists"
_MAX_LISTED = 3
# The commands that print a generated list and the suffix of the file each reads.
_PRINT_COMMANDS = {
    "printindex": ".ind",
    "printglossary": ".gls",
    "printglossaries": ".gls",
    "printacronyms": ".acr",
    "printnomenclature": ".nls",
}


def _list_base(command: Command, stem: str) -> tuple[str, str] | None:
    r"""Return the base name and suffix of the file a print command reads.

    imakeidx names an index in the option of ``\printindex[name]``, and glossaries
    names the acronym list in ``\printglossary[type=acronym]``; another glossary type
    has a suffix of its own definition and is not checked.

    Args:
        command: A print command of ``_PRINT_COMMANDS``.
        stem: The main file's name without suffix, the default base name.

    Returns:
        The base name and the suffix, or ``None`` for a list this check cannot name.
    """
    suffix = _PRINT_COMMANDS[command.name]
    option = command.options[0].value.strip() if command.options else ""
    if command.name == "printindex" and option:
        return (option, suffix) if is_relative_posix_path(option, characters="literal") else None
    if command.name == "printglossary" and option:
        settings = {item.strip().replace(" ", "") for item in option.split(",")}
        if "type=acronym" in settings:
            return stem, ".acr"
        if any(item.startswith("type=") and item != "type=main" for item in settings):
            return None
    return stem, suffix


def expected_lists(graph: SourceGraph, selected: set[str]) -> dict[str, tuple[str, int]]:
    """Find the generated list files the selected sources print, beside the main file.

    Args:
        graph: The literal source graph; it must have a selected main file.
        selected: The selected sources.

    Returns:
        The first source and line that prints each expected file, by the file's
        project-relative name.
    """
    main = PurePosixPath(graph.main or "main.tex")
    expected: dict[str, tuple[str, int]] = {}
    for name in sorted(selected, key=lambda item: (item != graph.main, item)):
        for command in commands_before_end(graph.sources[name]):
            if command.name not in _PRINT_COMMANDS:
                continue
            base = _list_base(command, main.stem)
            if base is not None:
                listed = (main.parent / (base[0] + base[1])).as_posix()
                expected.setdefault(listed, (name, command.line))
    return expected


def _contents(names: list[str]) -> str:
    """Describe what the generated files hold, such as ``"index and glossary"``."""
    kinds = list(dict.fromkeys(kind.content for name in names if (kind := generated_kind(name))))
    return " and ".join(kinds) if len(kinds) < 3 else ", ".join(kinds[:-1]) + " and " + kinds[-1]


def check_generated_lists(
    graph: SourceGraph, selected: set[str], *, venue_runs_index_tools: bool
) -> list[Finding]:
    """Check that the index, glossary and nomenclature files the document prints are shipped.

    Args:
        graph: The literal source graph, whose files are the shipped set; it must have a
            selected main file.
        selected: The selected sources.
        venue_runs_index_tools: Whether the receiving service runs makeindex and
            makeglossaries; when it does not, a missing file is a warning.

    Returns:
        One finding when the document prints a generated list, otherwise none.
    """
    expected = expected_lists(graph, selected)
    if not expected or graph.main is None:
        return []
    main = graph.main
    names = sorted(expected)
    missing = [name for name in names if name not in graph.files]
    details: dict[str, object] = {
        "expected": names,
        "missing": missing,
        "venue_runs_index_tools": venue_runs_index_tools,
        "scope": "Literal print commands in the selected source graph; the shipped file set "
        "is the supplied or prepared directory.",
    }
    if not missing:
        return [
            Finding(
                _RULE,
                f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} shipped beside {main} "
                f"for its {_contents(names)}.",
                severity="info",
                status="passed",
                path=names[0] if len(names) == 1 else main,
                details=details,
            )
        ]
    # Point at the first command that prints a missing list, the main file's first.
    path, line = min(
        (expected[name] for name in missing), key=lambda place: (place[0] != main, place)
    )
    shown = ", ".join(missing[:_MAX_LISTED]) + more_suffix(len(missing) - _MAX_LISTED)
    if venue_runs_index_tools:
        consequence = (
            "Only a service that runs neither makeindex nor makeglossaries needs them; arXiv "
            "runs neither, which submission_checks.venue_runs_index_tools = false records (the "
            "arxiv preset sets it)."
        )
    else:
        consequence = (
            "The receiving service runs neither makeindex nor makeglossaries, so the list "
            "would be empty there."
        )
    return [
        Finding(
            _RULE,
            f"{main} prints its {_contents(missing)}, but {shown} "
            f"{'is' if len(missing) == 1 else 'are'} not in the package. {consequence} "
            "prepare ships the files its verified build generated.",
            severity="info" if venue_runs_index_tools else "warning",
            status="not_applicable" if venue_runs_index_tools else "failed",
            path=path,
            line=line,
            evidence_kind=EvidenceKind.DERIVED,
            details=details,
        )
    ]


def never_created_findings(
    never_created: tuple[str, ...], main: str, *, xindy: tuple[str, ...] = ()
) -> list[Finding]:
    """Report the generated lists a build looked for that no tool wrote.

    The document wrote entries for each list (the tool's input is not empty), so the
    PDF lacks it. A glossary list written for xindy is not built at all, because the
    isolated build does not run xindy, and its finding says so.

    Args:
        never_created: The never-created files, relative to the build's output folder.
        main: The main file, which the findings name.
        xindy: The files among them that the build left empty because the glossaries
            package writes them for xindy.

    Returns:
        One warning per file, naming what it holds and the tool that writes it.
    """
    findings = []
    for name in never_created:
        kind = generated_kind(name)
        if kind is None:
            continue
        if name in xindy:
            tool = f"{kind.tool} with xindy"
            message = (
                f"The {kind.content} in {name} was not built, so the PDF has none: the "
                "glossaries package writes it for xindy, which is not available in the "
                "isolated build."
            )
        else:
            tool = kind.tool
            message = (
                f"The build looked for {name}, but no tool wrote it, so the PDF has no "
                f"{kind.content}: {production_hint(name)}."
            )
        findings.append(
            Finding(
                _RULE,
                message,
                severity="warning",
                status="failed",
                path=main,
                evidence_kind=EvidenceKind.DIRECT,
                suggestion=f"Run {tool} locally and ship {name} beside the main file; the "
                "isolated build then reads it instead of an empty list.",
                details={"never_created": name, "tool": tool},
            )
        )
    return findings
