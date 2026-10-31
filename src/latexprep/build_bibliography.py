"""Bibliography backends in one build: when to prepare Biber, and what the controls require.

Biber must be copied and unpacked before the macOS sandbox may run it, which costs
seconds per job, so an automatic build starts without it unless the sources mention
biblatex (``plan_biber``, ``sources_may_use_biber``). Afterwards the fresh control
files show which backend the document needs, and ``bibliography_policy`` compares that
with the requested strategy while ``verify_backend_tools`` records the versions of the
backends it needs. Reading the control files is ``bibliography_controls``'s work and
assembling the latexmk command is ``latexmk_command``'s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .bibliography_controls import BibliographyEvidence
from .cancellation import run_in_thread
from .command_runner import ToolVersion
from .errors import PreparationError
from .file_safety import read_regular_file
from .findings import Finding
from .latexmk_command import BiberPlan
from .limits import MIB
from .runtime import ToolRunner
from .setting_choices import BibliographyBackend

_BIBER_HINT_SUFFIXES = frozenset(
    {".tex", ".ltx", ".latex", ".sty", ".cls", ".cfg", ".bbx", ".cbx", ".dbx"}
)
# Source bytes read while looking for the word biblatex; more than this answers "yes".
_MAX_BIBER_HINT_BYTES = 64 * MIB
_BIBER_NOT_PREPARED = "not prepared: no source mentions biblatex and the build wrote no .bcf"
BIBER_PREPARED_AFTER_DEFERRAL = (
    "prepared after a build without Biber wrote a .bcf; that build was discarded"
)
_POLICY_RULE = "build.bibliography_backend"
_TOOL_RULE = "build.bibliography_tool"


class BiberRequired(Exception):
    """A build that deferred Biber wrote a fresh .bcf, so Biber is needed after all."""


class BiberSetup(NamedTuple):
    """The Biber executable the build names, and why preparing it failed, if it did.

    Attributes:
        executable: The Biber command the build runs: ``biber`` or the prepared copy.
        error: Why preparing Biber failed, or ``None`` when it did not fail.
    """

    executable: str
    error: str | None


@dataclass(frozen=True, slots=True)
class BackendVerification:
    """The findings about each required backend and the versions that were verified.

    Attributes:
        findings: One finding per backend, passed or inconclusive.
        versions: The verified version of each backend, by backend name.
    """

    findings: list[Finding] = field(default_factory=list)
    versions: dict[str, ToolVersion] = field(default_factory=dict)


def sources_may_use_biber(source: Path, max_bytes: int = _MAX_BIBER_HINT_BYTES) -> bool:
    """Return whether any TeX source mentions biblatex; unreadable or oversized input answers yes.

    A literal hint, so mentions in comments count. A miss is safe because a
    deferred build that writes a fresh .bcf is rebuilt with Biber prepared. Only
    regular files with a TeX source, class, package, configuration or biblatex style
    suffix are read; links are skipped.

    Args:
        source: The directory searched recursively.
        max_bytes: The most source bytes read in total.

    Returns:
        Whether a file holds the word ``biblatex``, or ``True`` when reading fails or the
        sources exceed ``max_bytes``.
    """
    total = 0
    try:
        for path in source.rglob("*"):
            if (
                path.suffix.lower() not in _BIBER_HINT_SUFFIXES
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            data = read_regular_file(path, max_bytes - total)
            total += len(data)
            if b"biblatex" in data:
                return True
    except OSError:
        return True
    return False


async def plan_biber(runner: ToolRunner, backend: BibliographyBackend, source: Path) -> BiberPlan:
    """Decide whether the first build may start without Biber.

    Only an automatic strategy defers, only where preparing Biber is costly, and only
    when no source mentions biblatex. The sources are scanned off the event loop.

    Args:
        runner: The runner that tells whether preparing Biber is still costly.
        backend: The requested bibliography strategy.
        source: The source directory scanned for mentions of biblatex.

    Returns:
        The plan, with the note that Biber was not prepared when the build defers it.
    """
    defer = (
        backend == "auto"
        and runner.biber_needs_preparation
        and not await run_in_thread(sources_may_use_biber, source)
    )
    return BiberPlan(deferred=defer, note=_BIBER_NOT_PREPARED if defer else None)


async def prepare_biber(
    backend: BibliographyBackend, runner: ToolRunner, biber: BiberPlan, root: Path
) -> BiberSetup:
    """Prepare Biber once per runner, unless this build cannot use it.

    A failure is returned, not raised, because an automatic build may never need
    Biber at all; the bibliography-tool check reports it if Biber turns out to be needed.

    Args:
        backend: The requested bibliography strategy; ``bibtex`` prepares nothing.
        runner: The runner that prepares Biber.
        biber: The build's Biber plan; a deferred plan prepares nothing.
        root: The directory passed to :meth:`ToolRunner.prepare_biber`.

    Returns:
        The executable to name, ``biber`` unless a prepared copy replaces it, and the
        preparation error, if any.
    """
    if backend == "bibtex" or biber.deferred:
        return BiberSetup("biber", None)
    try:
        prepared = await runner.prepare_biber(root)
    except PreparationError as error:
        return BiberSetup("biber", str(error))
    return BiberSetup("biber" if prepared is None else str(prepared), None)


def bibliography_policy(
    evidence: BibliographyEvidence, requested: BibliographyBackend, main: str
) -> Finding:
    """Compare the backends the fresh control files require with the requested strategy.

    Args:
        evidence: What the fresh control files show.
        requested: The requested bibliography strategy.
        main: The main document, the finding's path.

    Returns:
        A failed error finding when a required backend lies outside a non-automatic
        strategy, an inconclusive error finding when the evidence is uncertain, and
        otherwise an informational finding that passed, or is not applicable when no
        backend was observed.
    """
    mismatch = requested != "auto" and any(
        backend != requested for backend in evidence.observed_backends
    )
    uncertain = bool(evidence.uncertainty)
    if mismatch:
        status = "failed"
    elif uncertain:
        status = "inconclusive"
    else:
        status = "passed" if evidence.observed_backends else "not_applicable"
    return Finding(
        _POLICY_RULE,
        (
            "Fresh bibliography controls require a backend outside the selected strategy."
            if mismatch
            else "Checked the bibliography backend against fresh build control files."
        ),
        severity="error" if mismatch or uncertain else "info",
        status=status,
        path=main,
        details={"requested_backend": requested, **evidence.to_details()},
    )


def backends_to_verify(
    requested: BibliographyBackend, evidence: BibliographyEvidence
) -> tuple[str, ...]:
    """Select the backends the control files require and the requested strategy permits.

    Args:
        requested: The requested bibliography strategy.
        evidence: What the fresh control files show.

    Returns:
        The observed backends, all of them for ``auto`` and otherwise only the requested
        one.
    """
    return tuple(name for name in evidence.observed_backends if requested in {"auto", name})


async def verify_backend_tools(
    runner: ToolRunner, work: Path, main: str, backends: tuple[str, ...], biber_error: str | None
) -> BackendVerification:
    """Verify each backend in ``backends`` under isolation and record its version.

    Args:
        runner: The runner that probes each tool's version.
        work: The workspace the probes run in.
        main: The main document, the path of each finding.
        backends: The backends to verify; any name other than ``biber`` is probed as
            ``bibtex``.
        biber_error: Why preparing Biber failed; when set, Biber is reported as
            inconclusive without a probe.

    Returns:
        A passed finding and a version for each backend that ran, and an inconclusive
        error finding for each that could not.
    """
    verification = BackendVerification()
    for name in backends:
        version_command = (
            ["biber", "--noconf", "--version"] if name == "biber" else ["bibtex", "--version"]
        )
        try:
            if name == "biber" and biber_error is not None:
                raise PreparationError(
                    f"Cannot run biber under restricted execution: {biber_error}"
                )
            version = await runner.tool_version(version_command, workspace=work)
        except PreparationError as error:
            verification.findings.append(
                Finding(
                    _TOOL_RULE,
                    str(error),
                    severity="error",
                    status="inconclusive",
                    path=main,
                    details={"backend": name},
                )
            )
            continue
        verification.versions[name] = version
        verification.findings.append(
            Finding(
                _TOOL_RULE,
                f"Verified the locally available {name} version in isolation.",
                severity="info",
                status="passed",
                path=main,
                details={"backend": name, "version": version.to_report()},
            )
        )
    return verification
