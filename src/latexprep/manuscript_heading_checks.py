"""Heading depth, empty sections and required declarations (``MAN005``, ``MAN006``, ``MAN010``).

All three read the sectioning commands of the expanded document. A heading's subtree
runs to the next heading of the same or a higher level. Depth compares each literal
heading with a configured maximum; the empty-section scan is advisory and treats any
unknown command as possible content; required declarations look for a nonempty
section or ``\\declaration{name}{text}`` under each configured name. Presence is the
only claim: whether a statement is adequate needs a human.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cancellation import cancellation_point
from .expanded_document import ExpandedDocument
from .finding_text import plural
from .finding_weight import ADVISORY
from .findings import Finding
from .literal_text import (
    HEADING_DEPTHS,
    is_placeholder_text,
    normalize_heading,
    plain_argument_text,
    plain_text,
)
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .tex_commands import MATH_MARKER
from .tex_lexer import command_end
from .uncertainty_reasons import context_reasons

_DEPTH = Policy("manuscript.heading_depth", "Checked the configured literal heading depth.")
_EMPTY_SECTIONS = Policy(
    "manuscript.empty_sections", "Checked apparently empty section subtrees.", ADVISORY
)
_DECLARATIONS = Policy(
    "manuscript.required_declarations", "Checked required nonempty declarations."
)
# Commands blanked before judging whether a section body has content.
_BLANKED_IN_CONTENT = frozenset(
    {
        *HEADING_DEPTHS,
        "label",
        "nocite",
        "centering",
        "raggedright",
        "raggedleft",
        "noindent",
        "declaration",
    }
)
# Commands that put content on the page without any literal text.
_GENERATED_CONTENT = {
    "includegraphics",
    "rule",
    "bibliography",
    "printbibliography",
    "tableofcontents",
    "listoffigures",
    "listoftables",
}


@dataclass(slots=True)
class Heading:
    """One literal sectioning command and the offsets of the subtree it opens."""

    path: str | None
    line: int | None
    title: str
    depth: int
    start: int
    body_start: int
    end: int
    uncertainty: list[str]

    def as_dict(self) -> dict[str, object]:
        """The heading as report details."""
        return {
            "path": self.path,
            "line": self.line,
            "title": self.title,
            "depth": self.depth,
            "start": self.start,
            "body_start": self.body_start,
            "end": self.end,
            "uncertainty": self.uncertainty,
        }


def _headings(document: ExpandedDocument) -> list[Heading]:
    result: list[Heading] = []
    pending: list[int] = []
    for command in document.commands:
        if command.name not in HEADING_DEPTHS:
            continue
        cancellation_point()
        depth = HEADING_DEPTHS[command.name]
        value, reasons = plain_argument_text(command)
        reasons.extend(context_reasons(command, document.conditionals, "heading"))
        while pending and result[pending[-1]].depth >= depth:
            result[pending.pop()].end = command.start
        location = document.location(command.start)
        result.append(
            Heading(
                location.path,
                location.line,
                value,
                depth,
                command.start,
                command_end(command),
                len(document.text),
                reasons,
            )
        )
        pending.append(len(result) - 1)
    return result


def _heading_reasons(reading: Reading, headings: list[Heading]) -> list[str]:
    return [
        *reading.uncertainty,
        *(reason for heading in headings for reason in heading.uncertainty),
    ]


def _content(document: ExpandedDocument, start: int, end: int) -> tuple[bool, list[str]]:
    text = document.text[start:end]
    commands = document.between(start, end)
    edits = [
        (command.start - start, min(command_end(command), end) - start)
        for command in commands
        if command.name in _BLANKED_IN_CONTENT
    ]
    for opening, closing in reversed(edits):
        text = text[:opening] + " " * (closing - opening) + text[closing:]
    plain, reasons = plain_text(text)
    if any(command.name in _GENERATED_CONTENT for command in commands):
        return True, []
    # Mathematics makes a section nonempty.
    if MATH_MARKER.search(text):
        return True, []
    # Unknown commands prevent an empty-content claim even when their arguments
    # happen to contain text; semantic content cannot be inferred from an argument.
    return bool(plain.strip()), reasons


def _heading_depth_finding(headings: list[Heading], reasons: list[str], maximum: int) -> Finding:
    deep = [heading for heading in headings if heading.depth > maximum and not heading.uncertainty]
    count = plural(len(headings), "literal heading")
    return policy_finding(
        _DEPTH,
        CheckOutcome([heading.as_dict() for heading in deep], reasons),
        failure=f"{len(deep)} of {count} deeper than depth {maximum}",
        details={
            "maximum_depth": maximum,
            "headings": [heading.as_dict() for heading in headings],
            "depths": dict(HEADING_DEPTHS),
        },
    )


def _empty_sections_finding(document: ExpandedDocument, headings: list[Heading]) -> Finding:
    empty = []
    # Only the section's own heading and body decide whether it looks empty;
    # unexpanded constructs elsewhere cannot fill this subtree.
    unknown = list(document.graph_uncertainty)
    for heading in headings:
        nonempty, content_reasons = _content(document, heading.body_start, heading.end)
        local = [*heading.uncertainty, *content_reasons]
        unknown.extend(local)
        if not nonempty and not local:
            empty.append(heading.as_dict())
    count = plural(len(headings), "section subtree")
    return policy_finding(
        _EMPTY_SECTIONS,
        CheckOutcome(empty, unknown),
        failure=f"{len(empty)} of {count} with no observable content",
    )


def heading_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """The heading-depth and empty-section policies that are switched on."""
    if options.max_heading_depth is None and not options.check_empty_sections:
        return []
    headings = _headings(reading.document)
    findings = []
    if options.max_heading_depth is not None:
        reasons = _heading_reasons(reading, headings)
        findings.append(_heading_depth_finding(headings, reasons, options.max_heading_depth))
    if options.check_empty_sections:
        findings.append(_empty_sections_finding(reading.document, headings))
    return findings


def _declaration_entries(
    document: ExpandedDocument, headings: list[Heading], required: tuple[str, ...]
) -> tuple[list[dict[str, object]], list[str]]:
    """The headings and ``\\declaration`` commands that could supply a required declaration."""
    declarations: list[dict[str, object]] = []
    unknown: list[str] = []
    required_names = {normalize_heading(name) for name in required}
    for heading in headings:
        if normalize_heading(heading.title) not in required_names:
            continue
        nonempty, content_reasons = _content(document, heading.body_start, heading.end)
        unknown.extend(content_reasons)
        declarations.append({**heading.as_dict(), "nonempty": nonempty})
    for command in document.commands:
        if command.name != "declaration":
            continue
        name, name_reasons = plain_argument_text(command)
        value, content_reasons = plain_argument_text(command, 1)
        unknown.extend([*name_reasons, *content_reasons])
        unknown.extend(context_reasons(command, document.conditionals, "declaration"))
        nonempty = bool(value) and not is_placeholder_text(value)
        declarations.append(document.located(command.start, title=name, nonempty=nonempty))
    return declarations, unknown


def declaration_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """The required-declarations policy, when declarations are configured."""
    required = options.required_declarations
    if not required:
        return []
    document = reading.document
    headings = _headings(document)
    declarations, content_unknown = _declaration_entries(document, headings, required)
    unknown = [*_heading_reasons(reading, headings), *content_unknown]
    # An absent declaration could be generated by anything the scan could not
    # interpret, so a confirmed absence requires a fully interpreted source.
    missing = (
        [
            {"declaration": name, "reason": "missing or empty literal declaration"}
            for name in required
            if not any(
                normalize_heading(str(item["title"])) == normalize_heading(name)
                and item["nonempty"]
                for item in declarations
            )
        ]
        if not unknown
        else []
    )
    count = plural(len(required), "required declaration")
    return [
        policy_finding(
            _DECLARATIONS,
            CheckOutcome(missing, unknown),
            failure=f"{len(missing)} of {count} missing or empty",
            named=listing("Missing", (str(item["declaration"]) for item in missing)),
            details={
                "declarations": declarations,
                "required": list(required),
                "adequacy": "Presence only; applicability and content require human review.",
            },
        )
    ]
