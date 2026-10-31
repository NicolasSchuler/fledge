"""Cooperative cancellation: owned workers that finish before a cancellation propagates.

A cancelled caller must not abandon a thread, a child task or a cleanup step it
still owns, so each helper here keeps ownership until the work has stopped and then
lets the cancellation continue. Modules that parse files import this module, never
the scheduler, to reach ``cancellation_point``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, ParamSpec, TypeVar, cast

from .errors import PreparationError

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")
_thread_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "latexprep_thread_stop", default=None
)


class AnalysisCancelled(BaseException):
    """A cooperatively owned worker stopped because its owner was cancelled.

    Like ``asyncio.CancelledError`` this is not an ``Exception``: a checker's
    ``except PreparationError``/``except Exception`` must never turn a
    cancellation into an ordinary finding.
    """


def cancellation_point() -> None:
    """Stop a cooperatively owned worker whose owner was cancelled.

    A worker calls this at bounded parser and file boundaries. Outside
    :func:`run_in_thread` it does nothing.

    Raises:
        AnalysisCancelled: The caller of the :func:`run_in_thread` that runs this worker
            was cancelled.
    """
    stop = _thread_stop.get()
    if stop is not None and stop.is_set():
        raise AnalysisCancelled("Analysis cancelled")


async def complete_despite_cancellation(work: Awaitable[T]) -> T:
    """Await ``work`` to completion even when the caller is cancelled meanwhile.

    Cleanup such as reaping a child process or releasing a lease must finish before
    cancellation propagates. A result or error of ``work`` takes the usual path.

    Args:
        work: The awaitable to finish.

    Returns:
        The result of ``work``.

    Raises:
        asyncio.CancelledError: Each cancellation received while waiting is re-raised
            once ``work`` is done.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def cancel_and_drain(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel the tasks once, then retain ownership despite repeated caller cancellation.

    The caller is already unwinding, so a repeated cancellation is absorbed here
    and the exception being propagated stays the one the caller re-raises.

    Args:
        tasks: The tasks to cancel and wait for; a task that is done or already being
            cancelled is not cancelled again.
    """
    children = list(tasks)
    for child in children:
        if not child.done() and not child.cancelling():
            child.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await complete_despite_cancellation(asyncio.gather(*children, return_exceptions=True))


async def bounded_map(
    items: Sequence[T], worker: Callable[[T], Awaitable[R]], *, limit: int
) -> list[R]:
    """Apply ``worker`` to every item with a bounded number of concurrent workers.

    The results, meant to be small, are returned in the order of ``items``, and no
    unbounded task queue is created. The first error cancels the other workers and
    propagates.

    Args:
        items: The items to process.
        worker: The coroutine function applied to each item.
        limit: The largest number of workers that run at once.

    Returns:
        The result for each item, in the order of ``items``.

    Raises:
        PreparationError: ``limit`` is not a positive integer.
    """
    if type(limit) is not int or limit < 1:
        raise PreparationError("Worker limit must be a positive integer")
    indexed = iter(enumerate(items))
    results: dict[int, R] = {}

    async def consume() -> None:
        """Process items from the shared iterator until it is exhausted."""
        for index, item in indexed:
            results[index] = await worker(item)

    tasks = [asyncio.create_task(consume()) for _ in range(min(limit, len(items)))]

    async def collect() -> BaseException | None:
        """Wait for every worker; return the first error instead of raising it, or ``None``."""
        # Keep fail-fast gather semantics, but carry errors as data through the
        # shield. Python 3.14 logs late exceptions on a cancelled shield even
        # when the owner subsequently retrieves them while draining workers.
        try:
            await asyncio.gather(*tasks)
        except BaseException as error:  # noqa: BLE001 - carried as data and re-raised by the owner
            return error
        return None

    completion = asyncio.create_task(collect())
    try:
        error = await asyncio.shield(completion)
        if error is not None:
            raise error
    except BaseException:
        await cancel_and_drain(tasks)
        await cancel_and_drain([completion])
        raise
    return [results[index] for index in range(len(items))]


async def run_in_thread(function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``function`` in a worker thread that a cancelled caller still waits for.

    A snapshot or lease must not be released until a cancelled reader has stopped, so
    when the caller is cancelled, the :func:`cancellation_point` calls of the thread
    raise ``AnalysisCancelled``, and the cancellation propagates only once the thread
    has finished. An error of ``function`` is re-raised in the caller.

    Args:
        function: The function to run in the thread.
        *args: The positional arguments of ``function``.
        **kwargs: The keyword arguments of ``function``.

    Returns:
        The result of ``function``.

    Raises:
        asyncio.CancelledError: The caller was cancelled; raised once the thread has
            stopped.
    """
    stop = threading.Event()

    def invoke() -> tuple[R | None, BaseException | None]:
        """Call ``function`` and return its result or its error as data."""
        # Return worker errors as data until the owner can receive them. A
        # cancelled shield in newer Python versions logs a late exception even
        # when the owner subsequently drains and retrieves it.
        try:
            return function(*args, **kwargs), None
        except BaseException as error:  # noqa: BLE001 - carried as data and re-raised by the owner
            return None, error

    token = _thread_stop.set(stop)
    try:
        worker = asyncio.create_task(asyncio.to_thread(invoke))
    finally:
        _thread_stop.reset(token)
    try:
        value, error = await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        # Cancelling to_thread's future loses the underlying reader's lifetime.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await complete_despite_cancellation(worker)
        if not worker.cancelled():
            worker.exception()
        raise
    if error is not None:
        raise error
    return cast(R, value)
