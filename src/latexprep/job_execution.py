"""The resource budget, restricted runner, private workspace and deadline of one job.

Preparation jobs and setup diagnostics share this scaffolding, so both account
tool processes, memory and temporary bytes the same way.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from pathlib import Path

from .config import COORDINATOR_MEMORY_MB, Settings
from .runtime import RuntimeLimits, ToolRunner
from .scheduler import ResourceBudget


class JobDeadlineExceeded(TimeoutError):
    """The whole-job deadline passed.

    A ``TimeoutError`` subclass so that callers that already stop on a timeout keep
    working, but distinct from the ``TimeoutError`` of an individual socket or
    process, which must not be reported as the job running out of time.
    """


class JobExecution:
    """Create inside the job's coroutine: the whole-job deadline starts here."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.budget = ResourceBudget(
            settings.jobs,
            settings.memory_mb,
            build_jobs=settings.build_jobs,
            render_jobs=settings.render_jobs,
            coordinator_memory_mb=COORDINATOR_MEMORY_MB,
        )
        self.runner = ToolRunner(
            RuntimeLimits(
                timeout_seconds=settings.timeout_seconds,
                max_memory_mb=settings.build_memory_mb,
                max_file_bytes=settings.max_bytes,
            ),
            budget=self.budget,
            source_date_epoch=settings.source_date_epoch,
        )
        self.started = time.monotonic()
        self._deadline = self.started + settings.job_timeout_seconds
        self._task = asyncio.current_task()

    @contextmanager
    def workspace(self, prefix: str) -> Generator[Path]:
        """A private temporary directory, removed with everything in it afterwards."""
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            yield Path(temporary).resolve()

    @asynccontextmanager
    async def bounded(self, work: Path) -> AsyncGenerator[None]:
        """Account tool processes and temporary bytes to ``work`` within the job deadline."""
        async with self.runner.job_scope(work, max_temp_bytes=self.settings.max_temporary_bytes):
            try:
                async with asyncio.timeout(self.settings.job_timeout_seconds) as deadline:
                    yield
            except TimeoutError as error:
                if deadline.expired():
                    raise JobDeadlineExceeded from error
                raise

    def check_deadline(self) -> None:
        """Stop a synchronous step once the job is cancelled or its deadline has passed."""
        if self._task is not None and self._task.cancelling():
            raise asyncio.CancelledError
        if time.monotonic() >= self._deadline:
            raise JobDeadlineExceeded

    def statistics(self) -> dict[str, object]:
        """Reserved and observed resources and the elapsed time, for ``Report.execution``."""
        return {
            "resources": self.budget.statistics(),
            "observed": asdict(self.runner.statistics()),
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
        }
