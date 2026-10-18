"""Dependency scheduling of job tasks under the shared resource budget."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import KW_ONLY, dataclass, field
from enum import StrEnum
from typing import Any, Self

from .cancellation import cancel_and_drain
from .defects import is_expected_failure, summarize_traceback
from .errors import PreparationError
from .limits import DEFAULT_LEASE_MEMORY_MB
from .resource_budget import ResourceBudget, ResourceClass


@dataclass(frozen=True, slots=True)
class Task:
    """One scheduled operation with its prerequisites and resource reservation.

    A composite task (see ``composite``) composes other leased operations and holds
    no reservation of its own.
    """

    name: str
    run: Callable[[], Awaitable[Any]]
    _: KW_ONLY
    # Start only after each of these tasks succeeded; otherwise the task is blocked.
    needs_success_of: tuple[str, ...] = ()
    # Start only after each of these tasks finished, whatever its result.
    waits_for: tuple[str, ...] = ()
    cpu: int = 1
    memory_mb: int = DEFAULT_LEASE_MEMORY_MB
    kind: ResourceClass | None = None
    is_success: Callable[[Any], bool] | None = None

    def __post_init__(self) -> None:
        # Names may be StrEnum members; results and reports carry plain strings.
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "needs_success_of", tuple(map(str, self.needs_success_of)))
        object.__setattr__(self, "waits_for", tuple(map(str, self.waits_for)))

    @classmethod
    def composite(
        cls,
        name: str,
        run: Callable[[], Awaitable[Any]],
        *,
        needs_success_of: tuple[str, ...] = (),
        waits_for: tuple[str, ...] = (),
        is_success: Callable[[Any], bool] | None = None,
    ) -> Self:
        """A task whose operations lease their own resources, so it reserves none itself."""
        return cls(
            name,
            run,
            needs_success_of=needs_success_of,
            waits_for=waits_for,
            cpu=0,
            memory_mb=0,
            is_success=is_success,
        )

    @property
    def is_composite(self) -> bool:
        """Whether the task holds no reservation and is never admitted by the budget."""
        return (self.cpu, self.memory_mb, self.kind) == (0, 0, None)

    @property
    def dependencies(self) -> tuple[str, ...]:
        return (*self.needs_success_of, *self.waits_for)


class TaskStatus(StrEnum):
    """How a scheduled task ended; serialized as its string value in stage records."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class TaskResult:
    name: str
    status: TaskStatus
    value: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    queue_seconds: float = 0.0
    # Names of failed prerequisites for a ``blocked`` result, so reports can
    # group the tasks that one root failure prevented without parsing ``error``.
    blocked_by: tuple[str, ...] = ()
    # Path-free traceback frames of a task that failed by a defect rather than by an
    # expected ``PreparationError`` or ``OSError``; empty for every other result.
    defect_frames: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.SUCCEEDED


@dataclass
class Scheduler:
    budget: ResourceBudget
    progress: Callable[[str], None] = field(default=lambda _message: None)
    on_result: Callable[[TaskResult], None] = field(default=lambda _result: None)

    def _validate(self, tasks: list[Task]) -> dict[str, Task]:
        by_name = {task.name: task for task in tasks}
        if len(by_name) != len(tasks):
            raise PreparationError("Duplicate scheduler task name")
        for task in tasks:
            if type(task.cpu) is not int or type(task.memory_mb) is not int:
                raise PreparationError("Resource reservations must be integers")
            if not task.is_composite:
                self.budget.validate(cpu=task.cpu, memory_mb=task.memory_mb, kind=task.kind)
            for dependency in task.dependencies:
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
            for dependency in by_name[name].dependencies:
                visit(dependency)
            active.remove(name)
            visited.add(name)

        for name in by_name:
            visit(name)
        return by_name

    async def _invoke(self, task: Task) -> TaskResult:
        self.progress(f"Starting {task.name}")
        value = await task.run()
        succeeded = task.is_success is None or task.is_success(value)
        return TaskResult(
            task.name,
            TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED,
            value=value,
            error=None if succeeded else "Task did not produce a successful artifact",
        )

    async def _run_task(self, task: Task) -> TaskResult:
        """Run one task under its reservation; an ordinary error becomes a failed result."""
        started = time.monotonic()
        queue_seconds = 0.0
        try:
            if task.is_composite:
                started = time.monotonic()
                result = await self._invoke(task)
            else:
                async with self.budget.lease(
                    task.name, cpu=task.cpu, memory_mb=task.memory_mb, kind=task.kind
                ) as lease:
                    queue_seconds = lease.queue_seconds
                    started = time.monotonic()
                    result = await self._invoke(task)
        except asyncio.CancelledError:
            self.progress(f"Cancelled {task.name}")
            raise
        except Exception as error:
            # Keep the exception type: a bare message such as an empty string
            # or a lone path says nothing about what went wrong.
            result = TaskResult(
                task.name,
                TaskStatus.FAILED,
                error=f"{type(error).__name__}: {error}",
                defect_frames=() if is_expected_failure(error) else summarize_traceback(error),
            )
        result.elapsed_seconds = round(time.monotonic() - started, 6)
        result.queue_seconds = round(queue_seconds, 6)
        self.progress(f"{task.name}: {result.status} ({result.elapsed_seconds:.1f}s)")
        return result

    async def execute(self, tasks: list[Task]) -> dict[str, TaskResult]:
        pending = self._validate(tasks)
        results: dict[str, TaskResult] = {}
        running: dict[asyncio.Task[TaskResult], Task] = {}

        def record(result: TaskResult) -> None:
            if result.name not in results:
                results[result.name] = result
                self.on_result(result)

        def ready(task: Task) -> bool:
            return all(dependency in results for dependency in task.dependencies)

        try:
            while pending or running:
                for name, task in list(pending.items()):
                    if not ready(task):
                        continue
                    failed = [
                        dependency
                        for dependency in task.needs_success_of
                        if not results[dependency].succeeded
                    ]
                    del pending[name]
                    if failed:
                        record(
                            TaskResult(
                                name,
                                TaskStatus.BLOCKED,
                                error=f"Prerequisite failed: {', '.join(failed)}",
                                blocked_by=tuple(failed),
                            )
                        )
                    else:
                        running[asyncio.create_task(self._run_task(task))] = task
                if running:
                    completed, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for future in sorted(completed, key=lambda item: running[item].name):
                        result = future.result()
                        running.pop(future)
                        record(result)
                elif pending and not any(ready(task) for task in pending.values()):
                    raise PreparationError("No schedulable task remains")
        except BaseException:
            await cancel_and_drain(running)
            for future, task in running.items():
                if future.cancelled():
                    record(
                        TaskResult(
                            task.name,
                            TaskStatus.CANCELLED,
                            error="Cancelled while queued or running",
                        )
                    )
                elif future.exception() is None:
                    record(future.result())
                else:
                    failure = future.exception()
                    record(
                        TaskResult(
                            task.name,
                            TaskStatus.FAILED,
                            error=f"{type(failure).__name__}: {failure}",
                        )
                    )
            for name in pending:
                record(
                    TaskResult(
                        name, TaskStatus.CANCELLED, error="Cancelled before prerequisites completed"
                    )
                )
            raise
        return {name: results[name] for name in sorted(results)}
