"""The direct package inventory and the allowlist policy (``MAN001``).

Package declarations are read from every selected local source, class and style file,
not only the main file. The inventory is inconclusive when a construct could build,
rename or conditionally execute a declaration; a conditional declaration is never
reported as a violation. Packages that system classes load are out of scope.
"""

from __future__ import annotations

from .expanded_document import (
    ExpandedDocument,
    commands_before_end,
)
from .finding_text import plural
from .findings import EvidenceKind, Finding
from .literal_text import PACKAGE_COMMANDS, Declaration, first_argument_names
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import INTERPRETED_COMMANDS
from .tex_commands import MACRO_DEFINITION_COMMANDS
from .tex_lexer import (
    command_end,
    conditional_spans,
    definition_spans,
    in_conditional,
    in_half_open_span,
)

_INVENTORY_RULE = "manuscript.package_inventory"
_ALLOWLIST = Policy(
    "manuscript.package_allowlist", "Checked the configured direct-package allowlist."
)
_SCOPE = (
    "Direct literal package declarations in selected local sources, classes and styles; "
    "transitive system package loading and effective options are not established."
)
# Constructs that could construct, rename or conditionally execute a package
# declaration. Macro definitions cannot: they only name a replacement body.
_PACKAGE_GENERATORS = {
    "csname",
    "expandafter",
    "noexpand",
    "scantokens",
    "catcode",
    "ExplSyntaxOn",
    "directlua",
    "luadirect",
    "luaexec",
    "@ifpackageloaded",
    "@ifclassloaded",
    "@ifpackagelater",
    "@ifclasslater",
    "@ifpackagewith",
    "@ifclasswith",
    "IfFileExists",
    "InputIfFileExists",
    "ProcessOptions",
    "ExecuteOptions",
    "PassOptionsToPackage",
    "PassOptionsToClass",
    "AtEndOfPackage",
    "AtEndOfClass",
}
# Inside a conditional, only these commands cannot generate a package declaration.
_INERT_IN_PACKAGE_CONDITIONALS = (
    INTERPRETED_COMMANDS
    | MACRO_DEFINITION_COMMANDS
    | {"ProvidesPackage", "ProvidesClass", "ProvidesFile", "NeedsTeXFormat"}
)


def _package_declarations(document: ExpandedDocument) -> tuple[list[Declaration], list[str]]:
    """Collect the direct package declarations in each selected source, and what could hide others.

    Args:
        document: The expanded document whose selected sources are read.

    Returns:
        The declarations in source order, each with its names, options and its own gaps,
        and every reason the inventory may be incomplete.
    """
    declarations: list[Declaration] = []
    reasons = list(document.graph_uncertainty)
    for path in sorted(document.selected):
        source = document.graph.sources[path]
        # Only constructs that could build, rename or conditionally execute a
        # declaration matter here. An ordinary macro definition cannot, so its
        # presence must not turn the whole inventory inconclusive.
        conditionals = conditional_spans(source.commands)
        definitions = definition_spans(source.masked, source.commands)
        for command in commands_before_end(source):
            conditional = in_conditional(conditionals, command.start)
            if command.name == "endinput" and (
                command.depth
                or conditional
                or source.masked[command_end(command) :].split("\n", 1)[0].strip()
            ):
                reasons.append(f"{path}: package declarations have unresolved endinput context")
            if command.name in _PACKAGE_GENERATORS:
                reasons.append(
                    f"{path}:{command.line}: \\{command.name} can generate a package declaration"
                )
            if command.name not in PACKAGE_COMMANDS:
                if (
                    conditional
                    and not in_half_open_span(definitions, command.start)
                    and command.name not in _INERT_IN_PACKAGE_CONDITIONALS
                ):
                    reasons.append(
                        f"{path}:{command.line}: unexpanded constructs inside a conditional "
                        "can generate package declarations"
                    )
                continue
            names = first_argument_names(command)
            unknown = []
            if command.depth or conditional:
                unknown.append("grouped, macro-generated or conditional package declaration")
            if names is None:
                unknown.append("nonliteral package declaration")
            reasons.extend(unknown)
            declarations.append(
                {
                    "path": path,
                    "line": command.line,
                    "names": names,
                    "options": [argument.value for argument in command.options],
                    "uncertainty": unknown,
                }
            )
    return declarations, reasons


def package_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Inventory the direct package declarations, and check the allowlist when one is configured.

    A declaration with its own interpretation gaps or nonliteral names is never reported
    as a violation.

    Args:
        reading: The expanded document.
        options: ``inventory_packages`` asks for the inventory, and ``allowed_packages``
            configures the allowlist, which also produces the inventory.

    Returns:
        The inventory finding, followed by the allowlist finding when an allowlist is
        configured, or an empty list when neither is asked for.
    """
    if options.allowed_packages is None and not options.inventory_packages:
        return []
    declarations, reasons = _package_declarations(reading.document)
    findings = [
        Finding(
            _INVENTORY_RULE,
            "Inventoried direct local package declarations.",
            severity="info",
            status="inconclusive" if reasons else "passed",
            evidence_kind=EvidenceKind.DIRECT,
            details={
                "declarations": declarations,
                "uncertainty": sorted(set(reasons)),
                "scope": _SCOPE,
            },
        )
    ]
    allowed = options.allowed_packages
    if allowed is not None:
        matches = []
        not_allowed: list[str] = []
        for declaration in declarations:
            names = declaration["names"]
            if names is None or declaration["uncertainty"]:
                continue
            disallowed = [name for name in names if name not in allowed]
            if disallowed:
                matches.append({**declaration, "disallowed": disallowed})
                not_allowed.extend(disallowed)
        findings.append(
            policy_finding(
                _ALLOWLIST,
                CheckOutcome(matches, reasons),
                failure=f"{len(matches)} of "
                f"{plural(len(declarations), 'direct package declaration')} not allowed",
                named=listing("Not allowed", not_allowed),
                details={"allowed": list(allowed), "declarations": declarations, "scope": _SCOPE},
            )
        )
    return findings
