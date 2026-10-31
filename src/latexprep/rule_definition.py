"""The shape of one catalogued check; data-only ``*_rules.py`` modules build these."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Increases only when a field of the JSON output of ``fledge rules`` or ``fledge rule`` is
# removed, renamed or changes meaning; new fields may appear within a version.
RULE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Rule:
    """One catalogued check.

    ``mandatory``, ``reviewable`` and ``not_checked_area`` are facts about how the rule is
    treated, so no module keeps a list of codes of its own.

    Attributes:
        code: The stable public identifier (``TEX001``), never reused.
        name: The descriptive dotted name reports carry.
        title: The short title shown in the list of checks and in an explanation.
        description: What the check examines and what it cannot establish.
        tests: The dotted ids (``tests.test_module.Class.test_name``) of the tests that
            demonstrate the behaviour; a test verifies that each one exists. They document
            the check for developers and are shown only in the JSON form of
            ``fledge rule``.
        fix: The generic next step for a failure of this check; a finding may carry a more
            specific suggestion, which takes precedence.
        aliases: Former names that still resolve.
        mandatory: Marks a guard whose finding check selection never removes: ignoring its
            code does not switch it off.
        reviewable: Marks a check whose failure an explicit review decision may suppress
            or accept.
        not_checked_area: The opt-in area (``coverage_hints``) that a run reports as not
            checked while none of the area's checks is configured.
    """

    code: str
    name: str
    title: str
    description: str
    tests: tuple[str, ...]
    fix: str
    aliases: tuple[str, ...] = ()
    mandatory: bool = False
    reviewable: bool = False
    not_checked_area: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the rule as plain data for ``fledge rules --json`` and ``fledge rule --json``."""
        return asdict(self)
