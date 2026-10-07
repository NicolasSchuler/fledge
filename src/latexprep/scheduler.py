"""Dependency scheduling and shared admission for every expensive job operation."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from .models import PreparationError

T = TypeVar("T")
R = TypeVar("R")
_thread_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "latexprep_thread_stop", default=None
)


async def cancel_and_drain(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel once, then retain ownership despite repeated caller cancellation."""
    children = list(tasks)
    for child in children:
        if not child.done() and not child.cancelling():
            child.cancel()
    completion = asyncio.gather(*children, return_exceptions=True)
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            continue
    completion.result()


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


def cancellation_point() -> None:
    """Called at bounded parser/file boundaries by a cooperatively owned worker."""
    stop = _thread_stop.get()
    if stop is not None and stop.is_set():
        raise PreparationError("Analysis cancelled")


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
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
    if error is not None:
        raise error
    return cast(R, value)


@dataclass
class Lease:
    name: str
    cpu: int
    memory_mb: int
    kind: str | None
    owner: asyncio.Task[Any] | None
    queue_seconds: float
    active: bool = True


class ResourceBudget:
    """Atomic CPU, memory and tool-class reservations shared by phases and adapters.

    Reservations are conservative estimates. Runtime observes aggregate RSS and
    temporary bytes separately; this class does not claim an OS-hard memory cap.
    """

    def __init__(
        self,
        jobs: int,
        memory_mb: int,
        *,
        build_jobs: int = 1,
        render_jobs: int = 1,
        parent_memory_mb: int = 0,
    ) -> None:
        for name, value in (
            ("jobs", jobs),
            ("memory_mb", memory_mb),
            ("build_jobs", build_jobs),
            ("render_jobs", render_jobs),
        ):
            if type(value) is not int or value < 1:
                raise PreparationError(f"{name} must be a positive integer")
        if type(parent_memory_mb) is not int or not 0 <= parent_memory_mb < memory_mb:
            raise PreparationError("Parent memory reservation must fit the job budget")
        self.jobs, self.memory_mb = jobs, memory_mb
        self.build_jobs, self.render_jobs = build_jobs, render_jobs
        self.parent_memory_mb = parent_memory_mb
        self.available_memory_mb = memory_mb - parent_memory_mb
        self._condition = asyncio.Condition()
        self._queue: list[object] = []
        self._used_cpu = self._used_memory = 0
        self._used_kinds = {"build": 0, "render": 0}
        self._peak_cpu = self._peak_memory = 0
        self._peak_kinds = {"build": 0, "render": 0}
        self._operations: list[dict[str, object]] = []
        self._active: contextvars.ContextVar[Lease | None] = contextvars.ContextVar(
            "latexprep_resource_lease", default=None
        )

    @property
    def current_lease(self) -> Lease | None:
        lease = self._active.get()
        return lease if lease and lease.active and lease.owner is asyncio.current_task() else None

    def validate(self, *, cpu: int, memory_mb: int, kind: str | None = None) -> None:
        if type(cpu) is not int or type(memory_mb) is not int:
            raise PreparationError("Resource reservations must be integers")
        if not (1 <= cpu <= self.jobs and 1 <= memory_mb <= self.available_memory_mb):
            raise PreparationError(
                f"Operation requires {cpu} CPU units and {memory_mb} MiB; "
                f"available capacity is {self.jobs} CPU units and {self.available_memory_mb} MiB"
            )
        if kind not in {None, "build", "render"}:
            raise PreparationError(f"Unknown resource class: {kind}")

    def _fits(self, cpu: int, memory_mb: int, kind: str | None) -> bool:
        return (
            self._used_cpu + cpu <= self.jobs
            and self._used_memory + memory_mb <= self.available_memory_mb
            and (kind is None or self._used_kinds[kind] < getattr(self, f"{kind}_jobs"))
        )

    @asynccontextmanager
    async def lease(self, name: str, *, cpu: int = 1, memory_mb: int = 64, kind: str | None = None):
        self.validate(cpu=cpu, memory_mb=memory_mb, kind=kind)
        inherited = self._active.get()
        if inherited is not None and inherited.active:
            raise PreparationError("Resource-owning operations must not acquire nested leases")
        ticket = object()
        queued = time.monotonic()
        async with self._condition:
            self._queue.append(ticket)
            try:
                await self._condition.wait_for(
                    lambda: self._queue[0] is ticket and self._fits(cpu, memory_mb, kind)
                )
                self._queue.pop(0)
                self._used_cpu += cpu
                self._used_memory += memory_mb
                if kind:
                    self._used_kinds[kind] += 1
                self._peak_cpu = max(self._peak_cpu, self._used_cpu)
                self._peak_memory = max(self._peak_memory, self._used_memory)
                for key in self._used_kinds:
                    self._peak_kinds[key] = max(self._peak_kinds[key], self._used_kinds[key])
                self._condition.notify_all()
            except BaseException:
                self._queue.remove(ticket)
                self._condition.notify_all()
                raise
        started = time.monotonic()
        lease = Lease(name, cpu, memory_mb, kind, asyncio.current_task(), started - queued)
        token = self._active.set(lease)
        try:
            yield lease
        finally:
            # All updates happen without suspension: repeated cancellation cannot leak a lease.
            lease.active = False
            self._active.reset(token)
            self._used_cpu -= cpu
            self._used_memory -= memory_mb
            if kind:
                self._used_kinds[kind] -= 1
            self._operations.append(
                {
                    "name": name,
                    "cpu": cpu,
                    "memory_mb": memory_mb,
                    "kind": kind,
                    "queue_seconds": round(lease.queue_seconds, 6),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                }
            )

            # Condition notification needs its lock, but cleanup must not be interruptible.
            async def notify() -> None:
                async with self._condition:
                    self._condition.notify_all()

            notification = asyncio.create_task(notify())
            interrupted = False
            while not notification.done():
                try:
                    await asyncio.shield(notification)
                except asyncio.CancelledError:
                    interrupted = True
            notification.result()
            if interrupted:
                raise asyncio.CancelledError

    def statistics(self) -> dict[str, object]:
        return {
            "jobs": self.jobs,
            "memory_mb": self.memory_mb,
            "parent_reserved_memory_mb": self.parent_memory_mb,
            "build_jobs": self.build_jobs,
            "render_jobs": self.render_jobs,
            "peak_reserved_cpu": self._peak_cpu,
            "peak_reserved_memory_mb": self._peak_memory + self.parent_memory_mb,
            "peak_builds": self._peak_kinds["build"],
            "peak_renders": self._peak_kinds["render"],
            "active_cpu": self._used_cpu,
            "active_memory_mb": self._used_memory,
            "queued": len(self._queue),
            "reservation_scope": "Estimates; sampled RSS is reported separately by the runtime.",
            "operations": sorted(self._operations, key=lambda item: str(item["name"])),
        }


@dataclass(frozen=True)
class Task:
    name: str
    run: Callable[[], Awaitable[Any]]
    requires: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    cpu: int = 1
    memory_mb: int = 64
    kind: str | None = None
    is_success: Callable[[Any], bool] | None = None


@dataclass
class TaskResult:
    name: str
    status: str
    value: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    queue_seconds: float = 0.0


@dataclass
class Scheduler:
    jobs: int
    memory_mb: int
    progress: Callable[[str], None] = field(default=lambda _message: None)
    on_result: Callable[[TaskResult], None] = field(default=lambda _result: None)
    budget: ResourceBudget | None = None

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = ResourceBudget(self.jobs, self.memory_mb)

    def _validate(self, tasks: list[Task]) -> dict[str, Task]:
        assert self.budget is not None
        by_name = {task.name: task for task in tasks}
        if len(by_name) != len(tasks):
            raise PreparationError("Duplicate scheduler task name")
        for task in tasks:
            if type(task.cpu) is not int or type(task.memory_mb) is not int:
                raise PreparationError("Resource reservations must be integers")
            if (task.cpu, task.memory_mb, task.kind) != (0, 0, None):
                self.budget.validate(cpu=task.cpu, memory_mb=task.memory_mb, kind=task.kind)
            for dependency in (*task.requires, *task.after):
                if dependency not in by_name:
                    raise PreparationError(f"Task {task.name} has unknown dependency {dependency}")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(name: str) -> None:
            if name in active:
                raise PreparationError(f"Cyclic task dependency at {name}")
            if name in visited:
                return
            active.add(name)
            for dependency in (*by_name[name].requires, *by_name[name].after):
                visit(dependency)
            active.remove(name)
            visited.add(name)

        for name in by_name:
            visit(name)
        return by_name

    async def execute(self, tasks: list[Task]) -> dict[str, TaskResult]:
        pending = self._validate(tasks)
        assert self.budget is not None
        budget = self.budget
        results: dict[str, TaskResult] = {}
        running: dict[asyncio.Task[TaskResult], Task] = {}

        def record(result: TaskResult) -> None:
            if result.name not in results:
                results[result.name] = result
                self.on_result(result)

        async def execute_one(task: Task) -> TaskResult:
            start = time.monotonic()
            queue_seconds = 0.0

            async def run() -> TaskResult:
                nonlocal start
                start = time.monotonic()
                self.progress(f"Starting {task.name}")
                value = await task.run()
                succeeded = task.is_success is None or task.is_success(value)
                return TaskResult(
                    task.name,
                    "succeeded" if succeeded else "failed",
                    value=value,
                    error=None if succeeded else "Task did not produce a successful artifact",
                )

            try:
                if task.cpu == 0:
                    result = await run()
                else:
                    async with budget.lease(
                        task.name, cpu=task.cpu, memory_mb=task.memory_mb, kind=task.kind
                    ) as lease:
                        queue_seconds = lease.queue_seconds
                        result = await run()
            except asyncio.CancelledError:
                self.progress(f"Cancelled {task.name}")
                raise
            except Exception as error:
                # Keep the exception type: a bare message such as an empty string
                # or a lone path says nothing about what went wrong.
                result = TaskResult(task.name, "failed", error=f"{type(error).__name__}: {error}")
            result.elapsed_seconds = round(time.monotonic() - start, 6)
            result.queue_seconds = round(queue_seconds, 6)
            self.progress(f"{task.name}: {result.status} ({result.elapsed_seconds:.1f}s)")
            return result

        try:
            while pending or running:
                for name, task in list(pending.items()):
                    if not all(dep in results for dep in (*task.requires, *task.after)):
                        continue
                    failed = [dep for dep in task.requires if results[dep].status != "succeeded"]
                    del pending[name]
                    if failed:
                        record(
                            TaskResult(
                                name, "blocked", error=f"Prerequisite failed: {', '.join(failed)}"
                            )
                        )
                    else:
                        running[asyncio.create_task(execute_one(task))] = task
                if running:
                    completed, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for future in sorted(completed, key=lambda item: running[item].name):
                        result = future.result()
                        running.pop(future)
                        record(result)
                elif pending and not any(
                    all(dep in results for dep in (*task.requires, *task.after))
                    for task in pending.values()
                ):
                    raise PreparationError("No schedulable task remains")
        except BaseException:
            await cancel_and_drain(running)
            for future, task in running.items():
                if future.cancelled():
                    record(
                        TaskResult(
                            task.name, "cancelled", error="Cancelled while queued or running"
                        )
                    )
                elif future.exception() is None:
                    record(future.result())
                else:
                    failure = future.exception()
                    record(
                        TaskResult(
                            task.name, "failed", error=f"{type(failure).__name__}: {failure}"
                        )
                    )
            for name in pending:
                record(
                    TaskResult(name, "cancelled", error="Cancelled before prerequisites completed")
                )
            raise
        return {name: results[name] for name in sorted(results)}
