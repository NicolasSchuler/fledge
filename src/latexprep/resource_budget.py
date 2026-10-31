"""Atomic CPU, memory and tool-class reservations shared by every expensive job operation."""

from __future__ import annotations

import asyncio
import contextvars
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, get_args

from .cancellation import complete_despite_cancellation
from .errors import PreparationError
from .limits import DEFAULT_LEASE_MEMORY_MB

# Expensive tool classes with their own concurrency limit inside the shared budget.
ResourceClass = Literal["build", "render"]
RESOURCE_CLASSES: tuple[ResourceClass, ...] = get_args(ResourceClass)


@dataclass
class Lease:
    """One granted reservation, active until its operation ends.

    Attributes:
        name: The operation's name.
        cpu: The reserved CPU units.
        memory_mb: The reserved memory in MiB.
        kind: The tool class whose concurrency limit the lease counts against, if any.
        owner: The task that holds the lease.
        queue_seconds: How long the operation waited for the reservation.
        active: Whether the reservation is still held.
    """

    name: str
    cpu: int
    memory_mb: int
    kind: ResourceClass | None
    owner: asyncio.Task[Any] | None
    queue_seconds: float
    active: bool = True


class ResourceBudget:
    """Atomic CPU, memory and tool-class reservations shared by phases and adapters.

    Reservations are conservative estimates. Runtime observes aggregate RSS and
    temporary bytes separately; this class does not claim an OS-hard memory cap.
    Leases are granted in the order they were requested. Construction raises
    ``PreparationError`` for a limit that is not a positive integer or a coordinator
    reservation that does not fit the memory budget.

    Attributes:
        cpu_slots: The ``jobs`` setting: CPU units shared by every operation.
        memory_mb: The memory budget in MiB.
        build_jobs: How many builds may hold a lease at once.
        render_jobs: How many renders may hold a lease at once.
        coordinator_memory_mb: The memory in MiB reserved for the parent process.
        available_memory_mb: The memory in MiB that leases share.
    """

    def __init__(
        self,
        cpu_slots: int,
        memory_mb: int,
        *,
        build_jobs: int = 1,
        render_jobs: int = 1,
        coordinator_memory_mb: int = 0,
    ) -> None:
        for name, value in (
            ("cpu_slots", cpu_slots),
            ("memory_mb", memory_mb),
            ("build_jobs", build_jobs),
            ("render_jobs", render_jobs),
        ):
            if type(value) is not int or value < 1:
                raise PreparationError(f"{name} must be a positive integer")
        if type(coordinator_memory_mb) is not int or not 0 <= coordinator_memory_mb < memory_mb:
            raise PreparationError("Coordinator memory reservation must fit the job budget")
        self.cpu_slots, self.memory_mb = cpu_slots, memory_mb
        self.build_jobs, self.render_jobs = build_jobs, render_jobs
        self.coordinator_memory_mb = coordinator_memory_mb
        self.available_memory_mb = memory_mb - coordinator_memory_mb
        self._class_limits: dict[ResourceClass, int] = {
            "build": build_jobs,
            "render": render_jobs,
        }
        self._condition = asyncio.Condition()
        self._queue: list[object] = []
        self._used_cpu = self._used_memory = 0
        self._used_classes: dict[ResourceClass, int] = dict.fromkeys(RESOURCE_CLASSES, 0)
        self._peak_cpu = self._peak_memory = 0
        self._peak_classes: dict[ResourceClass, int] = dict.fromkeys(RESOURCE_CLASSES, 0)
        self._operations: list[dict[str, object]] = []
        self._active: contextvars.ContextVar[Lease | None] = contextvars.ContextVar(
            "latexprep_resource_lease", default=None
        )

    @property
    def current_lease(self) -> Lease | None:
        """The active lease that the calling task holds, or ``None``."""
        lease = self._active.get()
        return lease if lease and lease.active and lease.owner is asyncio.current_task() else None

    def validate(self, *, cpu: int, memory_mb: int, kind: ResourceClass | None = None) -> None:
        """Check that a reservation is well formed and fits the whole budget on its own.

        Args:
            cpu: The CPU units to reserve.
            memory_mb: The memory to reserve in MiB.
            kind: The tool class, if any.

        Raises:
            PreparationError: A value is not an integer, the reservation is below one unit
                or exceeds the capacity, or ``kind`` is unknown.
        """
        if type(cpu) is not int or type(memory_mb) is not int:
            raise PreparationError("Resource reservations must be integers")
        if not (1 <= cpu <= self.cpu_slots and 1 <= memory_mb <= self.available_memory_mb):
            raise PreparationError(
                f"Operation requires {cpu} CPU units and {memory_mb} MiB; "
                f"available capacity is {self.cpu_slots} CPU units "
                f"and {self.available_memory_mb} MiB"
            )
        if kind is not None and kind not in RESOURCE_CLASSES:
            raise PreparationError(f"Unknown resource class: {kind}")

    def _fits(self, cpu: int, memory_mb: int, kind: ResourceClass | None) -> bool:
        """Return whether a reservation fits beside the held ones and within its class limit."""
        return (
            self._used_cpu + cpu <= self.cpu_slots
            and self._used_memory + memory_mb <= self.available_memory_mb
            and (kind is None or self._used_classes[kind] < self._class_limits[kind])
        )

    @asynccontextmanager
    async def lease(
        self,
        name: str,
        *,
        cpu: int = 1,
        memory_mb: int = DEFAULT_LEASE_MEMORY_MB,
        kind: ResourceClass | None = None,
    ):
        """Hold a reservation for the duration of the ``async with`` block.

        The caller waits until every earlier request is granted and the reservation fits.
        Leaving the block releases the reservation without suspension, so repeated
        cancellation cannot leak it, and records the operation for :meth:`statistics`.

        Args:
            name: The operation's name in the statistics.
            cpu: The CPU units to reserve.
            memory_mb: The memory to reserve in MiB.
            kind: The tool class whose concurrency limit applies, if any.

        Yields:
            The granted lease.

        Raises:
            PreparationError: The reservation is invalid (see :meth:`validate`), or the
                caller already holds an active lease.
        """
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
                    self._used_classes[kind] += 1
                self._peak_cpu = max(self._peak_cpu, self._used_cpu)
                self._peak_memory = max(self._peak_memory, self._used_memory)
                for key, used in self._used_classes.items():
                    self._peak_classes[key] = max(self._peak_classes[key], used)
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
                self._used_classes[kind] -= 1
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
                """Wake every waiting request under the condition's lock."""
                async with self._condition:
                    self._condition.notify_all()

            await complete_despite_cancellation(notify())

    def statistics(self) -> dict[str, object]:
        """Summarize the budget, its peak and current use, and every finished operation.

        Returns:
            Plain data for the report, with the operations sorted by name.
        """
        # Report keys name the settings: ``jobs`` is the CPU slot count.
        return {
            "jobs": self.cpu_slots,
            "memory_mb": self.memory_mb,
            "parent_reserved_memory_mb": self.coordinator_memory_mb,
            "build_jobs": self.build_jobs,
            "render_jobs": self.render_jobs,
            "peak_reserved_cpu": self._peak_cpu,
            "peak_reserved_memory_mb": self._peak_memory + self.coordinator_memory_mb,
            "peak_builds": self._peak_classes["build"],
            "peak_renders": self._peak_classes["render"],
            "active_cpu": self._used_cpu,
            "active_memory_mb": self._used_memory,
            "queued": len(self._queue),
            "reservation_scope": "Estimates; sampled RSS is reported separately by the runtime.",
            "operations": sorted(self._operations, key=lambda item: str(item["name"])),
        }
