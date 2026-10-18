"""Catalog-language, annotation and action policies read from qpdf's object data.

qpdf's JSON supplies the document's object structure: the resolved catalog, page
annotation arrays and every reachable dictionary. These checks speak only for what the
objects declare. They do not classify executable semantics, read content streams or
look at orphan objects. This module owns the three object policies; the object graph is
``pdf_qpdf_graph`` and running qpdf is ``pdf_tools``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection

from .errors import PreparationError
from .findings import Finding, Severity
from .measurement_findings import heuristic_finding, measurement_finding, unavailable_finding
from .pdf_check_options import PdfCheckOptions
from .pdf_qpdf_graph import QpdfObjectGraph
from .pdf_rule_names import PdfRule
from .pdf_tool_output import MAX_PARSED_NODES
from .pdf_tools import PdfToolSession, ToolMeasurement, qpdf_objects_argv


def _language_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
    raw = objects.resolve(objects.root.get("/Lang"))
    language = raw[2:].strip() if isinstance(raw, str) and raw.startswith("u:") else None
    expected = options.expected_language
    violations: list[object] = []
    if raw is None or language == "":
        violations.append(
            "The PDF catalog declares no language"
            + (f"; the configured language is {expected!r}." if expected else ".")
        )
    elif (
        language is not None and expected is not None and language.casefold() != expected.casefold()
    ):
        violations.append(
            f"Catalog language {language!r} differs from the configured language {expected!r}."
        )
    return measurement_finding(
        PdfRule.DOCUMENT_LANGUAGE,
        "Checked the catalog language declaration.",
        violations,
        incomplete=raw is not None and language is None,
        details={
            "declared_language": language,
            "expected": expected,
            "scope": "Declaration presence/exact language-tag agreement, not the language of "
            "the prose.",
        },
    )


def _annotation_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
    annotations: list[object] = []
    count = 0
    for page_number, page in enumerate(objects.pages, 1):
        if not isinstance(page, dict) or "object" not in page:
            raise PreparationError("qpdf page inventory lacks a page object reference")
        page_object = objects.dictionary(page["object"])
        values = objects.resolve(page_object.get("/Annots", []))
        if not isinstance(values, list):
            raise PreparationError("qpdf page annotation inventory is malformed")
        for value in values:
            annotation = objects.dictionary(value)
            subtype = objects.resolve(annotation.get("/Subtype"))
            if not isinstance(subtype, str) or not subtype.startswith("/"):
                raise PreparationError("An annotation has no measurable subtype")
            count += 1
            if count > MAX_PARSED_NODES:
                raise PreparationError("Page annotations exceed the bounded traversal limit")
            if subtype[1:] in options.forbidden_annotation_types:
                annotations.append({"page": page_number, "type": subtype[1:]})
    return measurement_finding(
        PdfRule.ANNOTATION_POLICY,
        "Inspected page annotation object subtypes.",
        annotations,
        details={
            "annotation_count": count,
            "forbidden_types": list(options.forbidden_annotation_types),
            "scope": "Page Annots arrays; content-stream drawings and inaccessible "
            "orphan objects excluded.",
        },
    )


def _action_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
    actions: list[object] = []
    # A matching /S in reachable objects is intentionally conservative: qpdf
    # resolves compressed dictionaries but does not classify executable semantics.
    for node in objects.reachable_dictionaries():
        subtype = objects.resolve(node.get("/S"))
        if (
            isinstance(subtype, str)
            and subtype.startswith("/")
            and subtype[1:] in options.forbidden_action_types
        ):
            actions.append(subtype[1:])
    return heuristic_finding(
        PdfRule.ACTION_POLICY,
        "Scanned reachable object dictionaries for configured action names.",
        actions,
        details={
            "forbidden_types": list(options.forbidden_action_types),
            "scope": "Reachable /S names; a match is an action candidate requiring "
            "context review. "
            "Opaque streams and unreferenced objects are excluded; no malware-free claim.",
        },
    )


# Each object check, and the severity of its finding when its evidence is unavailable.
_OBJECT_CHECKS: tuple[
    tuple[str, Callable[[QpdfObjectGraph, PdfCheckOptions], Finding], Severity], ...
] = (
    (PdfRule.DOCUMENT_LANGUAGE, _language_finding, "error"),
    (PdfRule.ANNOTATION_POLICY, _annotation_finding, "error"),
    (PdfRule.ACTION_POLICY, _action_finding, "warning"),
)


def _object_findings(
    output: str, pages: int, options: PdfCheckOptions, rules: Collection[str]
) -> list[Finding]:
    objects = QpdfObjectGraph(output, pages)
    # Preserve completed observations if another part of the object graph is incomplete.
    result = []
    for rule, check, unavailable_severity in _OBJECT_CHECKS:
        if rule not in rules:
            continue
        try:
            result.append(check(objects, options))
        except PreparationError as error:
            result.append(unavailable_finding(rule, error, severity=unavailable_severity))
    return result


async def measure_object_policies(
    session: PdfToolSession, pages: int, options: PdfCheckOptions, rules: Collection[str]
) -> list[Finding]:
    """The configured object policies; each of ``rules`` is inconclusive if qpdf fails."""
    return await ToolMeasurement(
        "objects",
        qpdf_objects_argv(),
        tuple(rules),
        lambda result: _object_findings(result.stdout, pages, options, rules),
    ).run(session)
