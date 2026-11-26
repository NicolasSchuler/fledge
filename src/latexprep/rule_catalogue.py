"""Classify registered checks and name their enabling settings for the CLI and docs."""

from __future__ import annotations

from typing import NamedTuple

from .check_policy import ActivationSection, opt_in_activation
from .config import Settings, toml_table
from .rule_definition import Rule

ALWAYS = "always"
DEFAULT = "default"
OPT_IN = "opt-in"
SUGGESTION = "suggestion"

# Selection clears whole policies, including tuning options. For these rules, replace
# the individual fields of each policy row with this one set of required fields.
_REQUIRED_FIELDS = {
    "MAN004": ("abstract_abbreviation_policy",),
    "PDF101": ("expected_page_width_pt", "expected_page_height_pt"),
    "PDF205": ("min_page_ink_ratio",),
    "PDF215": ("min_text_contrast", "contrast_background_rgb"),
    "PDF301": ("detect_printer_marks", "trim_regions", "printer_mark_max_length_pt"),
    "PDF307": ("artwork_category", "category_min_dpi"),
    "PDF310": ("raster_regions", "min_raster_region_coverage"),
}
# The venue's engine list is not a selection reset policy.
_CATALOGUE_ONLY: dict[str, ActivationSection] = {
    "TEX013": ("bbl_checks", {"venue_engines": ()}),
}
_REGION_NOTE = (
    "Set `figure_regions` in `[rendered_pdf_artwork]` to measure manuscript regions; "
    "included PDF figures default to whole pages."
)
_NOTES = {
    "FMT001": (
        "No setting switches on this diagnostic. `apply_formatting` (or `prepare --format`) "
        "applies formatting to the prepared copy, but does not report FMT001."
    ),
    "PDF207": "`match_source_pdf_metadata` needs a source project, not a standalone PDF.",
    "PDF212": "Needs a source project with included PDF figures, not a standalone PDF.",
    "PDF302": _REGION_NOTE,
    "PDF303": _REGION_NOTE,
    "PDF307": "`artwork_category` must name a category listed in `category_min_dpi`.",
    "PKG105": "Only measured when `fledge prepare` creates the archive.",
}
_ONLINE_NOTE = "`--online` can grant permission instead of setting `online` in `[online_checks]`."


class RuleActivation(NamedTuple):
    """Describe how a check is switched on.

    Attributes:
        kind: ``ALWAYS``, ``DEFAULT``, ``OPT_IN`` or ``SUGGESTION``.
        settings: Ordered alternatives for enabling an opt-in check. An alternative
            lists the fields needed together in one option group (``None`` for top level).
        selection: The command-line flags that add a suggestion to the usual checks.
        command: The operation that reports an opt-in diagnostic without a setting.
        note: An operational qualifier, with Markdown inline code where appropriate.
    """

    kind: str
    settings: tuple[tuple[str | None, tuple[str, ...]], ...] = ()
    selection: str | None = None
    command: str | None = None
    note: str = ""


def _setting_alternatives(code: str) -> list[ActivationSection]:
    """Return each sufficient set of configuration fields with their disabled values."""
    if code in _CATALOGUE_ONLY:
        return [_CATALOGUE_ONLY[code]]
    if code == "BLD105":
        # Setting an expected release adds a policy to an existing observation.
        return []
    alternatives = []
    for group, fields in opt_in_activation().get(code, []):
        if required := _REQUIRED_FIELDS.get(code):
            alternatives.append((group, {name: fields[name] for name in required}))
        elif group == "online_checks":
            alternatives.append((group, {**fields, "online": False}))
        else:
            alternatives.extend((group, {name: disabled}) for name, disabled in fields.items())
    return alternatives


def rule_activation(rule: Rule, defaults: Settings) -> RuleActivation:
    """Classify a check and retain its ordered alternatives for CLI and docs.

    Args:
        rule: The check.
        defaults: The settings against which to decide which checks are on by default.

    Returns:
        The activation kind and the alternatives for enabling the check, if needed.
    """
    if rule.mandatory:
        return RuleActivation(ALWAYS)
    if rule.suggestion:
        return RuleActivation(SUGGESTION, selection=f"--select ALL --select {rule.code}")
    if rule.code == "FMT001":
        return RuleActivation(OPT_IN, command="fledge fmt", note=_NOTES[rule.code])
    alternatives = _setting_alternatives(rule.code)
    values = defaults.top_level_values()
    if not alternatives or any(
        all(
            (values[name] if group is None else getattr(values[group], name)) != disabled
            for name, disabled in fields.items()
        )
        for group, fields in alternatives
    ):
        return RuleActivation(DEFAULT)
    settings = tuple((group, tuple(fields)) for group, fields in alternatives)
    note = _ONLINE_NOTE if any(group == "online_checks" for group, _ in settings) else ""
    return RuleActivation(OPT_IN, settings, note=_NOTES.get(rule.code, note))


def _setting_choice(group: str | None, names: tuple[str, ...], *, markdown: bool) -> str:
    """Name the required fields together in the table the settings file uses."""
    fields = " and ".join(f"`{name}`" if markdown else name for name in names)
    if group is None:
        return fields
    table = toml_table(group)
    return f"{fields} in `{table}`" if markdown else f"{fields} in [{table}]"


def activation_options(activation: RuleActivation, *, markdown: bool = False) -> tuple[str, ...]:
    """Name the alternatives that switch a check on.

    Args:
        activation: The check's activation metadata.
        markdown: Whether to mark up setting names and flags as inline code.

    Returns:
        The setting alternatives or suggestion flags, or an empty tuple for other checks.
    """
    if invocation := activation.selection or activation.command:
        return (f"`{invocation}`" if markdown else invocation,)
    return tuple(
        _setting_choice(group, names, markdown=markdown) for group, names in activation.settings
    )
