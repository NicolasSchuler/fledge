"""Starting and stopping child processes so that none outlives a cancelled launcher."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from typing import Any

from .cancellation import complete_despite_cancellation


def kill_process_group(pid: int) -> None:
    # macOS reports EPERM instead of ESRCH for a group whose leader is already a
    # zombie being reaped; either way no signalable member of our group remains.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def kill_unfinished(process: asyncio.subprocess.Process) -> None:
    """Kill the process itself, but not its group, unless it has already exited."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def start_process(
    *argv: str, stop: Callable[[asyncio.subprocess.Process], None], **options: Any
) -> asyncio.subprocess.Process:
    """Start a subprocess; a launch cancelled midway is stopped and reaped first."""
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **options))
    try:
        return await asyncio.shield(launch)
    except asyncio.CancelledError:
        await complete_despite_cancellation(_stop_launched(launch, stop))
        raise


async def _stop_launched(
    launch: asyncio.Task[asyncio.subprocess.Process],
    stop: Callable[[asyncio.subprocess.Process], None],
) -> None:
    try:
        process = await launch
    except (OSError, ValueError):
        return
    stop(process)
    await process.communicate()
