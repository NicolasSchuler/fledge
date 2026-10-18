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
from typing import Any, TypeVar, cast

from .errors import PreparationError

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
    """Called at bounded parser/file boundaries by a cooperatively owned worker."""
    stop = _thread_stop.get()
    if stop is not None and stop.is_set():
        raise AnalysisCancelled("Analysis cancelled")


async def complete_despite_cancellation(work: Awaitable[T]) -> T:
    """Await ``work`` to completion even when the caller is cancelled meanwhile.

    Cleanup such as reaping a child process or releasing a lease must finish before
    cancellation propagates. Each cancellation received while waiting is re-raised
    once ``work`` is done, and a result or error of ``work`` takes the usual path.
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
    """Cancel once, then retain ownership despite repeated caller cancellation.

    The caller is already unwinding, so a repeated cancellation is absorbed here
    and the exception being propagated stays the one the caller re-raises.
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
    """A bounded number of workers, ordered small results, no unbounded task queue."""
    if type(limit) is not int or limit < 1:
        raise PreparationError("Worker limit must be a positive integer")
    indexed = iter(enumerate(items))
    results: dict[int, R] = {}

    async def consume() -> None:
        for index, item in indexed:
            results[index] = await worker(item)

    tasks = [asyncio.create_task(consume()) for _ in range(min(limit, len(items)))]

    async def collect() -> BaseException | None:
        # Keep fail-fast gather semantics, but carry errors as data through the
        # shield. Python 3.14 logs late exceptions on a cancelled shield even
        # when the owner subsequently retrieves them while draining workers.
        try:
            await asyncio.gather(*tasks)
        except BaseException as error:
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


async def run_in_thread(function: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    """Do not release the snapshot/lease until a cancelled reader has stopped."""
    stop = threading.Event()

    def invoke() -> tuple[R | None, BaseException | None]:
        # Return worker errors as data until the owner can receive them. A
        # cancelled shield in newer Python versions logs a late exception even
        # when the owner subsequently drains and retrieves it.
        try:
            return function(*args, **kwargs), None
        except BaseException as error:
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
