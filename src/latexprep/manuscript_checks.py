"""Opt-in, publisher-neutral checks of literal manuscript structures.

This is a bounded lexical interpretation, not TeX execution. Source order is
explicitly distinguished from rendered order and heuristic advice from policy
violations. No source is modified and no manuscript content is generated.

This module only runs the policies. Each policy family is a ``manuscript_*_checks``
module with the same signature, a ``Reading`` of the expanded document and the
options, and returns the findings of the policies that are switched on:

``manuscript_package_checks``     direct package inventory, allowlist, output drivers
``manuscript_layout_checks``      layout overrides and line numbers
``manuscript_abstract_checks``    citations and abbreviations in the abstract
``manuscript_heading_checks``     heading depth, empty sections, required declarations
``manuscript_reference_checks``   hard-coded figure, table and section numbers
``manuscript_metadata_checks``    required metadata, ORCID identifiers, accessibility metadata
``manuscript_float_checks``       captions, labels, references and descriptions of floats

The selected style suggestions of the ``STY`` family (``style_suggestions``) run over the
same expanded document. The constraints on document class, class options, forbidden
packages, abstract and keyword counts and required sections are the separate checks in
``manuscript``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .analysed_source import AnalysedSource, reuse_or_analyse
from .check_policy import suggestion_selected
from .check_selection import CheckSelection
from .findings import Finding
from .manuscript_abstract_checks import abstract_checks
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_float_checks import float_checks
from .manuscript_heading_checks import declaration_checks, heading_checks
from .manuscript_layout_checks import layout_checks, line_number_checks
from .manuscript_metadata_checks import document_metadata_checks, metadata_checks
from .manuscript_package_checks import output_driver_checks, package_checks
from .manuscript_policy_kernel import Reading, interpret
from .manuscript_reference_checks import reference_checks
from .style_suggestions import STYLE_CODES, style_suggestions

PolicyFamily = Callable[[Reading, ManuscriptCheckOptions], list[Finding]]

# Findings are reported in this order.
_FAMILIES: tuple[PolicyFamily, ...] = (
    package_checks,
    output_driver_checks,
    layout_checks,
    abstract_checks,
    heading_checks,
    reference_checks,
    declaration_checks,
    metadata_checks,
    document_metadata_checks,
    float_checks,
    line_number_checks,
)
_DEFAULT_OPTIONS = ManuscriptCheckOptions()


def selected_style_codes(selection: CheckSelection | None) -> frozenset[str]:
    """Return the style suggestions that a selector other than ``ALL`` selects.

    Args:
        selection: The check selection, or ``None`` for none.

    Returns:
        The selected ``STY`` codes; none without a selection.
    """
    if selection is None:
        return frozenset()
    return frozenset(code for code in STYLE_CODES if suggestion_selected(selection, code))


def check_manuscript_policies(
    root: Path,
    main: str | None,
    options: ManuscriptCheckOptions = _DEFAULT_OPTIONS,
    *,
    analysed: AnalysedSource | None = None,
    selection: CheckSelection | None = None,
) -> list[Finding]:
    """Run selected source policy checks; defaults enable no additional manuscript policy.

    Args:
        root: The project root to analyse.
        main: The main file as the caller named it; ``None`` selects the single document
            root.
        options: Which policies run and the values they compare against.
        analysed: The source analysis of the same root and main file, shared with other
            checks; without one the document is expanded here.
        selection: The check selection, whose explicit selectors choose the style
            suggestions to run; ``None`` runs none.

    Returns:
        The findings of every policy family that is switched on, in reporting order, then
        the selected style suggestions; an empty list with the default options and no
        selected suggestion.
    """
    style_codes = selected_style_codes(selection)
    if options == _DEFAULT_OPTIONS and not style_codes:
        return []
    document = reuse_or_analyse(root, main, analysed).expanded
    findings: list[Finding] = []
    if options != _DEFAULT_OPTIONS:
        reading = interpret(document)
        findings.extend(finding for family in _FAMILIES for finding in family(reading, options))
    if style_codes:
        findings.extend(
            style_suggestions(
                document,
                style_codes,
                output_drivers_checked=options.forbid_output_drivers,
                document_metadata_checked=options.require_document_metadata,
            )
        )
    return findings
