"""Opt-in, publisher-neutral checks of literal manuscript structures.

This is a bounded lexical interpretation, not TeX execution. Source order is
explicitly distinguished from rendered order and heuristic advice from policy
violations. No source is modified and no manuscript content is generated.

This module only runs the policies. Each policy family is a ``manuscript_*_checks``
module with the same signature, a ``Reading`` of the expanded document and the
options, and returns the findings of the policies that are switched on:

``manuscript_package_checks``     direct package inventory and allowlist
``manuscript_layout_checks``      layout overrides and line numbers
``manuscript_abstract_checks``    citations and abbreviations in the abstract
``manuscript_heading_checks``     heading depth, empty sections, required declarations
``manuscript_reference_checks``   hard-coded figure, table and section numbers
``manuscript_metadata_checks``    required metadata and ORCID identifiers
``manuscript_float_checks``       captions, labels, references and descriptions of floats

The constraints on document class, class options, forbidden packages, abstract and
keyword counts and required sections are the separate checks in ``manuscript``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .analysed_source import AnalysedSource, reuse_or_analyse
from .findings import Finding
from .manuscript_abstract_checks import abstract_checks
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_float_checks import float_checks
from .manuscript_heading_checks import declaration_checks, heading_checks
from .manuscript_layout_checks import layout_checks, line_number_checks
from .manuscript_metadata_checks import metadata_checks
from .manuscript_package_checks import package_checks
from .manuscript_policy_kernel import Reading, interpret
from .manuscript_reference_checks import reference_checks

PolicyFamily = Callable[[Reading, ManuscriptCheckOptions], list[Finding]]

# Findings are reported in this order.
_FAMILIES: tuple[PolicyFamily, ...] = (
    package_checks,
    layout_checks,
    abstract_checks,
    heading_checks,
    reference_checks,
    declaration_checks,
    metadata_checks,
    float_checks,
    line_number_checks,
)
_DEFAULT_OPTIONS = ManuscriptCheckOptions()


def check_manuscript_policies(
    root: Path,
    main: str | None,
    options: ManuscriptCheckOptions = _DEFAULT_OPTIONS,
    *,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Run selected source policy checks; defaults enable no additional manuscript policy.

    Args:
        root: The project root to analyse.
        main: The main file as the caller named it; ``None`` selects the single document
            root.
        options: Which policies run and the values they compare against.
        analysed: The source analysis of the same root and main file, shared with other
            checks; without one the document is expanded here.

    Returns:
        The findings of every policy family that is switched on, in reporting order, or
        an empty list with the default options.
    """
    if options == _DEFAULT_OPTIONS:
        return []
    reading = interpret(reuse_or_analyse(root, main, analysed).expanded)
    return [finding for family in _FAMILIES for finding in family(reading, options)]
