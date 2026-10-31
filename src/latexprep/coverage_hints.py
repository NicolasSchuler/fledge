"""Opt-in checks that a run did not perform because its settings left them off.

The job records the areas in ``execution.not_checked`` (``not_checked_areas`` builds
them, and ``execution_records`` writes and reads them); the terminal report names them
in a one-line hint (``not_checked_summary``) with where to enable them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from .check_policy import opt_in_activation
from .config import toml_table
from .documentation_links import CONFIGURATION_GUIDE
from .execution_records import OTHER_OPT_IN_AREA, NotCheckedArea
from .presets import preset_names
from .rules import RULES

# Opt-in areas named individually when they are not configured; every other
# unconfigured opt-in check is counted together. An area's checks are the rules that
# name it as their ``not_checked_area``, and their settings come from the check policy's
# activation tables, so a renamed setting cannot go stale here.
_AREA_LABELS = {
    "anonymity": "anonymity",
    "page_limit": "page limit",
    "archive_size": "ZIP size limit",
    "forbidden_packages": "forbidden packages",
    "font_embedding": "strict font embedding",
    "image_resolution": "image resolution",
}


def not_checked_areas(configured_codes: Collection[str]) -> list[NotCheckedArea]:
    """List the opt-in checks outside ``configured_codes``, the codes a run's settings activate.

    The first entries name the major areas; the final ``other_opt_in_checks`` entry
    lists the remaining unconfigured opt-in codes. A check disabled by check
    selection is reported here too, because it did not run either.

    Args:
        configured_codes: The codes the run's settings activate; pass
            ``check_policy.configured_opt_in_codes`` of the effective settings.

    Returns:
        One entry for each major area none of whose checks is configured, then at most one
        entry for the other unconfigured opt-in checks, each with the settings that enable
        its checks.
    """
    activation = opt_in_activation()

    def setting_names(codes: Sequence[str]) -> tuple[str, ...]:
        """Name the settings that activate ``codes``, each once, as ``table.name`` or ``name``."""
        return tuple(
            dict.fromkeys(
                name if group is None else f"{toml_table(group)}.{name}"
                for code in codes
                for group, fields in activation[code]
                for name in fields
            )
        )

    entries: list[NotCheckedArea] = []
    named: set[str] = set()
    for area, label in _AREA_LABELS.items():
        codes = tuple(rule.code for rule in RULES if rule.not_checked_area == area)
        named.update(codes)
        if not any(code in configured_codes for code in codes):
            entries.append(NotCheckedArea(area, label, codes, setting_names(codes)))
    others = sorted(
        code for code in activation if code not in named and code not in configured_codes
    )
    if others:
        entries.append(
            NotCheckedArea(
                OTHER_OPT_IN_AREA, "other opt-in checks", tuple(others), setting_names(others)
            )
        )
    return entries


def not_checked_summary(
    areas: Sequence[NotCheckedArea], config_path: str | None = None
) -> str | None:
    """Write one line naming unconfigured opt-in areas and how to enable them.

    Args:
        areas: The unconfigured areas, as :func:`not_checked_areas` lists them.
        config_path: The settings file this run loaded, if any (the report's
            ``execution.config_path``). The ``fledge init`` hint is only offered when no
            settings file was used.

    Returns:
        The line, or ``None`` when ``areas`` names nothing.
    """
    labels = [area.label for area in areas if area.area != OTHER_OPT_IN_AREA]
    others = next((area.codes for area in areas if area.area == OTHER_OPT_IN_AREA), ())
    if others:
        labels.append(f"{len(others)} other opt-in check{'s' if len(others) != 1 else ''}")
    if not labels:
        return None
    where = (
        f"Add them to {config_path}"
        if config_path
        else f"Set them in fledge.toml (start with fledge init --preset {'|'.join(preset_names())})"
    )
    return (
        f"Not checked (needs your settings): {', '.join(labels)}. "
        f"{where}; see {CONFIGURATION_GUIDE}"
    )
