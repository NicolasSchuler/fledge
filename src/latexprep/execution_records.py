"""Typed records of the ``Report.execution`` entries that jobs write and renderers show.

``Report.execution`` is a free-form record that jobs fill in and that JSON carries as it
is. The few entries a renderer shows have one definition here: the typed records, the
``record_*`` functions that a job writes them with, and the readers that give them back,
so a renderer never re-checks dictionary shapes. A malformed or missing entry reads as
absent rather than failing the report. Entries that no renderer reads, such as resource
statistics, are not described here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The ``area`` of the entry that counts every opt-in check not named individually.
OTHER_OPT_IN_AREA = "other_opt_in_checks"

# The keys of the entries in ``Report.execution``, shared by the writers and the readers.
CONFIG_PATH_KEY = "config_path"
PRESET_KEY = "preset"
CHECK_SELECTION_KEY = "check_selection"
NOT_CHECKED_KEY = "not_checked"
PDF_PREVIEWS_KEY = "pdf_previews"


@dataclass(frozen=True, slots=True)
class CheckSelectionRecord:
    """The ``--select`` and ``--ignore`` selectors a run used.

    ``always_enforced`` lists the codes of the guards that selection cannot switch off.
    """

    select: tuple[str, ...]
    ignore: tuple[str, ...]
    always_enforced: tuple[str, ...] = ()

    def to_report(self) -> dict[str, object]:
        """The entry as ``Report.execution`` carries it."""
        return {
            "select": list(self.select),
            "ignore": list(self.ignore),
            "always_enforced_when_applicable": list(self.always_enforced),
        }


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

    def to_report(self) -> dict[str, object]:
        """The entry as ``Report.execution`` carries it."""
        return {
            "area": self.area,
            "label": self.label,
            "codes": list(self.codes),
            "settings": list(self.settings),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRecords:
    """The execution entries a renderer shows, read from ``Report.execution``."""

    config_path: str | None = None
    preset: str | None = None
    check_selection: CheckSelectionRecord | None = None
    not_checked: tuple[NotCheckedArea, ...] = ()


def record_settings_source(
    execution: dict[str, object], *, config_path: str | None, preset: str | None
) -> None:
    """Record which settings file and preset a run used; ``None`` means it used none."""
    execution[CONFIG_PATH_KEY] = config_path
    execution[PRESET_KEY] = preset


def record_check_selection(execution: dict[str, object], selection: CheckSelectionRecord) -> None:
    """Record the check selectors a run used."""
    execution[CHECK_SELECTION_KEY] = selection.to_report()


def record_not_checked(execution: dict[str, object], areas: Sequence[NotCheckedArea]) -> None:
    """Record the opt-in areas a run left unchecked, which may be none."""
    execution[NOT_CHECKED_KEY] = [area.to_report() for area in areas]


def record_pdf_previews(execution: dict[str, object], summary: Mapping[str, object]) -> None:
    """Record the summary of the page-preview comparisons that ``pdf_previews`` collected."""
    execution[PDF_PREVIEWS_KEY] = dict(summary)


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
    always_enforced = _strings(value.get("always_enforced_when_applicable")) or ()
    return CheckSelectionRecord(select, ignore, always_enforced)


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
        config_path=_text(execution.get(CONFIG_PATH_KEY)),
        preset=_text(execution.get(PRESET_KEY)),
        check_selection=read_check_selection(execution.get(CHECK_SELECTION_KEY)),
        not_checked=read_not_checked(execution.get(NOT_CHECKED_KEY)),
    )
