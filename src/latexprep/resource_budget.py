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

# Expensive tool classes with their own concurrency limit inside the shared budget.
ResourceClass = Literal["build", "render"]
RESOURCE_CLASSES: tuple[ResourceClass, ...] = get_args(ResourceClass)


@dataclass
class Lease:
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
    ``cpu_slots`` is the ``jobs`` setting: CPU units shared by every operation.
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
        lease = self._active.get()
        return lease if lease and lease.active and lease.owner is asyncio.current_task() else None

    def validate(self, *, cpu: int, memory_mb: int, kind: ResourceClass | None = None) -> None:
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
        memory_mb: int = 64,
        kind: ResourceClass | None = None,
    ):
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
                async with self._condition:
                    self._condition.notify_all()

            await complete_despite_cancellation(notify())

    def statistics(self) -> dict[str, object]:
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
