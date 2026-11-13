"""The direct package inventory, the allowlist (``MAN001``) and fixed output drivers (``MAN018``).

Package declarations are read from every selected local source, class and style file,
not only the main file. The inventory is inconclusive when a construct could build,
rename or conditionally execute a declaration; a conditional declaration is never
reported as a violation. Packages that system classes load are out of scope. The output
driver policy reports driver options such as ``pdftex`` on packages that detect their
driver themselves, and packages such as psfig that work with one driver only: both break
the build on a service that chooses the engine itself.
"""

from __future__ import annotations

from .expanded_document import (
    ExpandedDocument,
    commands_before_end,
)
from .finding_text import plural
from .finding_weight import Weight
from .findings import EvidenceKind, Finding
from .literal_text import PACKAGE_COMMANDS, Declaration, first_argument_names, key_value_items
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import INTERPRETED_COMMANDS
from .tex_commands import (
    DRIVER_BOUND_PACKAGES,
    DRIVER_OPTION_PACKAGES,
    MACRO_DEFINITION_COMMANDS,
    OUTPUT_DRIVER_OPTIONS,
)
from .tex_spans import (
    command_end,
    conditional_spans,
    definition_spans,
    in_conditional,
    in_half_open_span,
)
from .uncertainty_reasons import context_reasons

_INVENTORY_RULE = "manuscript.package_inventory"
_ALLOWLIST = Policy(
    "manuscript.package_allowlist", "Checked the configured direct-package allowlist."
)
# A fixed driver warns; a declaration that could not be read only informs, because local
# classes and styles commonly hold constructs (\ProcessOptions) that could generate one.
_OUTPUT_DRIVERS = Policy(
    "manuscript.output_drivers",
    "No output driver is fixed in the sources.",
    Weight("warning", "info", EvidenceKind.DERIVED),
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


def fixed_output_drivers(names: list[str], options: list[str]) -> list[str]:
    """Name what ties one declaration to a single output driver.

    Args:
        names: The declared package names.
        options: The option groups of the declaration, as written.

    Returns:
        Each driver option given to a package that detects its driver itself, and each
        package that works with one driver only, as ``option pdftex`` or ``package psfig``.
    """
    keys = {key for value in options for key, _ in key_value_items(value)}
    drivers = sorted(keys & OUTPUT_DRIVER_OPTIONS) if set(names) & DRIVER_OPTION_PACKAGES else []
    return [
        *(f"option {driver}" for driver in drivers),
        *(f"package {name}" for name in sorted(set(names) & DRIVER_BOUND_PACKAGES)),
    ]


def _class_driver_options(
    document: ExpandedDocument,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    r"""Find driver options among the class options, which LaTeX passes on to every package.

    Args:
        document: The expanded document.

    Returns:
        The located ``\documentclass`` declarations with driver options, what each of
        them fixes (``class option dvips``), and the reasons a class declaration could not
        be read.
    """
    matches: list[dict[str, object]] = []
    fixed: list[str] = []
    reasons: list[str] = []
    for command in document.commands:
        if command.name != "documentclass":
            continue
        unclear = context_reasons(command, document.conditionals, "class declaration")
        if unclear:
            reasons.extend(unclear)
            continue
        keys = {key for option in command.options for key, _ in key_value_items(option.value)}
        if drivers := [f"class option {name}" for name in sorted(keys & OUTPUT_DRIVER_OPTIONS)]:
            matches.append(document.located(command.start, fixed=drivers))
            fixed.extend(drivers)
    return matches, fixed, reasons


def output_driver_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Find output drivers fixed in the sources when ``forbid_output_drivers`` is set.

    A declaration with its own interpretation gaps is never reported as a violation.

    Args:
        reading: The expanded document.
        options: ``forbid_output_drivers`` switches the policy on.

    Returns:
        One ``manuscript.output_drivers`` finding, or an empty list when the policy is off.
    """
    if not options.forbid_output_drivers:
        return []
    declarations, reasons = _package_declarations(reading.document)
    matches, fixed_names, class_reasons = _class_driver_options(reading.document)
    for declaration in declarations:
        names = declaration["names"]
        if names is None or declaration["uncertainty"]:
            continue
        if fixed := fixed_output_drivers(names, declaration["options"] or []):
            matches.append(
                {"path": declaration["path"], "line": declaration["line"], "fixed": fixed}
            )
            fixed_names.extend(fixed)
    return [
        policy_finding(
            _OUTPUT_DRIVERS,
            CheckOutcome(matches, [*reasons, *class_reasons]),
            failure=f"Output drivers fixed at {plural(len(matches), 'declaration')}",
            named=listing("Fixed", fixed_names),
            details={"scope": _SCOPE},
        )
    ]
