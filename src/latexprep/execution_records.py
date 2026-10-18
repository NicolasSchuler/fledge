"""Typed reads of the ``Report.execution`` entries that renderers use.

``Report.execution`` is a free-form record that jobs fill in and that JSON carries as it
is. The few entries a renderer shows are read here, once, into typed records, so a
renderer never re-checks dictionary shapes. A malformed or missing entry reads as absent
rather than failing the report. This module does not produce the entries; the job does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The ``area`` of the entry that counts every opt-in check not named individually.
OTHER_OPT_IN_AREA = "other_opt_in_checks"


@dataclass(frozen=True, slots=True)
class CheckSelectionRecord:
    """The ``--select`` and ``--ignore`` selectors a run used."""

    select: tuple[str, ...]
    ignore: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotCheckedArea:
    """Opt-in checks a run did not perform because its settings left them off.

    ``codes`` are the check codes and ``settings`` the setting names that would
    activate them. The area ``OTHER_OPT_IN_AREA`` stands for all remaining opt-in checks.
    """

    area: str
    label: str
    codes: tuple[str, ...]
    settings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionRecords:
    """The execution entries a renderer shows, read from ``Report.execution``."""

    config_path: str | None = None
    preset: str | None = None
    check_selection: CheckSelectionRecord | None = None
    not_checked: tuple[NotCheckedArea, ...] = ()


def _strings(value: object) -> tuple[str, ...] | None:
    """The items of a list as strings, or None when ``value`` is not a list."""
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(map(str, value))


def read_check_selection(value: object) -> CheckSelectionRecord | None:
    """The selectors in an ``execution.check_selection`` entry, or None if it is malformed."""
    if not isinstance(value, Mapping):
        return None
    select = _strings(value.get("select", ["ALL"]))
    ignore = _strings(value.get("ignore", []))
    if select is None or ignore is None:
        return None
    return CheckSelectionRecord(select, ignore)


def read_not_checked(entries: object) -> tuple[NotCheckedArea, ...]:
    """The areas in an ``execution.not_checked`` entry; items without a label are skipped."""
    if not isinstance(entries, (list, tuple)):
        return ()
    areas = []
    for item in entries:
        if not isinstance(item, Mapping) or "label" not in item:
            continue
        areas.append(
            NotCheckedArea(
                area=str(item.get("area", "")),
                label=str(item["label"]),
                codes=_strings(item.get("codes")) or (),
                settings=_strings(item.get("settings")) or (),
            )
        )
    return tuple(areas)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def read_execution_records(execution: Mapping[str, object]) -> ExecutionRecords:
    """Read the entries renderers show from ``Report.execution``."""
    return ExecutionRecords(
        config_path=_text(execution.get("config_path")),
        preset=_text(execution.get("preset")),
        check_selection=read_check_selection(execution.get("check_selection")),
        not_checked=read_not_checked(execution.get("not_checked")),
    )
