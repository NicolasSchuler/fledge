"""Explicit checks over complete build evidence, including transitive packages."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .loaded_options import loaded_options_unavailable
from .models import Finding, PreparationError
from .runtime import BuildResult, ToolRunner
from .scheduler import ResourceBudget, bounded_map, cancellation_point


@dataclass(frozen=True)
class BuildCheckOptions:
    overfull_tolerance_pt: float | None = None
    inventory_loaded_packages: bool = False
    inventory_loaded_options: bool = False
    allowed_loaded_packages: tuple[str, ...] | None = None
    forbidden_loaded_packages: tuple[str, ...] = ()
    required_loaded_packages: tuple[str, ...] = ()
    minimum_package_dates: tuple[tuple[str, str], ...] = ()
    check_local_package_shadows: bool = False

    def __post_init__(self) -> None:
        if self.overfull_tolerance_pt is not None and (
            isinstance(self.overfull_tolerance_pt, bool)
            or not isinstance(self.overfull_tolerance_pt, (int, float))
            or not math.isfinite(self.overfull_tolerance_pt)
            or self.overfull_tolerance_pt < 0
        ):
            raise PreparationError("overfull_tolerance_pt must be a finite nonnegative number")
        for name in (
            "inventory_loaded_packages",
            "inventory_loaded_options",
            "check_local_package_shadows",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
        for names in (
            self.allowed_loaded_packages,
            self.forbidden_loaded_packages,
            self.required_loaded_packages,
        ):
            if names is None:
                continue
            if (
                not isinstance(names, tuple)
                or any(
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:cls|sty)", name)
                    for name in names
                )
                or len(set(names)) != len(names)
            ):
                raise PreparationError(
                    "Loaded package policies require unique literal .sty/.cls filenames"
                )
        if set(self.required_loaded_packages) & set(self.forbidden_loaded_packages):
            raise PreparationError("A loaded package cannot be both required and forbidden")
        if self.allowed_loaded_packages is not None and set(self.required_loaded_packages) - set(
            self.allowed_loaded_packages
        ):
            raise PreparationError(
                "Required packages must be included in the loaded-package allowlist"
            )
        if not isinstance(self.minimum_package_dates, tuple):
            raise PreparationError("minimum_package_dates must contain filename/date pairs")
        seen = set()
        for item in self.minimum_package_dates:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PreparationError("minimum_package_dates must contain filename/date pairs")
            name, value = item
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:cls|sty)", name)
                or name in seen
            ):
                raise PreparationError("Minimum package dates require unique .sty/.cls filenames")
            if not isinstance(value, str) or _date(value) is None:
                raise PreparationError("Minimum package dates must use YYYY-MM-DD or YYYY/MM/DD")
            seen.add(name)


def _date(value: str) -> date | None:
    if not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", value):
        return None
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None


def _finding(
    rule: str,
    message: str,
    *,
    failed: bool = False,
    incomplete: bool = False,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        rule,
        message,
        "error" if failed or incomplete else "info",
        "inconclusive" if incomplete else "failed" if failed else "passed",
        details=details or {},
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
        threshold = options.overfull_tolerance_pt
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
        findings.append(
            _finding(
                "build.overfull_tolerance",
                f"{len(violations)} overfull boxes exceed {threshold:g} TeX pt.",
                failed=bool(violations),
                incomplete=(unreadable or not log_complete) and not violations,
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
        )
    policies = options.allowed_loaded_packages is not None or bool(
        options.forbidden_loaded_packages
        or options.required_loaded_packages
        or options.minimum_package_dates
    )
    if not (policies or options.inventory_loaded_packages):
        return findings
    records = getattr(result, "loaded_packages", [])
    complete = bool(getattr(result, "recorder_complete", False)) and result.success
    names = {record["name"] for record in records}
    if options.inventory_loaded_packages:
        findings.append(
            Finding(
                "build.package_inventory",
                f"{len(names)} class/package filenames were observed in the build recorder.",
                "info",
                "passed" if complete else "inconclusive",
                details={
                    "packages": records,
                    "scope": "Recorder-observed .cls/.sty inputs; header dates are declarations, "
                    "not authenticity checks.",
                },
            )
        )
    if (
        options.allowed_loaded_packages is not None
        or options.forbidden_loaded_packages
        or options.required_loaded_packages
    ):
        forbidden = sorted(names & set(options.forbidden_loaded_packages))
        disallowed = (
            sorted(names - set(options.allowed_loaded_packages))
            if options.allowed_loaded_packages is not None
            else []
        )
        missing = sorted(set(options.required_loaded_packages) - names)
        findings.append(
            _finding(
                "build.loaded_package_policy",
                f"Loaded package policy: {len(forbidden)} forbidden, "
                f"{len(disallowed)} outside allowlist, {len(missing)} required names unobserved.",
                failed=bool(forbidden or disallowed or (missing and complete)),
                incomplete=not complete and not (forbidden or disallowed),
                details={
                    "forbidden": forbidden,
                    "not_allowed": disallowed,
                    "unobserved_required": missing,
                    "recorder_complete": complete,
                },
            )
        )
    for name, minimum in options.minimum_package_dates:
        cancellation_point()
        selected = [record for record in records if record["name"] == name]
        observed = {_date(str(record.get("declared_date", ""))) for record in selected}
        incomplete = not complete or len(observed) != 1 or None in observed
        required_date = _date(minimum)
        earlier = (
            any(
                value is not None and required_date is not None and value < required_date
                for value in observed
            )
            and not incomplete
        )
        observed_text = ", ".join(str(item) for item in sorted(observed, key=str)) or "not loaded"
        findings.append(
            _finding(
                "build.package_date",
                f"{name}: required declared release date at least {minimum}; "
                f"observed {observed_text}.",
                failed=earlier,
                incomplete=incomplete,
                details={
                    "package": name,
                    "minimum_date": minimum,
                    "observed_dates": [
                        str(item) if item else None for item in sorted(observed, key=str)
                    ],
                },
            )
        )
    return findings


async def check_local_package_shadows(
    result: BuildResult, work: Path, runner: ToolRunner, *, budget: ResourceBudget | None = None
) -> list[Finding]:
    """Resolve system counterparts from an empty sandboxed directory, never user cwd."""
    records = [
        item for item in getattr(result, "loaded_packages", []) if item.get("origin") == "project"
    ]
    if not getattr(result, "recorder_complete", False) or not result.success:
        return [
            _finding(
                "build.local_package_shadow",
                "Local package shadow checks need a complete successful recorder trace.",
                incomplete=True,
            )
        ]
    if not records:
        return [_finding("build.local_package_shadow", "No local class/package files were loaded.")]

    async def inspect(record: dict[str, Any]) -> Finding:
        leaf = work / record["name"]
        leaf.mkdir(parents=True)
        try:
            (leaf / "version").mkdir()
            await runner.tool_version(["kpsewhich", "--version"], leaf / "version")
            command_work = leaf / "lookup"
            command_work.mkdir()
            execution = await runner.run(
                ["kpsewhich", "--format=tex", record["name"]],
                cwd=command_work,
                workspace=command_work,
            )
            if (
                execution.timed_out
                or execution.output_limited
                or execution.resource_exceeded
                or execution.returncode not in {0, 1}
            ):
                raise PreparationError("System package lookup did not complete")
            paths = execution.stdout.strip().splitlines()
            if execution.returncode == 1 and not paths:
                return _finding(
                    "build.local_package_shadow",
                    f"{record['name']}: no system counterpart was found.",
                    details={"project_path": record["path"]},
                )
            if len(paths) != 1 or not Path(paths[0]).is_absolute():
                raise PreparationError("System package lookup returned ambiguous evidence")
            return Finding(
                "build.local_package_shadow",
                f"Local {record['path']} shadows a system file of the same name; "
                "review whether that override is intended.",
                "warning",
                "failed",
                path=record["path"],
                evidence="derived",
                details={"system_file": paths[0]},
                suggestion="Compare the local file with the intended toolchain or template.",
            )
        except (PreparationError, OSError) as error:
            return _finding(
                "build.local_package_shadow",
                f"Cannot establish the system counterpart for {record['name']}: {error}",
                incomplete=True,
            )

    # Multiple paths with one basename are themselves ambiguous; do not share a workspace.
    by_name = {record["name"]: record for record in records}
    return await bounded_map(list(by_name.values()), inspect, limit=budget.jobs if budget else 1)
