"""Explicit checks over complete build evidence, including transitive packages."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from .build_result import BuildResult
from .cancellation import bounded_map, cancellation_point
from .command_runner import CommandRunner
from .errors import PreparationError, SettingsError
from .findings import EvidenceKind, Finding, Status
from .loaded_options import loaded_options_unavailable
from .option_config import (
    NONNEGATIVE_NUMBER,
    Bounds,
    TextFormat,
    require_pairs,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .package_headers import PACKAGE_FILENAME, LoadedPackage, parse_declared_date
from .resource_budget import ResourceBudget
from .setting_choices import ENGINES

# The first TeX Live release was 1996; the upper bound only rejects an obvious typo.
MIN_TEXLIVE_YEAR = 1996
MAX_TEXLIVE_YEAR = 2100
_SHADOW_RULE = "build.local_package_shadow"
_DISTRIBUTION_RULE = "build.tex_distribution"
_PACKAGE_FILENAME_FORMAT = TextFormat(PACKAGE_FILENAME, "a literal .sty or .cls filename")
_PACKAGE_FILENAME_TEXT = partial(
    require_text, expected=_PACKAGE_FILENAME_FORMAT.description, pattern=PACKAGE_FILENAME
)


def _require_declared_date(name: str, value: object) -> None:
    if not isinstance(value, str) or parse_declared_date(value) is None:
        raise SettingsError(f"{name} must be a date written YYYY-MM-DD or YYYY/MM/DD", (name,))


@dataclass(frozen=True)
class BuildCheckOptions:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "overfull_tolerance_pt": NONNEGATIVE_NUMBER,
        "expected_texlive_year": Bounds(MIN_TEXLIVE_YEAR, MAX_TEXLIVE_YEAR),
    }

    overfull_tolerance_pt: float | None = None
    inventory_loaded_packages: bool = False
    inventory_loaded_options: bool = False
    allowed_loaded_packages: tuple[str, ...] | None = None
    forbidden_loaded_packages: tuple[str, ...] = ()
    required_loaded_packages: tuple[str, ...] = ()
    minimum_package_dates: tuple[tuple[str, str], ...] = ()
    check_local_package_shadows: bool = False
    expected_texlive_year: int | None = None

    def __post_init__(self) -> None:
        validate_scalars(self)
        for name in (
            "allowed_loaded_packages",
            "forbidden_loaded_packages",
            "required_loaded_packages",
        ):
            require_unique_strings(
                name,
                getattr(self, name),
                item_format=_PACKAGE_FILENAME_FORMAT,
                allow_none=name == "allowed_loaded_packages",
            )
        require_pairs(
            "minimum_package_dates",
            self.minimum_package_dates,
            first=_PACKAGE_FILENAME_TEXT,
            second=_require_declared_date,
        )
        if set(self.required_loaded_packages) & set(self.forbidden_loaded_packages):
            raise SettingsError(
                "A loaded package cannot be both required and forbidden",
                ("required_loaded_packages", "forbidden_loaded_packages"),
            )
        if self.allowed_loaded_packages is not None and set(self.required_loaded_packages) - set(
            self.allowed_loaded_packages
        ):
            raise SettingsError(
                "required_loaded_packages must all appear in allowed_loaded_packages",
                ("required_loaded_packages", "allowed_loaded_packages"),
            )


def _finding(
    rule: str, message: str, status: Status, *, details: dict[str, Any] | None = None
) -> Finding:
    """A configured build policy result; anything but a pass blocks as an error."""
    return Finding(
        rule,
        message,
        severity="info" if status == "passed" else "error",
        status=status,
        details=details or {},
    )


class _UnresolvedAfterTeXError(NamedTuple):
    """A log warning that only means "not resolved yet" once a TeX error stopped the build."""

    rule: str
    noun: str
    keys_field: str
    key_pattern: re.Pattern[str]
    unreached: str


_UNRESOLVED_AFTER_TEX_ERROR = (
    _UnresolvedAfterTeXError(
        "build.undefined_citation",
        "Citation",
        "citation_keys",
        re.compile(r"Citation [`']([^'`]+)'"),
        "the bibliography was resolved",
    ),
    _UnresolvedAfterTeXError(
        "build.undefined_reference",
        "Reference",
        "reference_keys",
        re.compile(r"Reference [`']([^'`]+)'"),
        "labels were resolved",
    ),
)


def qualify_citation_findings(findings: list[Finding], *, build_succeeded: bool) -> list[Finding]:
    """Build-log findings, with citation and reference warnings qualified after a TeX error.

    TeX reports every citation and cross-reference as undefined until a later
    pass has read the bibliography backend's output and the .aux labels. When
    the build stopped on a TeX error (BLD009), those warnings say nothing about
    whether the keys are defined, so each kind becomes one inconclusive result
    that points at the error.
    """
    findings = list(findings)
    stopped = not build_succeeded and any(
        item.rule == "build.tex_error" and item.status == "failed" for item in findings
    )
    if not stopped:
        return findings
    for kind in _UNRESOLVED_AFTER_TEX_ERROR:
        findings = _merge_unresolved(findings, kind)
    return findings


def _merge_unresolved(findings: list[Finding], kind: _UnresolvedAfterTeXError) -> list[Finding]:
    unresolved = [item for item in findings if item.rule == kind.rule and item.status == "failed"]
    if not unresolved:
        return findings
    first = unresolved[0]
    noun = kind.noun
    keys = list(
        dict.fromkeys(
            found[1] for item in unresolved if (found := kind.key_pattern.search(item.message))
        )
    )
    listed = ", ".join(repr(key) for key in keys[:3]) + (
        f" (+{len(keys) - 3} more)" if len(keys) > 3 else ""
    )
    qualified = Finding(
        kind.rule,
        f"{noun} resolution was not checked"
        + (f" for {listed}" if keys else "")
        + f": the build stopped on a TeX error (BLD009) before {kind.unreached}.",
        severity="warning",
        status="inconclusive",
        path=first.path,
        line=first.line,
        suggestion=f"Fix the TeX error reported as BLD009 and rebuild; {noun.lower()}s are "
        "then checked again.",
        details={
            **first.details,
            "blocked_by": "BLD009",
            kind.keys_field: keys,
            "log_messages": [item.message for item in unresolved],
        },
    )
    merged = {id(item) for item in unresolved[1:]}
    return [qualified if item is first else item for item in findings if id(item) not in merged]


_TEXLIVE = re.compile(r"\(TeX Live (\d{4})(?:/([^)]*))?\)")
_MIKTEX = re.compile(r"\(MiKTeX ([0-9][^)\s]*)\)")


def check_tex_distribution(
    result: BuildResult, options: BuildCheckOptions | None = None
) -> Finding:
    """Record the engine's TeX distribution from the build's single version probe.

    The observation is informational. Only an explicit expected TeX Live year turns
    a different or unidentified distribution into an advisory; unavailable version
    evidence stays inconclusive and never passes that policy.
    """
    options = options or BuildCheckOptions()
    expected = options.expected_texlive_year
    versions = result.tools.versions
    engine = next((name for name in ENGINES if name in versions), None)
    banner = versions[engine].version[0] if engine and versions[engine].version else None
    details: dict[str, Any] = {"engine": engine, "version_banner": banner}
    if expected is not None:
        details["expected_texlive_year"] = expected
    policy = (
        f"; expected TeX Live {expected}"
        if expected is not None
        else ". Services that compile your sources, such as arXiv, use their own TeX Live "
        "release, so package behaviour can differ; set build_checks.expected_texlive_year "
        "to compare"
    )
    if banner is None:
        return Finding(
            _DISTRIBUTION_RULE,
            "The TeX distribution is unknown: the build recorded no engine version output"
            + (f"; expected TeX Live {expected} cannot be confirmed." if expected else "."),
            severity="warning" if expected is not None else "info",
            status="inconclusive",
            details=details,
        )
    texlive = _TEXLIVE.search(banner)
    miktex = _MIKTEX.search(banner)
    if texlive:
        year = int(texlive.group(1))
        details.update(distribution="TeX Live", texlive_year=year, packager=texlive.group(2))
        name = f"TeX Live {year}" + (f" ({texlive.group(2)})" if texlive.group(2) else "")
    elif miktex:
        details.update(distribution="MiKTeX", miktex_version=miktex.group(1))
        name, year = f"MiKTeX {miktex.group(1)}", None
    else:
        return Finding(
            _DISTRIBUTION_RULE,
            f"The TeX distribution could not be identified from {engine} output{policy}.",
            severity="warning" if expected is not None else "info",
            status="inconclusive",
            details=details,
        )
    mismatch = expected is not None and year != expected
    return Finding(
        _DISTRIBUTION_RULE,
        f"Built with {name} via {engine}{policy}.",
        severity="warning" if mismatch else "info",
        status="failed" if mismatch else "passed",
        evidence_kind=EvidenceKind.DIRECT,
        details=details,
    )


def check_build_details(
    result: BuildResult, options: BuildCheckOptions | None = None
) -> list[Finding]:
    options = options or BuildCheckOptions()
    findings: list[Finding] = []
    if options.inventory_loaded_options and not any(
        finding.rule == "build.loaded_options" for finding in result.findings
    ):
        findings.append(
            loaded_options_unavailable("The build did not collect kernel option evidence", "")
        )
    if options.overfull_tolerance_pt is not None:
        findings.append(_overfull_tolerance(result, options.overfull_tolerance_pt))
    if options.inventory_loaded_packages:
        findings.append(_package_inventory(result))
    if (
        options.allowed_loaded_packages is not None
        or options.forbidden_loaded_packages
        or options.required_loaded_packages
    ):
        findings.append(_loaded_package_policy(result, options))
    for name, minimum in options.minimum_package_dates:
        cancellation_point()
        findings.append(_package_date(result, name, minimum))
    return findings


def _recorder_complete(result: BuildResult) -> bool:
    return result.recorder_complete and result.success


def _overfull_tolerance(result: BuildResult, threshold: float) -> Finding:
    overflows = [finding for finding in result.findings if finding.rule == "build.overfull_box"]
    unreadable = any("overflow_pt" not in finding.details for finding in overflows)
    log_complete = result.success and not any(
        finding.rule == "build.log_unavailable" for finding in result.findings
    )
    violations = [
        finding
        for finding in overflows
        if isinstance(overflow := finding.details.get("overflow_pt"), (int, float))
        and overflow > threshold
    ]
    if violations:
        status: Status = "failed"
    else:
        status = "inconclusive" if unreadable or not log_complete else "passed"
    return _finding(
        "build.overfull_tolerance",
        f"{len(violations)} overfull boxes exceed {threshold:g} TeX pt.",
        status,
        details={
            "tolerance_tex_pt": threshold,
            "measurements": [
                {
                    "path": item.path,
                    "line": item.line,
                    "overflow_tex_pt": item.details.get("overflow_pt"),
                    "direction": item.details.get("direction"),
                }
                for item in overflows
            ],
        },
    )


def _package_inventory(result: BuildResult) -> Finding:
    names = {package.name for package in result.loaded_packages}
    return Finding(
        "build.package_inventory",
        f"{len(names)} class/package filenames were observed in the build recorder.",
        severity="info",
        status="passed" if _recorder_complete(result) else "inconclusive",
        details={
            "packages": [asdict(package) for package in result.loaded_packages],
            "scope": "Recorder-observed .cls/.sty inputs; header dates are declarations, "
            "not authenticity checks.",
        },
    )


def _loaded_package_policy(result: BuildResult, options: BuildCheckOptions) -> Finding:
    complete = _recorder_complete(result)
    names = {package.name for package in result.loaded_packages}
    forbidden = sorted(names & set(options.forbidden_loaded_packages))
    disallowed = (
        sorted(names - set(options.allowed_loaded_packages))
        if options.allowed_loaded_packages is not None
        else []
    )
    missing = sorted(set(options.required_loaded_packages) - names)
    # Directly observed forbidden input is evidence even in a partial build; a
    # required package can only be reported missing from a complete one.
    if forbidden or disallowed:
        status: Status = "failed"
    elif not complete:
        status = "inconclusive"
    else:
        status = "failed" if missing else "passed"
    return _finding(
        "build.loaded_package_policy",
        f"Loaded package policy: {len(forbidden)} forbidden, "
        f"{len(disallowed)} outside allowlist, {len(missing)} required names unobserved.",
        status,
        details={
            "forbidden": forbidden,
            "not_allowed": disallowed,
            "unobserved_required": missing,
            "recorder_complete": complete,
        },
    )


def _package_date(result: BuildResult, name: str, minimum: str) -> Finding:
    observed = {
        parse_declared_date(package.declared_date or "")
        for package in result.loaded_packages
        if package.name == name
    }
    required_date = parse_declared_date(minimum)
    if not _recorder_complete(result) or len(observed) != 1 or None in observed:
        status: Status = "inconclusive"
    elif any(
        value is not None and required_date is not None and value < required_date
        for value in observed
    ):
        status = "failed"
    else:
        status = "passed"
    observed_text = ", ".join(str(item) for item in sorted(observed, key=str)) or "not loaded"
    return _finding(
        "build.package_date",
        f"{name}: required declared release date at least {minimum}; observed {observed_text}.",
        status,
        details={
            "package": name,
            "minimum_date": minimum,
            "observed_dates": [str(item) if item else None for item in sorted(observed, key=str)],
        },
    )


async def check_local_package_shadows(
    result: BuildResult, work: Path, runner: CommandRunner, *, budget: ResourceBudget | None = None
) -> list[Finding]:
    """Resolve system counterparts from an empty sandboxed directory, never user cwd."""
    if not _recorder_complete(result):
        return [
            _finding(
                _SHADOW_RULE,
                "Local package shadow checks need a complete successful recorder trace.",
                "inconclusive",
            )
        ]
    by_name: dict[str, list[LoadedPackage]] = {}
    for package in result.loaded_packages:
        if package.origin == "project":
            by_name.setdefault(package.name, []).append(package)
    if not by_name:
        return [_finding(_SHADOW_RULE, "No local class/package files were loaded.", "passed")]

    async def inspect(name: str) -> Finding:
        packages = by_name[name]
        if len(packages) > 1:
            # One lookup cannot tell which of several same-named local files a
            # system counterpart shadows, and they must not share a workspace.
            return _finding(
                _SHADOW_RULE,
                f"Cannot establish the system counterpart for {name}: "
                f"{len(packages)} loaded local files share this name.",
                "inconclusive",
                details={"project_paths": [package.path for package in packages]},
            )
        return await _local_package_shadow(packages[0], work, runner)

    return await bounded_map(list(by_name), inspect, limit=budget.cpu_slots if budget else 1)


async def _system_lookup(name: str, leaf: Path, runner: CommandRunner) -> list[str]:
    """The system paths ``kpsewhich`` finds for ``name``; empty when it finds none."""
    (leaf / "version").mkdir()
    await runner.tool_version(["kpsewhich", "--version"], leaf / "version")
    command_work = leaf / "lookup"
    command_work.mkdir()
    execution = await runner.run(
        ["kpsewhich", "--format=tex", name], cwd=command_work, workspace=command_work
    )
    # kpsewhich exits with status 1 when it finds no file.
    if not execution.within_limits or execution.returncode not in {0, 1}:
        raise PreparationError(
            f"System package lookup did not complete: {execution.failure_reason('kpsewhich')}"
        )
    paths = execution.stdout.strip().splitlines()
    if execution.returncode == 1 and not paths:
        return []
    if len(paths) != 1 or not Path(paths[0]).is_absolute():
        raise PreparationError("System package lookup returned ambiguous evidence")
    return paths


async def _local_package_shadow(
    package: LoadedPackage, work: Path, runner: CommandRunner
) -> Finding:
    leaf = work / package.name
    leaf.mkdir(parents=True)
    try:
        paths = await _system_lookup(package.name, leaf, runner)
    except (PreparationError, OSError) as error:
        return _finding(
            _SHADOW_RULE,
            f"Cannot establish the system counterpart for {package.name}: {error}",
            "inconclusive",
        )
    if not paths:
        return _finding(
            _SHADOW_RULE,
            f"{package.name}: no system counterpart was found.",
            "passed",
            details={"project_path": package.path},
        )
    return Finding(
        _SHADOW_RULE,
        f"Local {package.path} shadows a system file of the same name; "
        "review whether that override is intended.",
        severity="warning",
        status="failed",
        path=package.path,
        evidence_kind=EvidenceKind.DERIVED,
        details={"system_file": paths[0]},
        suggestion="Compare the local file with the intended toolchain or template.",
    )
