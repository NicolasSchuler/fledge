"""How the platform lists processes, and which registered command each one belongs to.

The resource monitor asks ``ps`` for one row per process and needs to know how much
memory every tool command is using, descendants included. The two table classes name
the ``ps`` columns a platform needs and read the rows; a sandbox backend inherits the
one that fits its isolation. This module starts no process and reads no file.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

from .errors import PreparationError

# Columns of a ``ps`` row: pid, parent pid, process group and resident size.
_ANCESTRY_COLUMNS = 4


class ProcessSample(NamedTuple):
    """One process's resident memory, attributed to the process group it counts against."""

    pid: int
    group: int
    rss_kb: int


class ProcessTable(Protocol):
    """How the platform lists processes; the sandbox backend supplies it."""

    @property
    def process_columns(self) -> str:
        """The ``ps -o`` columns that ``parse_processes`` reads."""
        ...

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        """Attribute each listed process to the registered command group it belongs to."""
        ...


def parse_group_rows(output: bytes) -> list[ProcessSample]:
    """Read ``pid pgid rss`` rows; each command already leads its own process group."""
    samples = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != len(ProcessSample._fields):
            continue
        try:
            samples.append(ProcessSample(*(int(value) for value in fields)))
        except ValueError as error:
            raise PreparationError("Process accounting returned invalid values") from error
    if not samples:
        raise PreparationError("Process memory accounting returned no processes")
    return samples


def parse_ancestry_rows(output: bytes, registered: set[int]) -> list[ProcessSample]:
    """Read ``pid ppid pgid rss`` rows and follow parents, so Bubblewrap descendants count.

    Bubblewrap starts a new session for the tool it runs, which leaves the tool in a
    process group of its own; the parent chain still leads back to the registered
    command that started it.
    """
    records: dict[int, tuple[int, int, int]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != _ANCESTRY_COLUMNS:
            raise PreparationError("Linux process accounting returned an incomplete row")
        try:
            pid, parent, group, rss = (int(value) for value in fields)
        except ValueError as error:
            raise PreparationError("Linux process accounting returned invalid values") from error
        if pid < 1 or parent < 0 or group < 0 or rss < 0 or pid in records:
            raise PreparationError("Linux process accounting returned invalid process identities")
        records[pid] = (parent, group, rss)
    if not records:
        raise PreparationError("Process memory accounting returned no processes")
    samples = []
    for pid, (_, group, rss) in records.items():
        current = pid
        visited: set[int] = set()
        while current in records and current not in visited:
            if current in registered:
                group = current
                break
            visited.add(current)
            current = records[current][0]
        samples.append(ProcessSample(pid, group, rss))
    return samples


class GroupProcessTable:
    """Process groups as listed: each registered command leads its own group."""

    process_columns = "pid=,pgid=,rss="

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        return parse_group_rows(output)


class AncestryProcessTable:
    """Process groups found through parent links, for a sandbox that splits sessions."""

    process_columns = "pid=,ppid=,pgid=,rss="

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        return parse_ancestry_rows(output, registered)
