"""Opt-in checks that a run did not perform because its settings left them off.

The job records the areas in ``execution.not_checked``; the terminal report
names them in a one-line hint with where to enable them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from .check_policy import opt_in_activation

CONFIGURATION_GUIDE = "https://nicolasschuler.github.io/fledge/configuration.html"
PRESET_COMMAND = "fledge init --preset arxiv|anonymous-review|camera-ready"
# Opt-in areas named individually when they are not configured; every other
# unconfigured opt-in check is counted together. Codes and settings come from
# the check policy's activation tables, so a renamed setting cannot go stale here.
_NOT_CHECKED_AREAS = (
    ("anonymity", "anonymity", ("PRV001", "PRV002")),
    ("page_limit", "page limit", ("PDF001",)),
    ("archive_size", "ZIP size limit", ("PKG105",)),
    ("forbidden_packages", "forbidden packages", ("TEX103", "BLD102")),
    ("font_embedding", "strict font embedding", ("PDF104", "PDF105")),
    ("image_resolution", "image resolution", ("PDF103",)),
)


def not_checked_areas(configured_codes: Collection[str]) -> list[dict[str, object]]:
    """Opt-in checks outside ``configured_codes``, the codes a run's settings activate.

    Pass ``check_policy.configured_opt_in_codes`` of the effective settings. The
    first entries name the major areas; the final ``other_opt_in_checks`` entry
    lists the remaining unconfigured opt-in codes. A check disabled by check
    selection is reported here too, because it did not run either.
    """
    activation = opt_in_activation()

    def setting_names(codes: Sequence[str]) -> list[str]:
        return list(
            dict.fromkeys(
                name if group is None else f"{group}.{name}"
                for code in codes
                for group, fields in activation[code]
                for name in fields
            )
        )

    entries: list[dict[str, object]] = []
    named: set[str] = set()
    for area, label, codes in _NOT_CHECKED_AREAS:
        named.update(codes)
        if not any(code in configured_codes for code in codes):
            entries.append(
                {
                    "area": area,
                    "label": label,
                    "codes": list(codes),
                    "settings": setting_names(codes),
                }
            )
    others = sorted(
        code for code in activation if code not in named and code not in configured_codes
    )
    if others:
        entries.append(
            {
                "area": "other_opt_in_checks",
                "label": "other opt-in checks",
                "codes": others,
                "settings": setting_names(others),
            }
        )
    return entries


def not_checked_summary(entries: object, config_path: object = None) -> str | None:
    """One line naming unconfigured opt-in areas and how to enable them.

    ``config_path`` is the settings file this run loaded, if any (the report's
    ``execution.config_path``). The ``fledge init`` hint is only offered when no
    settings file was used.
    """
    if not isinstance(entries, list) or not entries:
        return None
    labels = [
        str(item["label"])
        for item in entries
        if isinstance(item, Mapping) and item.get("area") != "other_opt_in_checks"
    ]
    others = next(
        (
            item.get("codes")
            for item in entries
            if isinstance(item, Mapping) and item.get("area") == "other_opt_in_checks"
        ),
        None,
    )
    if isinstance(others, list) and others:
        labels.append(f"{len(others)} other opt-in check{'s' if len(others) != 1 else ''}")
    if not labels:
        return None
    where = (
        f"Add them to {config_path}"
        if isinstance(config_path, str) and config_path
        else f"Set them in fledge.toml (start with {PRESET_COMMAND})"
    )
    return (
        f"Not checked (needs your settings): {', '.join(labels)}. "
        f"{where}; see {CONFIGURATION_GUIDE}"
    )
