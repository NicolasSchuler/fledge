"""Shared test helpers: project fixtures and controlled build results.

New tests should import from here instead of redefining ``write``/``build`` helpers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from latexprep.models import Finding
from latexprep.runtime import BuildResult

MINIMAL_PDF = b"%PDF-controlled-test\n"
MINIMAL_DOCUMENT = "\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n"


def write_project(root: Path, files: Mapping[str, str | bytes]) -> Path:
    """Create ``root`` and write each relative path with its text or bytes; return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    base = root.resolve()
    for name, value in files.items():
        destination = (base / name).resolve()
        if base not in destination.parents:
            raise ValueError(f"Fixture path escapes the project root: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode() if isinstance(value, str) else value)
    return root


def fake_build_result(
    work: Path,
    main: str = "main.tex",
    *,
    success: bool = True,
    pdf_bytes: bytes = MINIMAL_PDF,
    findings: Iterable[Finding] = (),
    dependencies: Iterable[str] | None = None,
    submission_inputs: Iterable[str] | None = None,
    tools: Mapping[str, object] | None = None,
    command: Iterable[str] = (),
    log: str = "",
) -> BuildResult:
    """Return a ``BuildResult`` as ``latexprep.runtime.build_project`` would after a build.

    Like the controlled builders in the existing tests, this writes ``work/built.pdf`` (creating
    ``work``) and reports ``main`` as both the only dependency and the only submission input,
    unless the arguments say otherwise. A failed build records no PDF. The recorder is reported
    complete with no loaded packages, so dependency checks treat the trace as authoritative.
    """
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / "built.pdf"
    if success:
        pdf.write_bytes(pdf_bytes)
    return BuildResult(
        success=success,
        pdf=pdf if success else None,
        findings=list(findings),
        dependencies={main} if dependencies is None else set(dependencies),
        tools=dict(tools or {}),
        command=list(command),
        log=log,
        loaded_packages=[],
        recorder_complete=True,
        submission_inputs={main} if submission_inputs is None else set(submission_inputs),
    )


async def fake_build(tree, main, work, engine, runner, **_options) -> BuildResult:
    """Drop-in for ``patch("latexprep.core.build_project", side_effect=fake_build)``."""
    return fake_build_result(work, main)


def live_tests_enabled(*legacy: str) -> bool:
    """Whether opt-in live tool tests run: ``FLEDGE_RUN_INTEGRATION=1`` or a legacy variable."""
    import os

    return any(
        os.environ.get(name) == "1"
        for name in ("FLEDGE_RUN_INTEGRATION", "LATEX_PREP_RUN_INTEGRATION", *legacy)
    )
