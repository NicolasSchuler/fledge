"""Stable public check codes, descriptions, and behavioral test associations.

Codes are assigned explicitly and are never recycled. Operational results (see
``OPERATIONAL_RULE_NAMES``: results that describe the run or the tool environment, not the
manuscript) are not checks and intentionally have no code. Reports carry
the descriptive rule name (``family.check_name``); renamed rules keep their former names
as aliases so selectors and lookups that use them still resolve.

Every family keeps its entries in a data-only ``*_rules.py`` module that exports
``RULES``, a tuple of :class:`Rule` values. The facts about a rule (``mandatory``,
``reviewable``, ``not_checked_area``) are fields of its entry; the sets of codes below
are derived from them here and nowhere else.
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
from .pdf_rule_names import OPERATIONAL_PDF_RULES
from .reporting_rules import RULES as REPORTING_RULES
from .rule_definition import Rule
from .runtime_rules import RULES as RUNTIME_RULES
from .source_graph_rules import RULES as SOURCE_GRAPH_RULES
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
    SOURCE_GRAPH_RULES,
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

# These establish the safety of reading, transforming, building or publishing a prepared
# copy. Check selection never removes their findings: ignoring the code does not switch
# them off. Bibliography syntax, key, macro and relationship checks are kept in every
# command rather than trying to infer which proposed edit relies on them.
MANDATORY_CODES = frozenset(rule.code for rule in RULES if rule.mandatory)
# Explicitly enumerated presentation and content policies whose failure an explicit review
# decision may suppress or accept. A new check is ineligible until its evidence and safety
# contract have been reviewed and the rule is marked ``reviewable``.
REVIEWABLE_CODES = frozenset(rule.code for rule in RULES if rule.reviewable)

# Names that reports carry but the catalogue deliberately does not list. Operational means
# the result describes the run or the tool environment, not the manuscript, so it has no
# public code, cannot be selected or ignored, and is always reported (``Finding.code`` is
# ``None``). Every other name a finding can carry is catalogued;
# ``tests/test_emitted_rule_names.py`` fails for one that is neither.
OPERATIONAL_RULE_NAMES = frozenset(
    {
        # The run itself: why it stopped, was refused or could not write its outputs.
        "execution.cancelled",
        "execution.deadline",
        "execution.internal",
        "execution.request",
        "execution.resources",
        "execution.task",
        "package.document",
        "report.redaction_limit",
        "report.write",
        # Builds: their outcome, limits and raw evidence. The failure of one rebuild stage is
        # reported as ``build.<stage>`` (baseline, transformed, prepared, archive).
        "build.completed",
        "build.dependency_trace",
        "build.execution_unavailable",
        "build.failed",
        "build.log_unavailable",
        "build.missing_pdf",
        "build.output_limit",
        "build.package_inventory",
        "build.resource_limit",
        "build.submission_dependencies",
        "build.timeout",
        # Tools and services: their availability, versions and request counts.
        "compare.tools",
        "compare.unavailable",
        "online.not_reached",
        "online.requests",
        "source.format_execution",
        "source.formatter_available",
        "doctor.build_failed",
        "doctor.execution_unavailable",
        "doctor.fixture_failed",
        "doctor.fixture_passed",
        "doctor.format_evidence",
        "doctor.pdf_evidence",
        "doctor.sandbox",
        "doctor.sandbox_unavailable",
        "doctor.tool_missing",
        "doctor.tool_unavailable",
        # Raw inventories of what was read.
        "manuscript.inventory",
        "manuscript.package_inventory",
        *OPERATIONAL_PDF_RULES,
    }
)


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
