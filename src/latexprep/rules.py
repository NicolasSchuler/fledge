"""Stable public check codes, descriptions, and behavioral test associations.

Codes are assigned explicitly and are never recycled. Execution diagnostics and
raw inventories are not checks and intentionally have no code. Reports carry the
descriptive rule name (``family.check_name``); renamed rules keep their former
names as aliases so selectors and lookups that use them still resolve.

Every family keeps its entries in a data-only ``*_rules.py`` module that exports
``RULES``, a tuple of :class:`Rule` values.
"""

from __future__ import annotations

from collections import Counter
from types import MappingProxyType

from .bibliography_check_rules import RULES as BIBLIOGRAPHY_RULES
from .bibliography_transform_rules import RULES as BIBLIOGRAPHY_TRANSFORM_RULES
from .build_check_rules import RULES as BUILD_RULES
from .core_rules import RULES as CORE_RULES
from .loaded_options_rules import RULES as LOADED_OPTIONS_RULES
from .manuscript_check_rules import RULES as MANUSCRIPT_RULES
from .metadata_privacy_rules import RULES as METADATA_PRIVACY_RULES
from .online_check_rules import RULES as ONLINE_RULES
from .pdf_artwork_rules import RULES as PDF_ARTWORK_RULES
from .pdf_check_rules import RULES as PDF_RULES
from .reporting_rules import RULES as REPORTING_RULES
from .rule_definition import Rule
from .runtime_rules import RULES as RUNTIME_RULES
from .source_transform_rules import RULES as SOURCE_TRANSFORM_RULES
from .structure_rules import RULES as STRUCTURE_RULES
from .submission_check_rules import RULES as SUBMISSION_RULES
from .submission_readiness_rules import RULES as SUBMISSION_READINESS_RULES
from .workflow_rules import RULES as WORKFLOW_RULES


def _catalogue(*families: tuple[Rule, ...]) -> tuple[Rule, ...]:
    rules = tuple(rule for family in families for rule in family)
    duplicates = [
        value
        for value, count in Counter(
            [rule.code for rule in rules]
            + [name for rule in rules for name in (rule.name, *rule.aliases)]
        ).items()
        if count > 1
    ]
    if duplicates:
        raise RuntimeError(f"Duplicate check codes or rule names: {', '.join(sorted(duplicates))}")
    return rules


RULES = _catalogue(
    CORE_RULES,
    BIBLIOGRAPHY_RULES,
    BUILD_RULES,
    LOADED_OPTIONS_RULES,
    MANUSCRIPT_RULES,
    METADATA_PRIVACY_RULES,
    ONLINE_RULES,
    PDF_RULES,
    SUBMISSION_RULES,
    SUBMISSION_READINESS_RULES,
    STRUCTURE_RULES,
    WORKFLOW_RULES,
    REPORTING_RULES,
    RUNTIME_RULES,
    SOURCE_TRANSFORM_RULES,
    BIBLIOGRAPHY_TRANSFORM_RULES,
    PDF_ARTWORK_RULES,
)
BY_CODE = MappingProxyType({rule.code: rule for rule in RULES})
BY_NAME = MappingProxyType({name: rule for rule in RULES for name in (rule.name, *rule.aliases)})


def code_for(name: str) -> str | None:
    rule = BY_NAME.get(name)
    return rule.code if rule is not None else None


def get_rule(code: str) -> Rule:
    try:
        return BY_CODE[code.upper()]
    except KeyError:
        raise ValueError(
            f"Unknown check code {code!r}; run 'fledge rules' to list checks"
        ) from None
