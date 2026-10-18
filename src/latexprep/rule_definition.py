"""The shape of one catalogued check; data-only ``*_rules.py`` modules build these."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Increases only when a field of the JSON output of ``fledge rules`` or ``fledge rule`` is
# removed, renamed or changes meaning; new fields may appear within a version.
RULE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Rule:
    """One catalogued check.

    ``code`` is the stable public identifier (``TEX001``), never reused. ``name`` is
    the descriptive dotted name reports carry, and ``aliases`` are former names that
    still resolve. ``description`` states what the check examines and what it cannot
    establish. ``fix`` is the generic next step for a failure of this check; a finding
    may carry a more specific suggestion, which takes precedence. ``tests`` are the
    dotted ids (``tests.test_module.Class.test_name``) of the tests that demonstrate
    the behaviour; a test verifies that each one exists. They document the check for
    developers and are shown only in the JSON form of ``fledge rule``.
    """

    code: str
    name: str
    title: str
    description: str
    tests: tuple[str, ...]
    fix: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """The rule as plain data for ``fledge rules --json`` and ``fledge rule --json``."""
        return asdict(self)
