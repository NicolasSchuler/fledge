"""Running PDF tools: argument builders, frozen inputs, sessions and measurements.

Every external command the PDF checks run is built here, so equal measurements share
one argument list (and one memoized result within a session). A session freezes its
inputs and gives each command its own new directory; it never reads or interprets tool
output (``pdf_tool_output``, ``pdf_qpdf_graph``) or decides what a result means.

The check groups of one PDF can share a ``SharedPdfSession``: it freezes the PDF once
and hands each group a view with its own working directory and tool record over one
shared measurement memo, so a read-only measurement runs once however many groups ask.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, Self, TypeVar

from .cancellation import bounded_map, run_in_thread
from .command_runner import CommandResult, CommandRunner, ToolVersion
from .errors import PreparationError
from .findings import Finding
from .limits import DEFAULT_LEASE_MEMORY_MB
from .measurement_findings import (
    EVIDENCE_LIMITATION_SUGGESTION,
    findings_or_unavailable,
    unavailable_finding,
)
from .pdf_tool_output import page_count, pdfinfo_properties
from .resource_budget import ResourceBudget

_T = TypeVar("_T")

MEASUREMENT_RENDER_MEMORY_MB = 128
# Every external tool the PDF adapters can run; qpdf alone reports its version with --version.
PDF_TOOLS = frozenset(
    {
        "pdfinfo",
        "pdftotext",
        "pdftoppm",
        "pdffonts",
        "pdfdetach",
        "pdfimages",
        "pdftohtml",
        "qpdf",
        "mutool",
    }
)
_VERSION_ARGUMENTS = {"qpdf": "--version"}
# pdftoppm option and output file for each supported page raster.
_RASTER_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "gray": (("-gray",), "page.pgm"),
    "rgb": ((), "page.ppm"),
    "png": (("-png",), "page.png"),
}
_DOCUMENT_NAME = "document"


def pdf_tool_version_argv(tool: str) -> list[str]:
    """The command that prints the version of one of the ``PDF_TOOLS``."""
    return [tool, _VERSION_ARGUMENTS.get(tool, "-v")]


def pdf_input(name: str) -> str:
    """The argument a session replaces with the read-only path of a frozen input."""
    return f"@{name}@"


DOCUMENT = pdf_input(_DOCUMENT_NAME)


# Every external command the PDF checks run is built here, so equal measurements share
# one argument list (and one memoized result within a session).


def pdfinfo_argv(document: str = DOCUMENT) -> list[str]:
    """Document properties, with ISO 8601 dates and UTF-8 text.

    Every PDF check shares this form. The parsers split each line at its first colon,
    which an ISO date does not move, and Poppler writes UTF-8 whatever the locale.
    """
    return ["pdfinfo", "-enc", "UTF-8", "-isodates", document]


def page_boxes_argv(pages: int, document: str = DOCUMENT) -> list[str]:
    return ["pdfinfo", "-box", "-f", "1", "-l", str(pages), document]


def layout_text_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdftotext", "-enc", "UTF-8", "-layout", document, "-"]


def font_inventory_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdffonts", document]


def text_geometry_argv(document: str = DOCUMENT) -> list[str]:
    """Poppler XML text boxes: ``-i`` excludes image extraction, ``-nomerge`` keeps
    smaller text boxes apart."""
    return [
        "pdftohtml",
        "-xml",
        "-stdout",
        "-i",
        "-zoom",
        "1",
        "-noroundcoord",
        "-nomerge",
        "-enc",
        "UTF-8",
        document,
    ]


def attachment_list_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdfdetach", "-list", document]


def image_list_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdfimages", "-list", document]


def render_page_argv(
    page: int, dpi: int, raster: Literal["gray", "rgb", "png"], document: str = DOCUMENT
) -> list[str]:
    """Render one page as a single file named ``raster_filename(raster)`` in the working
    directory: 8-bit PGM (gray), PPM (rgb) or PNG."""
    options, _ = _RASTER_FORMATS[raster]
    return [
        "pdftoppm",
        "-f",
        str(page),
        "-l",
        str(page),
        "-singlefile",
        *options,
        "-r",
        str(dpi),
        document,
        "page",
    ]


def raster_filename(raster: Literal["gray", "rgb", "png"]) -> str:
    """The file ``render_page_argv`` writes in a command's working directory."""
    return _RASTER_FORMATS[raster][1]


def qpdf_objects_argv() -> list[str]:
    """qpdf JSON v2 object and page data without stream contents."""
    return [
        "qpdf",
        "--json=2",
        "--json-key=qpdf",
        "--json-key=pages",
        "--json-stream-data=none",
        DOCUMENT,
    ]


def qpdf_decoded_objects_argv(references: Iterable[str]) -> list[str]:
    """qpdf JSON v2 data for the selected indirect objects, with decoded inline streams.

    ``references`` are indirect references such as ``12 0 R``; only they are decoded, so
    image pixels and font files are never expanded.
    """
    selectors = [
        "--json-object=" + ",".join(reference.split()[:2]) for reference in sorted(references)
    ]
    return [
        "qpdf",
        "--json=2",
        "--json-key=qpdf",
        "--decode-level=all",
        "--json-stream-data=inline",
        *selectors,
        DOCUMENT,
    ]


def mupdf_trace_argv(pages: int) -> list[str]:
    return ["mutool", "trace", DOCUMENT, f"1-{pages}"]


async def freeze_pdf_inputs(
    work: Path, documents: Mapping[str, Path], runner: CommandRunner, budget: ResourceBudget | None
) -> tuple[Path, dict[str, Path]]:
    """Copy regular PDF inputs into a new workspace; return it resolved with the copies."""
    async with (
        budget.lease(f"{work.name}: freeze PDF inputs", memory_mb=DEFAULT_LEASE_MEMORY_MB)
        if budget is not None
        else nullcontext()
    ):
        if work.exists():
            raise PreparationError("PDF checks require a new, separate workspace")
        for document in documents.values():
            if not document.is_file() or document.is_symlink():
                raise PreparationError("PDF check input must be a regular, non-symlink file")
            if document.stat().st_size > runner.limits.max_file_bytes:
                raise PreparationError("PDF input exceeds the configured file size limit")
        work.mkdir(parents=True)
        work = work.resolve()
        frozen: dict[str, Path] = {}
        for name, document in documents.items():
            frozen[name] = runner.freeze_input(document, work / "inputs")
            with frozen[name].open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise PreparationError("PDF input has no PDF header")
        return work, frozen


# Poppler reports damaged input as "Syntax Error: ..." and, when it knows the byte offset,
# as "Syntax Error (257): ..."; MuPDF reports "error: ...". Warnings describe repairs the
# tool made, so they do not make the evidence incomplete.
_TOOL_ERROR_REPORT = re.compile(
    r"\b(?:Syntax|Command Line|I/O|Permission|Internal|Fontconfig) Error\b(?: \(\d+\))?:|^error:",
    re.IGNORECASE | re.MULTILINE,
)


def _require_complete(result: CommandResult, tool: str) -> None:
    if (reason := result.failure_reason(tool)) is not None:
        raise PreparationError(reason)
    if _TOOL_ERROR_REPORT.search(result.stderr):
        raise PreparationError(
            f"{tool} reported damaged or incomplete input: {result.stderr[:1000]}"
        )


@dataclass(frozen=True, slots=True)
class _Measured:
    """A completed measurement and the version of the tool that produced it."""

    result: CommandResult
    version: ToolVersion


@dataclass(slots=True)
class MeasurementMemo:
    """Completed read-only measurements by argument list, and a lock for each list.

    A memo is retained for as long as a session (or the views of a shared session) can
    ask for its results. It holds the stdout and stderr of each distinct argument list,
    each bounded by the runner's output limit, and ``clear`` releases all of it.
    """

    results: dict[tuple[str, ...], _Measured] = field(default_factory=dict)
    locks: dict[tuple[str, ...], asyncio.Lock] = field(default_factory=dict)

    def clear(self) -> None:
        self.results.clear()
        self.locks.clear()


@dataclass(slots=True)
class PdfToolSession:
    """Frozen PDF inputs and one new directory for each tool command.

    An argument equal to ``pdf_input(name)`` is replaced by the frozen copy of that
    input, which is the only read-only file the command may access.
    """

    work: Path
    inputs: dict[str, Path]
    runner: CommandRunner
    budget: ResourceBudget | None
    # The tools this session ran or relied on, for the per-document ``pdf.tools`` finding.
    # The runner's own ``tool_versions`` spans every tool of the run, which that finding
    # must not list.
    versions: dict[str, ToolVersion] = field(default_factory=dict)
    # Completed read-only measurements, shared by the views of one session; see ``measure``.
    memo: MeasurementMemo = field(default_factory=MeasurementMemo, repr=False)

    @classmethod
    async def open(
        cls,
        work: Path,
        documents: Mapping[str, Path],
        runner: CommandRunner,
        budget: ResourceBudget | None,
    ) -> Self:
        work, inputs = await freeze_pdf_inputs(work, documents, runner, budget)
        return cls(work, inputs, runner, budget)

    def view(self, work: Path) -> PdfToolSession:
        """The same frozen inputs and remembered measurements, with their own directory.

        Commands write below ``work`` and ``versions`` lists only the tools this view ran
        or relied on, so a check group reports its own tools however many groups share
        the measurements. ``work`` must not exist yet; a command creates it.
        """
        if work.exists():
            raise PreparationError("PDF checks require a new, separate workspace")
        return PdfToolSession(work, self.inputs, self.runner, self.budget, memo=self.memo)

    def lease(
        self,
        label: str,
        *,
        memory_mb: int = DEFAULT_LEASE_MEMORY_MB,
        kind: Literal["render"] | None = None,
    ) -> AbstractAsyncContextManager[object]:
        if self.budget is None:
            return nullcontext()
        return self.budget.lease(f"{self.work.name}: {label}", memory_mb=memory_mb, kind=kind)

    async def run_without_lease(
        self, name: str, argv: Sequence[str], *, memory_mb: int | None = None
    ) -> CommandResult:
        """Run one tool; the caller already holds a reservation that covers it.

        Each probe and command gets its own writable tool-state directory.
        """
        arguments = {pdf_input(key): path for key, path in self.inputs.items()}
        inputs = tuple(dict.fromkeys(arguments[arg] for arg in argv if arg in arguments))
        actual = [str(arguments[arg]) if arg in arguments else arg for arg in argv]
        tool = argv[0]
        leaf = self.work / name
        version_work, command_work = leaf / "version", leaf / "command"
        version_work.mkdir(parents=True)
        command_work.mkdir()
        self.versions[tool] = await self.runner.tool_version(
            pdf_tool_version_argv(tool), version_work
        )
        result = await self.runner.run(
            actual,
            cwd=command_work,
            workspace=command_work,
            readonly_inputs=inputs,
            memory_mb=memory_mb,
        )
        _require_complete(result, tool)
        return result

    async def run(self, name: str, argv: Sequence[str]) -> CommandResult:
        """Run one tool under its own small reservation and return its output."""
        async with self.lease(f"PDF {name}"):
            return await self.run_without_lease(name, argv)

    async def measure(
        self,
        name: str,
        argv: Sequence[str],
        parse: Callable[[CommandResult], _T],
        *,
        in_thread: bool = False,
    ) -> _T:
        """Run a read-only measurement and parse its output within one bounded lease.

        The session remembers each completed result by argument list, so asking for the
        same measurement again (the page boxes for two checks, say) parses the earlier
        output instead of running the tool again; the views of one shared session remember
        together. Concurrent requests for one argument list take turns outside any lease,
        so the tool still runs once; if the first is cancelled or fails, the next runs it.
        A remembered result also records the tool's version for this session, so its
        ``tool_versions`` are the same whether it ran the tool or reused the output. Only
        deterministic, read-only commands belong here; renders and anything that writes
        use ``run`` or ``render_page``.

        ``in_thread`` runs ``parse`` in a worker thread, still inside the lease: parsing
        that walks a large document then neither blocks the event loop nor ignores a
        cancellation, because ``cancellation_point`` reaches it there.
        """
        key = tuple(argv)
        memo = self.memo
        async with memo.locks.setdefault(key, asyncio.Lock()), self.lease(name):
            measured = memo.results.get(key)
            if measured is None:
                result = await self.run_without_lease(name, argv, memory_mb=DEFAULT_LEASE_MEMORY_MB)
                self._require_bounded_output(result)
                measured = memo.results[key] = _Measured(result, self.versions[key[0]])
            else:
                self.versions[key[0]] = measured.version
            result = measured.result
            return await run_in_thread(parse, result) if in_thread else parse(result)

    async def render_page(
        self,
        name: str,
        page: int,
        dpi: int,
        parse: Callable[[Path], _T],
        *,
        raster: Literal["gray", "rgb"],
    ) -> _T:
        """Render one page of the document and parse the raster file in a worker thread."""
        argv = render_page_argv(page, dpi, raster)
        path = self.work / name / "command" / raster_filename(raster)
        async with self.lease(name, memory_mb=MEASUREMENT_RENDER_MEMORY_MB, kind="render"):
            try:
                result = await self.run_without_lease(
                    name, argv, memory_mb=MEASUREMENT_RENDER_MEMORY_MB
                )
                self._require_bounded_output(result)
                return await run_in_thread(parse, path)
            finally:
                # Remove only this disposable render, including partial output.
                path.unlink(missing_ok=True)

    def tool_versions(self) -> dict[str, object]:
        return {name: self.versions[name].to_report() for name in sorted(self.versions)}

    def _require_bounded_output(self, result: CommandResult) -> None:
        if len(result.stdout.encode("utf-8")) > self.runner.limits.max_output_bytes:
            raise PreparationError("PDF measurement exceeds the diagnostic output limit")


async def open_pdf_session(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    budget: ResourceBudget | None,
    *,
    default_to_runner_budget: bool = True,
) -> PdfToolSession:
    """One frozen document.

    Configured measurements default to the runner's budget when none is supplied, so
    their commands stay admitted and bounded. The basic inspection passes
    ``default_to_runner_budget=False`` and runs under exactly the supplied budget.
    """
    if default_to_runner_budget:
        budget = budget or runner.budget
    return await PdfToolSession.open(work, {_DOCUMENT_NAME: pdf}, runner, budget)


class SharedPdfSession:
    """One PDF frozen once for all the check groups of a pipeline run, opened on first use.

    Each group asks for ``view`` and measures through it. The views share one measurement
    memo, so a read-only measurement (properties, page boxes, text, qpdf objects, MuPDF
    trace) runs once per PDF however many groups need it, while each view has its own
    working directory, so the output names of different groups cannot collide, and its own
    tool record, so each group's ``pdf.tools`` finding lists the tools it relied on.

    The first ``view`` freezes the PDF while later ones wait; a failure to open is
    remembered and raised to every group, as each group would have met it alone. A
    cancelled opening is not remembered: its partial workspace is removed and the next
    group opens the PDF again, as it does when a measurement is cancelled.

    Memory: from its first request until ``release`` the memo keeps the stdout and stderr
    of each distinct read-only command, each capped by the runner's output limit (parsed
    results are not kept). A pipeline run releases the session when its groups are done or
    cancelled (use it as a context manager); renders are never remembered.
    """

    def __init__(
        self, pdf: Path, work: Path, runner: CommandRunner, budget: ResourceBudget | None
    ) -> None:
        self.pdf = pdf
        self._work = work
        self._runner = runner
        self._budget = budget
        self._base: PdfToolSession | None = None
        self._failure: str | None = None
        self._released = False
        self._lock = asyncio.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    async def view(self, pdf: Path, work: Path) -> PdfToolSession:
        """A session over the shared frozen ``pdf`` that writes below the new ``work``.

        Raises the failure to freeze the PDF (``PreparationError`` or ``OSError``).
        """
        if pdf != self.pdf:
            raise ValueError("The shared PDF session belongs to a different PDF")
        return (await self._open()).view(work)

    async def _open(self) -> PdfToolSession:
        async with self._lock:
            if self._released:
                raise RuntimeError("The shared PDF session was released")
            if self._failure is not None:
                raise PreparationError(self._failure)
            if self._base is None:
                created = not self._work.exists()
                try:
                    self._base = await PdfToolSession.open(
                        self._work, {_DOCUMENT_NAME: self.pdf}, self._runner, self._budget
                    )
                except (PreparationError, OSError) as error:
                    self._failure = str(error)
                    raise
                except BaseException:
                    if created:
                        shutil.rmtree(self._work, ignore_errors=True)
                    raise
            return self._base

    def release(self) -> None:
        """Forget every remembered measurement; the session cannot be used again."""
        self._released = True
        if self._base is not None:
            self._base.memo.clear()
            self._base = None


async def gather_measurements(
    operations: Sequence[Callable[[], Awaitable[_T]]], budget: ResourceBudget | None
) -> list[_T | PreparationError]:
    """Keep completed evidence when a sibling fails, and drain on cancellation."""

    async def measure(operation: Callable[[], Awaitable[_T]]) -> _T | PreparationError:
        try:
            return await operation()
        except (PreparationError, OSError) as error:
            # Exception tracebacks can otherwise retain the failed worker's
            # large raster buffers until the complete comparison is collected.
            return PreparationError(str(error))

    return await bounded_map(
        operations, measure, limit=budget.cpu_slots if budget is not None else 1
    )


@dataclass(frozen=True, slots=True)
class PdfPreflight:
    """A frozen document whose pdfinfo properties and page count are established."""

    session: PdfToolSession
    fields: dict[str, str]
    pages: int

    @classmethod
    async def open(
        cls,
        pdf: Path,
        work: Path,
        runner: CommandRunner,
        budget: ResourceBudget | None,
        *,
        default_to_runner_budget: bool = True,
        session: SharedPdfSession | None = None,
    ) -> Self:
        """Freeze ``pdf`` and read its properties; raises when either cannot be established.

        With a shared ``session`` the frozen PDF, runner and budget are the shared ones and
        the properties may already be remembered; ``work`` is this caller's own directory.
        """
        if session is None:
            opened = await open_pdf_session(
                pdf, work, runner, budget, default_to_runner_budget=default_to_runner_budget
            )
        else:
            opened = await session.view(pdf, work)
        fields = await opened.measure(
            "properties", pdfinfo_argv(), lambda result: pdfinfo_properties(result.stdout)
        )
        return cls(opened, fields, page_count(fields))


async def preflight_pdf(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    budget: ResourceBudget | None,
    rules: Iterable[str],
    *,
    default_to_runner_budget: bool = True,
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION,
    session: SharedPdfSession | None = None,
) -> PdfPreflight | list[Finding]:
    """The preflighted document, or each rule of ``rules`` as unavailable evidence.

    Without a readable document, page count and unambiguous properties no configured
    measurement has anything to measure, so none of them may pass. A failure arrives as
    the list of unavailable findings the caller returns as its result.
    """
    try:
        return await PdfPreflight.open(
            pdf,
            work,
            runner,
            budget,
            default_to_runner_budget=default_to_runner_budget,
            session=session,
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error, suggestion=suggestion) for rule in rules]


@dataclass(frozen=True, slots=True)
class ToolMeasurement:
    """One read-only tool command, how its output becomes findings and the rules it feeds.

    Every check family describes its commands this way and runs them with ``run`` (or
    ``run_tool_measurements``), so a command that fails, is cut off or prints something
    unreadable leaves each of its rules inconclusive in the same way.
    """

    name: str
    argv: Sequence[str]
    rules: tuple[str, ...]
    evaluate: Callable[[CommandResult], list[Finding]]
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION

    async def run(self, session: PdfToolSession) -> list[Finding]:
        """The findings of ``evaluate`` over the command's output, or one unavailable
        finding for each of ``rules``. Evaluation runs in a worker thread."""
        return await findings_or_unavailable(
            self.rules,
            partial(session.measure, self.name, self.argv, self.evaluate, in_thread=True),
            suggestion=self.suggestion,
        )


async def run_tool_measurements(
    session: PdfToolSession, measurements: Sequence[ToolMeasurement]
) -> list[list[Finding]]:
    """Run measurements concurrently, within the session's CPU slots, in the given order."""
    slots = session.budget.cpu_slots if session.budget is not None else 1
    return await bounded_map(
        measurements, lambda measurement: measurement.run(session), limit=slots
    )
