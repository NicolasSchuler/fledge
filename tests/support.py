"""Shared test helpers: project fixtures, controlled build results and PDF artwork fixtures.

New tests should import from here instead of redefining ``write``/``build`` helpers.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from latexprep.build_result import BuildResult, BuildTools
from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.pdf_artwork_options import PdfContrastSample
from latexprep.pdf_check_options import PdfRegion
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner

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
    tools: BuildTools | None = None,
    command: Iterable[str] = (),
    log: str = "",
) -> BuildResult:
    """Return a ``BuildResult`` as ``latexprep.latexmk_build.build_project`` would after a build.

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
        recorder_project_inputs={main} if dependencies is None else set(dependencies),
        tools=tools or BuildTools(),
        command=list(command),
        log=log,
        loaded_packages=[],
        recorder_complete=True,
        submission_inputs={main} if submission_inputs is None else set(submission_inputs),
    )


async def fake_build(tree, main, work, engine, runner, **_options) -> BuildResult:
    """Drop-in for ``patch("latexprep.document_tasks.build_project", side_effect=fake_build)``."""
    return fake_build_result(work, main)


def live_tests_enabled(*legacy: str) -> bool:
    """Whether opt-in live tool tests run: ``FLEDGE_RUN_INTEGRATION=1`` or a legacy variable."""
    import os

    return any(
        os.environ.get(name) == "1"
        for name in ("FLEDGE_RUN_INTEGRATION", "LATEX_PREP_RUN_INTEGRATION", *legacy)
    )


# A valid 1 x 1 PNG, for tests that capture or embed preview images.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ap1kAAAAASUVORK5CYII="
)


def trace(content: str = "", *, box: str = "0 0 100 100") -> str:
    return f'<document><page number="1" mediabox="{box}">{content}</page></document>'


def rectangle(
    *, tag: str = "fill_path", left: int = 10, top: int = 10, right: int = 90, bottom: int = 90
) -> str:
    return (
        f'<{tag} transform="1 0 0 1 0 0"><moveto x="{left}" y="{top}"/>'
        f'<lineto x="{right}" y="{top}"/><lineto x="{right}" y="{bottom}"/>'
        f'<lineto x="{left}" y="{bottom}"/><closepath/></{tag}>'
    )


def raster(*, box: tuple[int, int, int, int] = (10, 10, 90, 90), background: int = 255) -> bytes:
    pixels = bytearray([background] * 10_000)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            pixels[y * 100 + x] = 255 - background
    return b"P5\n100 100\n255\n" + bytes(pixels)


def type3_objects(
    program: bytes = b"500 0 d0 0 0 20 20 re f", *, filtered: bool = False, nested: bool = False
) -> str:
    objects: dict[str, object] = {
        "trailer": {"value": {"/Root": "1 0 R"}},
        "obj:1 0 R": {"value": {"/Type": "/Catalog", "/Pages": "2 0 R"}},
        "obj:2 0 R": {"value": {"/Kids": ["3 0 R"]}},
        "obj:3 0 R": {"value": {"/Resources": {"/Font": {"/F1": "4 0 R"}}}},
        "obj:4 0 R": {
            "value": {
                "/Subtype": "/Type3",
                "/CharProcs": {"/A": "5 0 R"},
                "/Resources": {"/XObject": {"/Picture": "6 0 R"}},
            }
        },
        "obj:5 0 R": {
            "stream": {
                "dict": {"/Filter": "/FlateDecode"} if filtered else {},
                "data": base64.b64encode(program).decode(),
            }
        },
        "obj:6 0 R": {"value": {"/Subtype": "/Image"}},
    }
    if nested:
        objects["obj:6 0 R"] = {
            "stream": {
                "dict": {"/Subtype": "/Form", "/Resources": {"/XObject": {"/Picture": "7 0 R"}}},
                "data": base64.b64encode(b"/Picture Do").decode(),
            }
        }
        objects["obj:7 0 R"] = {"value": {"/Subtype": "/Image"}}
    return json.dumps(
        {"version": 2, "pages": [{"object": "3 0 R"}], "qpdf": [{"jsonversion": 2}, objects]}
    )


def rgb_raster(
    *,
    foreground: tuple[int, int, int] = (0, 0, 0),
    background: tuple[int, int, int] = (255, 255, 255),
    alternate: tuple[int, int, int] | None = None,
) -> bytes:
    pixels = bytearray(bytes(background) * 10_000)
    for y in range(10, 20):
        for x in range(10, 20):
            value = alternate if alternate is not None and x >= 15 else foreground
            offset = (y * 100 + x) * 3
            pixels[offset : offset + 3] = bytes(value)
    return b"P6\n100 100\n255\n" + pixels


def contrast_sample(*, minimum: float = 4.5, tolerance: int = 0) -> PdfContrastSample:
    return PdfContrastSample(
        "Label/background",
        PdfRegion("Foreground", 1, 10, 10, 20, 20),
        PdfRegion("Background", 1, 80, 80, 90, 90),
        minimum,
        tolerance,
    )


class ArtworkRunner(ToolRunner):
    def __init__(self, **values: object):
        super().__init__()
        self.geometry = (
            "Page 1 size: 100 x 100 pts\nPage 1 rot: 0\n"
            "Page 1 MediaBox: 0 0 100 100\nPage 1 CropBox: 0 0 100 100\n"
        )
        self.trace = trace(rectangle())
        self.objects = type3_objects()
        self.fonts = (
            "name type encoding emb sub uni object ID\n---\nFont Type 1 Builtin yes yes no 3 0\n"
        )
        self.raster = raster()
        self.rgb = rgb_raster()
        self.missing: str | None = None
        self.failure: str | None = None
        self.calls: list[tuple[list[str], Path, tuple[Path, ...]]] = []
        self.expected_budget: ResourceBudget | None = None
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()
        for key, value in values.items():
            setattr(self, key, value)

    def _resolve_tool(self, name: str) -> Path:
        return Path("/fixture-tools") / name

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        self._frozen.validate(readonly_inputs)
        if self.expected_budget is not None and self.expected_budget.current_lease is None:
            raise AssertionError("Artwork adapter escaped the shared resource lease")
        self.calls.append((list(argv), workspace, readonly_inputs))
        if argv[0] == self.missing:
            raise PreparationError(f"Missing tool: {argv[0]}")
        if argv[-1] in {"-v", "--version"}:
            return CommandResult(0, "Fixture tool 1\n", "", resolved_command=argv)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        output = self.geometry if argv[0] == "pdfinfo" and "-box" in argv else "Pages: 1\n"
        if argv[0] == "mutool":
            output = self.trace
        elif argv[0] == "qpdf":
            output = self.objects
        elif argv[0] == "pdffonts":
            output = self.fonts
        elif argv[0] == "pdftoppm":
            if "-gray" in argv:
                (workspace / "page.pgm").write_bytes(self.raster)
            else:
                (workspace / "page.ppm").write_bytes(self.rgb)
        return CommandResult(
            0,
            output,
            "",
            resolved_command=argv,
            timed_out=self.failure == "timeout",
            output_limited=self.failure == "output",
            resource_exceeded="Resource exceeded" if self.failure == "resource" else None,
        )
