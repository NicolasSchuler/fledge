"""Generated files that TeX reads back, what each holds and the tool that writes it.

A LaTeX build writes some of its content with external tools and reads the result on a
later pass: the bibliography (``.bbl``, written by BibTeX or Biber from the ``.aux`` or
``.bcf``), the index (``.ind``, by makeindex from the ``.idx``), the glossary lists
(``.gls``, ``.acr`` and ``.sls``, by makeglossaries) and the nomenclature (``.nls``, by
makeindex with nomencl's style). A service that does not run the tool needs the
generated file shipped beside the main file. This module only names these files and
their tools; seeding them into a build, shipping them and checking that they are shipped
is the work of ``build_workspace``, ``package_inputs`` and ``submission_readiness_checks``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import NamedTuple


class GeneratedKind(NamedTuple):
    """What a generated file holds and how a build makes it.

    Attributes:
        content: What the file holds, as a researcher calls it, such as ``"index"``.
        tool: The tool that writes the file, as a researcher names it.
        tool_inputs: The suffixes of the files the tool reads, which the TeX run writes.
    """

    content: str
    tool: str
    tool_inputs: tuple[str, ...]


BIBLIOGRAPHY_SUFFIX = ".bbl"
_KINDS: dict[str, GeneratedKind] = {
    BIBLIOGRAPHY_SUFFIX: GeneratedKind("bibliography", "BibTeX or Biber", (".aux", ".bcf")),
    ".ind": GeneratedKind("index", "makeindex", (".idx",)),
    ".gls": GeneratedKind("glossary", "makeglossaries", (".glo",)),
    ".acr": GeneratedKind("list of acronyms", "makeglossaries", (".acn",)),
    ".sls": GeneratedKind("list of symbols", "makeglossaries", (".slo",)),
    ".nls": GeneratedKind(
        "nomenclature", "makeindex with nomencl's style (nomencl.ist) or makeglossaries", (".nlo",)
    ),
}
# The index, glossary and nomenclature files. arXiv runs neither makeindex nor
# makeglossaries, and a service that does run them only regenerates the files, so
# preparation always ships the ones its verified build generated.
INDEX_SUFFIXES = tuple(suffix for suffix in _KINDS if suffix != BIBLIOGRAPHY_SUFFIX)
GENERATED_SUFFIXES = (BIBLIOGRAPHY_SUFFIX, *INDEX_SUFFIXES)


def generated_kind(name: str) -> GeneratedKind | None:
    """Return what the generated file ``name`` holds and which tool writes it.

    Args:
        name: A file name or path; only its suffix counts.

    Returns:
        The kind, or ``None`` when the suffix is not one of ``GENERATED_SUFFIXES``.
    """
    return _KINDS.get(PurePosixPath(name).suffix)


def production_hint(name: str) -> str | None:
    """Say what the generated file ``name`` holds and which tool writes it from which input.

    Args:
        name: The file name or path, which the hint repeats.

    Returns:
        A clause such as ``"main.ind holds the index, which makeindex writes from main.idx"``,
        or ``None`` when ``name`` is not a generated file. The name is shown as given.
    """
    kind = generated_kind(name)
    if kind is None:
        return None
    path = PurePosixPath(name)
    sources = " or ".join(path.with_suffix(suffix).name for suffix in kind.tool_inputs)
    return f"{name} holds the {kind.content}, which {kind.tool} writes from {sources}"
