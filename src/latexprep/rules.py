"""Stable public check codes, descriptions, and behavioral test associations.

Codes are assigned explicitly and are never recycled. Execution diagnostics and
raw inventories are not checks and intentionally have no code. Existing internal
rule names remain in reports for compatibility and more detailed context.

Every family keeps its entries in a data-only ``*_rules.py`` module with the same
shape: code, name, title, description, tests, fix and optional aliases.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from .bibliography_check_rules import RULE_DEFINITIONS as BIBLIOGRAPHY_RULES
from .bibliography_transform_rules import RULE_DEFINITIONS as BIBLIOGRAPHY_TRANSFORM_RULES
from .build_check_rules import RULE_DEFINITIONS as BUILD_RULES
from .core_rules import RULE_DEFINITIONS as CORE_RULES
from .loaded_options_rules import RULE_DEFINITIONS as LOADED_OPTIONS_RULES
from .manuscript_check_rules import RULE_DEFINITIONS as MANUSCRIPT_RULES
from .metadata_privacy_rules import RULE_DEFINITIONS as METADATA_PRIVACY_RULES
from .online_check_rules import RULE_DEFINITIONS as ONLINE_RULES
from .pdf_artwork_rules import RULE_DEFINITIONS as PDF_ARTWORK_RULES
from .pdf_check_rules import RULE_DEFINITIONS as PDF_RULES
from .reporting_rules import RULE_DEFINITIONS as REPORTING_RULES
from .runtime_rules import RULE_DEFINITIONS as RUNTIME_RULES
from .source_transform_rules import SOURCE_TRANSFORM_RULES
from .structure_rules import RULE_DEFINITIONS as STRUCTURE_RULES
from .submission_check_rules import RULE_DEFINITIONS as SUBMISSION_RULES
from .workflow_rules import WORKFLOW_RULES


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    title: str
    description: str
    tests: tuple[str, ...]
    fix: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _rule(definition: Mapping[str, Any]) -> Rule:
    return Rule(
        code=str(definition["code"]),
        name=str(definition["name"]),
        title=str(definition["title"]),
        description=str(definition["description"]),
        tests=tuple(definition["tests"]),
        fix=str(definition["fix"]),
        aliases=tuple(definition.get("aliases", ())),
    )


def _catalogue(*families: Iterable[Mapping[str, Any]]) -> tuple[Rule, ...]:
    rules = tuple(_rule(definition) for family in families for definition in family)
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
