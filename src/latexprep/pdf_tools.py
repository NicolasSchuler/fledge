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
import logging
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

logger = logging.getLogger(__name__)

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
    """Return the command that prints the version of one of the ``PDF_TOOLS``."""
    return [tool, _VERSION_ARGUMENTS.get(tool, "-v")]


def pdf_input(name: str) -> str:
    """Return the argument a session replaces with the read-only path of a frozen input."""
    return f"@{name}@"


DOCUMENT = pdf_input(_DOCUMENT_NAME)


# Every external command the PDF checks run is built here, so equal measurements share
# one argument list (and one memoized result within a session).


def pdfinfo_argv(document: str = DOCUMENT) -> list[str]:
    """Build the ``pdfinfo`` command for document properties, with ISO 8601 dates and UTF-8 text.

    Every PDF check shares this form. The parsers split each line at its first colon,
    which an ISO date does not move, and Poppler writes UTF-8 whatever the locale.
    """
    return ["pdfinfo", "-enc", "UTF-8", "-isodates", document]


def page_boxes_argv(pages: int, document: str = DOCUMENT) -> list[str]:
    """Build the ``pdfinfo`` command for the page boxes of pages 1 to ``pages``."""
    return ["pdfinfo", "-box", "-f", "1", "-l", str(pages), document]


def layout_text_argv(document: str = DOCUMENT) -> list[str]:
    """Build the ``pdftotext`` command that prints the UTF-8 text in its page layout."""
    return ["pdftotext", "-enc", "UTF-8", "-layout", document, "-"]


def font_inventory_argv(document: str = DOCUMENT) -> list[str]:
    """Build the ``pdffonts`` command that lists the fonts of the document."""
    return ["pdffonts", document]


def text_geometry_argv(document: str = DOCUMENT) -> list[str]:
    """Build the ``pdftohtml`` command that prints Poppler's XML text boxes.

    ``-i`` excludes image extraction, and ``-nomerge`` keeps smaller text boxes apart.
    """
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
    """Build the ``pdfdetach`` command that lists the embedded files."""
    return ["pdfdetach", "-list", document]


def image_list_argv(document: str = DOCUMENT) -> list[str]:
    """Build the ``pdfimages`` command that lists the images."""
    return ["pdfimages", "-list", document]


def render_page_argv(
    page: int, dpi: int, raster: Literal["gray", "rgb", "png"], document: str = DOCUMENT
) -> list[str]:
    """Build the ``pdftoppm`` command that renders one page.

    The page becomes a single file named ``raster_filename(raster)`` in the working
    directory: 8-bit PGM (gray), PPM (rgb) or PNG.

    Args:
        page: The page to render, from 1.
        dpi: The resolution in dots per inch.
        raster: The raster format.
        document: The input argument.

    Returns:
        The command.
    """
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
    """Return the file name ``render_page_argv`` writes in a command's working directory."""
    return _RASTER_FORMATS[raster][1]


def qpdf_objects_argv() -> list[str]:
    """Build the ``qpdf`` command for JSON v2 object and page data without stream contents."""
    return [
        "qpdf",
        "--json=2",
        "--json-key=qpdf",
        "--json-key=pages",
        "--json-stream-data=none",
        DOCUMENT,
    ]


def qpdf_decoded_objects_argv(references: Iterable[str]) -> list[str]:
    """Build the ``qpdf`` command for JSON v2 data of selected objects, with decoded streams.

    Args:
        references: Indirect references such as ``12 0 R``; only they are decoded, inline,
            so image pixels and font files are never expanded.

    Returns:
        The command.
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
    """Build the ``mutool trace`` command for pages 1 to ``pages``."""
    return ["mutool", "trace", DOCUMENT, f"1-{pages}"]


async def freeze_pdf_inputs(
    work: Path, documents: Mapping[str, Path], runner: CommandRunner, budget: ResourceBudget | None
) -> tuple[Path, dict[str, Path]]:
    """Copy regular PDF inputs into a new workspace; return it resolved with the copies.

    Args:
        work: The new workspace.
        documents: The PDF inputs, by name.
        runner: Freezes the inputs; its file size limit applies to them.
        budget: Admits the copying, if any.

    Returns:
        The resolved workspace and the frozen copy of each input, by name.

    Raises:
        PreparationError: ``work`` exists, or an input is not a regular file, is too large
            or has no PDF header.
    """
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


# Poppler reports damaged input as "<Category> Error: ...", for example "Syntax Error",
# "Config Error", "Command Line Error", "I/O Error", "Permission Error" or "Internal
# Error", and, when it knows the byte offset, as "Syntax Error (257): ..."; MuPDF reports
# "error: ...". Any "error:" in a tool's own words counts, wherever it stands on the line.
# Warnings ("Syntax Warning") and "Unimplemented Feature" describe repairs or gaps the
# tool worked around, so they do not make the evidence incomplete.
_TOOL_ERROR_REPORT = re.compile(r"\berror(?: \(\d+\))?:", re.IGNORECASE)


def _require_complete(result: CommandResult, tool: str) -> None:
    """Refuse the result of a command that failed, hit a limit or reported an error.

    Args:
        result: The command's result.
        tool: The tool's name, as the error mentions it.

    Raises:
        PreparationError: The command did not complete normally, or the tool reported
            damaged or incomplete input.
    """
    if (reason := result.failure_reason(tool)) is not None:
        raise PreparationError(reason)
    if _TOOL_ERROR_REPORT.search(result.stderr):
        raise PreparationError(
            f"{tool} reported damaged or incomplete input: {result.stderr[:1000]}"
        )


@dataclass(frozen=True, slots=True)
class _Measured:
    """A completed measurement and the version of the tool that produced it.

    Attributes:
        result: The command's result.
        version: The tool's version.
    """

    result: CommandResult
    version: ToolVersion


@dataclass(slots=True)
class MeasurementMemo:
    """Completed read-only measurements by argument list, and a lock for each list.

    A memo is retained for as long as a session (or the views of a shared session) can
    ask for its results. It holds the stdout and stderr of each distinct argument list,
    each bounded by the runner's output limit, and ``clear`` releases all of it.

    Attributes:
        results: The completed measurements, by argument list.
        locks: One lock per argument list.
    """

    results: dict[tuple[str, ...], _Measured] = field(default_factory=dict)
    locks: dict[tuple[str, ...], asyncio.Lock] = field(default_factory=dict)

    def clear(self) -> None:
        """Release every remembered measurement and lock."""
        self.results.clear()
        self.locks.clear()


@dataclass(slots=True)
class PdfToolSession:
    """Frozen PDF inputs and one new directory for each tool command.

    An argument equal to ``pdf_input(name)`` is replaced by the frozen copy of that
    input, which is the only read-only file the command may access.

    Attributes:
        work: The directory below which each command gets its own new directory.
        inputs: The frozen copy of each input, by name.
        runner: Runs the tools.
        budget: Admits the commands, if any.
        versions: The version of each tool this session ran or relied on.
        memo: The remembered measurements.
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
        """Freeze the documents into the new ``work`` and open a session over them.

        Args:
            work: The new workspace.
            documents: The PDF inputs, by name.
            runner: Runs the tools.
            budget: Admits the commands, if any.

        Returns:
            The session.

        Raises:
            PreparationError: An input cannot be frozen (see :func:`freeze_pdf_inputs`).
        """
        work, inputs = await freeze_pdf_inputs(work, documents, runner, budget)
        return cls(work, inputs, runner, budget)

    def view(self, work: Path) -> PdfToolSession:
        """Open a view with the same frozen inputs and remembered measurements.

        Commands write below ``work`` and ``versions`` lists only the tools this view ran
        or relied on, so a check group reports its own tools however many groups share
        the measurements. ``work`` must not exist yet; a command creates it.

        Returns:
            The view.

        Raises:
            PreparationError: ``work`` already exists.
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
        """Reserve budget for one operation of this session, if there is a budget.

        Args:
            label: Names the operation, after the session's directory.
            memory_mb: The memory to reserve in MiB.
            kind: ``render`` for a render, which counts against its class limit.

        Returns:
            The lease, or a context that reserves nothing.
        """
        if self.budget is None:
            return nullcontext()
        return self.budget.lease(f"{self.work.name}: {label}", memory_mb=memory_mb, kind=kind)

    async def run_without_lease(
        self, name: str, argv: Sequence[str], *, memory_mb: int | None = None
    ) -> CommandResult:
        """Run one tool; the caller already holds a reservation that covers it.

        Each probe and command gets its own writable tool-state directory.

        Args:
            name: The command's directory below ``work``.
            argv: The command; an argument ``pdf_input(name)`` becomes the frozen copy of
                that input.
            memory_mb: A memory limit for the command in MiB, if any.

        Returns:
            What the command printed and how it ended.

        Raises:
            PreparationError: The tool gave no usable version, or the command did not
                complete normally or reported damaged input.
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
        """Run one tool under its own small reservation and return its output.

        The arguments and exceptions are those of :meth:`run_without_lease`.
        """
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

        Args:
            name: The measurement's name, also its command directory.
            argv: The read-only command.
            parse: Turns the command's result into the measurement.
            in_thread: Runs ``parse`` in a worker thread, still inside the lease: parsing
                that walks a large document then neither blocks the event loop nor ignores
                a cancellation, because ``cancellation_point`` reaches it there.

        Returns:
            What ``parse`` returns.

        Raises:
            PreparationError: The command did not complete normally, reported damaged
                input or printed more than the output limit.
        """
        key = tuple(argv)
        memo = self.memo
        async with memo.locks.setdefault(key, asyncio.Lock()), self.lease(name):
            measured = memo.results.get(key)
            if measured is None:
                logger.debug("PDF measurement %s: not remembered, running %s", name, key[0])
                result = await self.run_without_lease(name, argv, memory_mb=DEFAULT_LEASE_MEMORY_MB)
                self._require_bounded_output(result)
                measured = memo.results[key] = _Measured(result, self.versions[key[0]])
            else:
                logger.debug("PDF measurement %s: reused the remembered %s output", name, key[0])
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
        """Render one page of the document and parse the raster file in a worker thread.

        The raster file is removed afterwards, partial output included.

        Args:
            name: The render's name, also its command directory.
            page: The page to render, from 1.
            dpi: The resolution in dots per inch.
            parse: Reads the raster file.
            raster: The raster format.

        Returns:
            What ``parse`` returns.

        Raises:
            PreparationError: The render did not complete normally, reported damaged
                input or printed more than the output limit.
        """
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
        """Return the version of each tool this session ran or relied on, as report data."""
        return {name: self.versions[name].to_report() for name in sorted(self.versions)}

    def _require_bounded_output(self, result: CommandResult) -> None:
        """Refuse a result whose standard output exceeds the runner's output limit.

        Raises:
            PreparationError: The output exceeds the limit.
        """
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
    """Open a session over one frozen document.

    Configured measurements default to the runner's budget when none is supplied, so
    their commands stay admitted and bounded. The basic inspection passes
    ``default_to_runner_budget=False`` and runs under exactly the supplied budget.

    Args:
        pdf: The document.
        work: The new workspace.
        runner: Runs the tools.
        budget: Admits the commands, if any.
        default_to_runner_budget: Whether to use the runner's budget when ``budget`` is
            ``None``.

    Returns:
        The session.

    Raises:
        PreparationError: The document cannot be frozen.
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

    Args:
        work: The new workspace that the first view freezes the PDF into.
        runner: Runs the tools.
        budget: Admits the commands, if any.

    Attributes:
        pdf: The shared PDF.
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
        """Return a session over the shared frozen ``pdf`` that writes below the new ``work``.

        Args:
            pdf: The PDF the caller expects; it must be the shared one.
            work: The new working directory of the view.

        Returns:
            The view.

        Raises:
            PreparationError: The PDF could not be frozen, now or for an earlier view, or
                ``work`` already exists.
            OSError: The PDF could not be frozen.
            ValueError: ``pdf`` is not the shared PDF.
            RuntimeError: The session was released.
        """
        if pdf != self.pdf:
            raise ValueError("The shared PDF session belongs to a different PDF")
        return (await self._open()).view(work)

    async def _open(self) -> PdfToolSession:
        """Freeze the PDF on first use and return the session that the views share.

        A failure to freeze is remembered; a cancelled opening removes the workspace it
        created and is not remembered.

        Returns:
            The shared session.

        Raises:
            PreparationError: The PDF could not be frozen, now or earlier.
            OSError: The PDF could not be frozen.
            RuntimeError: The session was released.
        """
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
    """Keep completed evidence when a sibling fails, and drain on cancellation.

    The operations run concurrently within the budget's CPU slots, one at a time without
    a budget. A ``PreparationError`` or ``OSError`` takes the place of its operation's
    result; any other exception cancels the other operations and propagates.

    Args:
        operations: The measurements to run.
        budget: Supplies the CPU slots, if any.

    Returns:
        Each operation's result, or its ``PreparationError`` or ``OSError`` converted to a
        plain ``PreparationError``, in the given order.
    """

    async def measure(operation: Callable[[], Awaitable[_T]]) -> _T | PreparationError:
        """Run one operation; return a ``PreparationError`` or ``OSError`` as a plain one."""
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
    """A frozen document whose pdfinfo properties and page count are established.

    Attributes:
        session: The session over the frozen document.
        fields: The pdfinfo properties.
        pages: The page count.
    """

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
        """Freeze ``pdf`` and read its properties.

        With a shared ``session`` the frozen PDF, runner and budget are the shared ones and
        the properties may already be remembered; ``work`` is this caller's own directory.

        Args:
            pdf: The document.
            work: The new workspace of this caller.
            runner: Runs the tools.
            budget: Admits the commands, if any.
            default_to_runner_budget: As for :func:`open_pdf_session`.
            session: A shared session for ``pdf``, if any.

        Returns:
            The preflighted document.

        Raises:
            PreparationError: The PDF cannot be frozen, or its properties or page count
                cannot be established.
            OSError: The PDF cannot be frozen.
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
    """Preflight the document, or report each rule of ``rules`` as unavailable evidence.

    Without a readable document, page count and unambiguous properties no configured
    measurement has anything to measure, so none of them may pass.

    Args:
        pdf: The document.
        work: The new workspace.
        runner: Runs the tools.
        budget: Admits the commands, if any.
        rules: The configured rules that need the document.
        default_to_runner_budget: As for :func:`open_pdf_session`.
        suggestion: The suggestion of each unavailable finding.
        session: A shared session for ``pdf``, if any.

    Returns:
        The preflighted document, or on failure the list of unavailable findings the
        caller returns as its result.
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

    Attributes:
        name: The measurement's name, also its command directory.
        argv: The read-only command.
        rules: The rules the command's output feeds.
        evaluate: Turns the command's result into findings.
        suggestion: The suggestion of each unavailable finding.
    """

    name: str
    argv: Sequence[str]
    rules: tuple[str, ...]
    evaluate: Callable[[CommandResult], list[Finding]]
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION

    async def run(self, session: PdfToolSession) -> list[Finding]:
        """Measure through ``session`` and evaluate the output in a worker thread.

        Returns:
            The findings of ``evaluate`` over the command's output, or one unavailable
            finding for each of ``rules``.
        """
        return await findings_or_unavailable(
            self.rules,
            partial(session.measure, self.name, self.argv, self.evaluate, in_thread=True),
            suggestion=self.suggestion,
        )


async def run_tool_measurements(
    session: PdfToolSession, measurements: Sequence[ToolMeasurement]
) -> list[list[Finding]]:
    """Run measurements concurrently, within the session's CPU slots, in the given order.

    Args:
        session: The session that runs the commands.
        measurements: The measurements to run.

    Returns:
        The findings of each measurement, in the given order.
    """
    slots = session.budget.cpu_slots if session.budget is not None else 1
    return await bounded_map(
        measurements, lambda measurement: measurement.run(session), limit=slots
    )
