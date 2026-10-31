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
    """Check that a setting holds a declared release date.

    Args:
        name: The setting name, for the error.
        value: The value to check.

    Raises:
        SettingsError: The value is not a string date written YYYY-MM-DD or YYYY/MM/DD.
    """
    if not isinstance(value, str) or parse_declared_date(value) is None:
        raise SettingsError(f"{name} must be a date written YYYY-MM-DD or YYYY/MM/DD", (name,))


@dataclass(frozen=True)
class BuildCheckOptions:
    """The opt-in checks over build evidence and their explicit policies.

    Construction raises ``SettingsError`` when a value has the wrong type or is out of
    bounds, a filename is not a literal .sty or .cls name or is repeated, a date is not
    written YYYY-MM-DD or YYYY/MM/DD, a package is both required and forbidden, or a
    required package is missing from the allowlist.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to
            ``overfull_tolerance_pt`` and ``expected_texlive_year``.
        overfull_tolerance_pt: The largest overfull box allowed, in TeX points, or
            ``None`` to skip the tolerance check.
        inventory_loaded_packages: Whether to report every class and package file the
            build recorder observed.
        inventory_loaded_options: Whether the options of loaded packages are inventoried;
            a build that collected no option evidence then gets an unavailable finding.
        allowed_loaded_packages: The class and package filenames the build may load, or
            ``None`` for no allowlist.
        forbidden_loaded_packages: The class and package filenames the build must not
            load.
        required_loaded_packages: The class and package filenames the build must load.
        minimum_package_dates: Pairs of a class or package filename and the earliest
            release date its header may declare.
        check_local_package_shadows: Whether to look up a system counterpart of every
            loaded local class or package.
        expected_texlive_year: The TeX Live release the build should use, or ``None`` to
            only record the distribution.
    """

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
    """Build a configured build policy result; anything but a pass blocks as an error.

    Args:
        rule: The rule name.
        message: The message.
        status: The status; ``passed`` gives an informational finding.
        details: The details, or ``None`` for none.

    Returns:
        The finding.
    """
    return Finding(
        rule,
        message,
        severity="info" if status == "passed" else "error",
        status=status,
        details=details or {},
    )


class _UnresolvedAfterTeXError(NamedTuple):
    """A log warning that only means "not resolved yet" once a TeX error stopped the build.

    Attributes:
        rule: The rule of the log warning.
        noun: The capitalised word that names what was not resolved, for messages.
        keys_field: The details entry that lists the unresolved keys.
        key_pattern: The expression whose first group is the key in a log message.
        unreached: What the build stopped before, the end of the qualified message.
    """

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
    """Qualify the citation and reference warnings of build-log findings after a TeX error.

    TeX reports every citation and cross-reference as undefined until a later
    pass has read the bibliography backend's output and the .aux labels. When
    the build stopped on a TeX error (BLD009), those warnings say nothing about
    whether the keys are defined, so each kind becomes one inconclusive result
    that points at the error.

    Args:
        findings: The build-log findings.
        build_succeeded: Whether the build succeeded; a successful build is left alone.

    Returns:
        A new list of the findings, in which each kind of failed warning is merged into
        one inconclusive warning when the build failed with a failed TeX error.
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
    """Replace the failed warnings of one kind with one inconclusive warning.

    Args:
        findings: The build-log findings.
        kind: The kind of warning to merge.

    Returns:
        The findings with the merged warning, which lists the keys and log messages, in
        place of the first failed warning; unchanged when none failed.
    """
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

    Args:
        result: The build whose engine version banner is read.
        options: The build check options, of which ``expected_texlive_year`` applies;
            ``None`` for the defaults.

    Returns:
        The finding: inconclusive when the banner is missing or names no TeX Live or
        MiKTeX release, failed when the release differs from the expected year, and
        passed otherwise.
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
    """Run the configured checks that need only the build result.

    Args:
        result: The finished build.
        options: The build check options; ``None`` for the defaults, which run no check.

    Returns:
        In this order and as configured: the unavailable loaded-option evidence, the
        overfull tolerance, the package inventory, the loaded package policy and one
        finding per minimum package date.
    """
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
    """Return whether the build succeeded with a complete recorder trace."""
    return result.recorder_complete and result.success


def _overfull_tolerance(result: BuildResult, threshold: float) -> Finding:
    """Compare every overfull box in the final log with the tolerance.

    Args:
        result: The finished build.
        threshold: The largest overflow allowed, in TeX points.

    Returns:
        A failed finding when a box exceeds ``threshold``; otherwise inconclusive when a
        box has no measurement or the log is incomplete, and passed when neither holds.
        The details list every measurement.
    """
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
    """Report every class and package file the build recorder observed.

    Args:
        result: The finished build.

    Returns:
        An informational finding, passed when the recorder trace is complete and
        inconclusive otherwise.
    """
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
    """Apply the forbidden, allowed and required package sets to the observed packages.

    Args:
        result: The finished build.
        options: The build check options that hold the package sets.

    Returns:
        A failed finding for a forbidden or disallowed package, even in a partial build;
        otherwise inconclusive without a complete recorder trace, failed when a required
        package was not observed, and passed when all hold.
    """
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
    """Compare the declared release date of one loaded package with a minimum.

    Args:
        result: The finished build.
        name: The class or package filename.
        minimum: The earliest date allowed, as configured.

    Returns:
        An inconclusive finding unless the recorder trace is complete and the loaded
        files of that name declare exactly one readable date; otherwise failed when the
        date is earlier than ``minimum`` and passed when it is not.
    """
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
    """Resolve system counterparts from an empty sandboxed directory, never user cwd.

    Args:
        result: The finished build, whose loaded project classes and packages are looked
            up.
        work: The directory that receives a new lookup folder per package.
        runner: The runner of ``kpsewhich``.
        budget: The resource budget whose CPU slots bound the concurrent lookups; one at
            a time when ``None``.

    Returns:
        One finding per local file name, or one finding alone: inconclusive without a
        complete recorder trace, or passed when no local class or package was loaded.
    """
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
        """Look up one local file name; a name several loaded files share is inconclusive."""
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
    """Return the system paths ``kpsewhich`` finds for ``name``; empty when it finds none.

    Args:
        name: The class or package filename.
        leaf: A new directory in which the version probe and the lookup each get their
            own folder.
        runner: The runner of ``kpsewhich``.

    Returns:
        One absolute path, or no path when ``kpsewhich`` finds none.

    Raises:
        PreparationError: The lookup did not complete within its limits, exited with a
            status other than 0 or 1, or returned several paths or a relative one.
        OSError: A lookup folder cannot be created.
    """
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
    """Report whether a loaded local class or package shadows a system file.

    Args:
        package: The loaded local file.
        work: The directory that receives the new lookup folder named after the file.
        runner: The runner of ``kpsewhich``.

    Returns:
        A passed finding when no system counterpart exists, a failed warning when one
        does, and an inconclusive finding when the lookup fails.
    """
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
