"""The frozen PDFs, remembered measurements and page-render digests that one job shares.

A job looks at the same PDF several times: the baseline PDF is inspected, compared with the
prepared PDF, perhaps with a reference and with its own repeats; the prepared PDF is compared
twice. Each look used to freeze the file again, run the same Poppler commands again and render
the same pages again. A job scope (``pdf_job_scope``, entered by ``JobExecution``) gives every
PDF of the job one ``SharedPdfSession`` and one ``PageDigests``, so a read-only measurement
runs once per PDF and a page whose render was already digested is not rendered again.

Outside a job scope nothing is shared: ``job_pdf_sessions`` finds no registry and each caller
opens what it needs, exactly as before. A registry serves only the runner and budget it was
created for, so a call that passes others is not given its sessions. Everything it holds is
released when the scope ends; the memoized tool output is bounded by the runner's output limit
for each distinct command, and a digest is 32 bytes for each rendered page.

Does not own: freezing and measuring (``pdf_tools``), rendering (``pdf_page_renders``) or when
a job needs a PDF.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .command_runner import CommandRunner, ToolVersion
from .errors import PreparationError
from .pdf_tools import SharedPdfSession, pdf_tool_version_argv
from .resource_budget import ResourceBudget

logger = logging.getLogger(__name__)

# The frozen copies of the job's PDFs live below this directory of the job's workspace, one
# numbered directory each, and the version probes below ``_PROBES_DIRECTORY``.
_SESSIONS_DIRECTORY = "pdf-sessions"
_PROBES_DIRECTORY = "tool-probes"
_active: ContextVar[PdfJobSessions | None] = ContextVar("latexprep_pdf_job_sessions", default=None)


@dataclass(frozen=True, slots=True)
class PageRaster:
    """What a rendered comparison page is known by: its pixel size and a digest of its pixels.

    Two pages with equal ``PageRaster`` values have identical rasters.

    Attributes:
        width: The width of the raster in pixels.
        height: The height of the raster in pixels.
        digest: The SHA-256 digest of the interleaved samples.
    """

    width: int
    height: int
    digest: bytes


class PageDigests:
    """The digests of the pages of one PDF, by page and the resolution they were rendered at.

    A PDF compared twice at different resolutions, such as a poster against a reference
    with larger pages, keeps one digest per resolution. The version of the renderer that
    produced them is kept too, because a comparison that relies on a digest relied on that
    renderer and reports its version.
    """

    def __init__(self) -> None:
        self._pages: dict[tuple[int, int], PageRaster] = {}
        self._renderer: ToolVersion | None = None

    @property
    def renderer(self) -> ToolVersion | None:
        """The version of the renderer that produced the digests, once there is one."""
        return self._renderer

    def get(self, page: int, dpi: int) -> PageRaster | None:
        """Return the digest of ``page`` at ``dpi``, or ``None`` when it was not rendered yet."""
        return self._pages.get((page, dpi))

    def record(self, page: int, dpi: int, raster: PageRaster, renderer: ToolVersion | None) -> None:
        """Remember the digest of ``page`` at ``dpi`` and the renderer that made it."""
        self._pages[page, dpi] = raster
        if renderer is not None:
            self._renderer = renderer

    def clear(self) -> None:
        """Forget every digest."""
        self._pages.clear()
        self._renderer = None


@dataclass(frozen=True, slots=True)
class JobPdf:
    """One PDF of a job.

    Attributes:
        session: The frozen PDF and the measurements remembered for it.
        digests: The digests of its rendered pages.
    """

    session: SharedPdfSession
    digests: PageDigests


class PdfJobSessions:
    """The sessions and digests of every PDF one job looks at, and the early tool probes.

    Args:
        work: The job's workspace, below which the frozen PDFs and the probes are written.
        runner: Runs the PDF tools.
        budget: Admits the commands, if any.
    """

    def __init__(self, work: Path, runner: CommandRunner, budget: ResourceBudget | None) -> None:
        self._work = work
        self._runner = runner
        self._budget = budget
        self._pdfs: dict[tuple[str, int, int], JobPdf] = {}
        self._probes: list[asyncio.Task[None]] = []
        self._probed: set[str] = set()

    def serves(self, runner: CommandRunner, budget: ResourceBudget | None) -> bool:
        """Return whether this registry was created for exactly this runner and budget."""
        return runner is self._runner and budget is self._budget

    def pdf(self, pdf: Path) -> JobPdf:
        """Return the session and digests of ``pdf``, creating them on first use.

        The PDF is identified by its resolved path, size and modification time, so a file
        that was replaced is not served from the memory of its predecessor. The session
        freezes the file when it is first asked for a view.

        Args:
            pdf: The PDF.

        Returns:
            The same ``JobPdf`` for every call with the same file.
        """
        key = _identity(pdf)
        shared = self._pdfs.get(key)
        if shared is None:
            directory = self._work / _SESSIONS_DIRECTORY / f"pdf-{len(self._pdfs) + 1:03d}"
            shared = self._pdfs[key] = JobPdf(
                SharedPdfSession(pdf, directory, self._runner, self._budget), PageDigests()
            )
        return shared

    def warm_tool_probes(self, tools: Iterable[str]) -> None:
        """Start probing the versions of tools that the job will need, without waiting.

        The probes overlap whatever the job does meanwhile, typically the baseline build,
        and a command that needs a probed tool finds its version remembered. A tool already
        probed or being probed is skipped.

        Args:
            tools: The names of the PDF tools.
        """
        fresh = set(tools) - self._probed
        if not fresh:
            return
        self._probed |= fresh
        directory = self._work / _PROBES_DIRECTORY
        self._probes.append(
            asyncio.create_task(probe_pdf_tool_versions(self._runner, fresh, directory))
        )

    async def close(self) -> None:
        """Stop the probes still running and forget every PDF, measurement and digest."""
        for task in self._probes:
            task.cancel()
        results = await asyncio.gather(*self._probes, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.debug("PDF tool probe ahead of use ended with %s", type(result).__name__)
        self._probes.clear()
        for shared in self._pdfs.values():
            shared.session.release()
            shared.digests.clear()
        self._pdfs.clear()


async def probe_pdf_tool_versions(runner: CommandRunner, tools: Iterable[str], work: Path) -> None:
    """Probe the versions of ``tools`` concurrently, so that later commands find them remembered.

    The runner remembers each probe, so a command that needs the tool afterwards starts it
    at once instead of first waiting for its own probe, and the probes of several tools no
    longer follow one another on the path of the first measurement that needs each. A tool
    that is missing or has no usable version is not an error here: the command that needs
    it meets and reports the same failure.

    Args:
        runner: Probes the versions; it remembers each result.
        tools: The names of the tools, each probed once.
        work: The directory below which every probe gets a directory of its own, named for
            the tool; it need not exist yet.
    """

    async def probe(tool: str) -> None:
        """Probe one tool, leaving a failure to the command that needs it."""
        directory = work / tool
        directory.mkdir(parents=True, exist_ok=True)
        try:
            await runner.tool_version(pdf_tool_version_argv(tool), directory)
        except (PreparationError, OSError) as error:
            logger.debug(
                "PDF tool %s: version probe ahead of use failed (%s)", tool, type(error).__name__
            )

    await asyncio.gather(*(probe(tool) for tool in sorted(set(tools))))


def _identity(pdf: Path) -> tuple[str, int, int]:
    """Identify a PDF by its resolved path, size and modification time.

    A file that cannot be examined is identified by its path alone; the caller that
    freezes it meets the failure.
    """
    resolved = pdf.resolve()
    try:
        status = resolved.stat()
    except OSError:
        return str(resolved), -1, -1
    return str(resolved), status.st_size, status.st_mtime_ns


@contextlib.asynccontextmanager
async def pdf_job_scope(
    work: Path, runner: CommandRunner, budget: ResourceBudget | None
) -> AsyncGenerator[PdfJobSessions]:
    """Share the PDF sessions and digests of one job for the duration of the block.

    Args:
        work: The job's workspace.
        runner: Runs the PDF tools of the job.
        budget: The job's resource budget, if any.

    Yields:
        The registry that ``job_pdf_sessions`` hands to the job's tasks.
    """
    sessions = PdfJobSessions(work, runner, budget)
    token = _active.set(sessions)
    try:
        yield sessions
    finally:
        _active.reset(token)
        await sessions.close()


def job_pdf_sessions(runner: CommandRunner, budget: ResourceBudget | None) -> PdfJobSessions | None:
    """Return the registry of the job that is running, if it serves this runner and budget.

    Args:
        runner: The runner the caller uses.
        budget: The budget the caller uses.

    Returns:
        The registry, or ``None`` outside a job scope or for another runner or budget.
    """
    sessions = _active.get()
    return sessions if sessions is not None and sessions.serves(runner, budget) else None
