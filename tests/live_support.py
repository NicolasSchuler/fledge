"""Shared support for the opt-in live tests, which run Fledge with the real tools.

Every ``tests/test_integration_*.py`` module imports from here instead of defining its own
settings, report summaries, PNG builder, tool probes and temporary base directory. The
module holds no test of its own and runs no tool at import time; ``tests/test_live_support.py``
checks the TeX-free parts.

A live outcome assertion is :meth:`LiveCaseSupport.assert_outcome`. When it fails, its message
lists every unresolved finding with its code, rule name, stage, status, message and
``details.uncertainty``, because a bare outcome does not say which check could not decide.
If the environment variable ``FLEDGE_TEST_ARTIFACT_DIR`` names a directory, it also saves the
whole report there as ``<test id>.json`` before raising, so a continuous-integration run can
upload what a failed job saw.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unicodedata
import unittest
import zlib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Literal

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobCommand, JobRequest
from latexprep.report import Outcome, Report

# The fixed build date of every live job, so that PDFs and archives repeat byte for byte.
LIVE_BUILD_EPOCH = 1_700_000_000
# Other live builds may share the host, LuaLaTeX starts slowly and Biber preparation alone
# takes about ten seconds, so a job gets the documented default deadline; no live test
# asserts how long a step took.
LIVE_JOB_TIMEOUT_SECONDS = 600
# The outcomes that publish a bundle (docs/reports.md, "Exit codes").
PASSING_OUTCOMES = frozenset({Outcome.PASSED, Outcome.PASSED_WITH_ADVISORIES})
# The quick start's example: nested sources that cite knuth1984 from a BibTeX database.
NESTED_PAPER = Path(__file__).resolve().parents[1] / "examples" / "nested-paper"
# The environment variable that names the directory for the reports of failed assertions.
ARTIFACT_DIRECTORY_VARIABLE = "FLEDGE_TEST_ARTIFACT_DIR"

# The most findings a failure message lists; the rest are counted.
_MAX_LISTED_FINDINGS = 40
_UNSAFE_FILE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILE_NAME_CHARACTERS = 160
# The artifacts this process has written, so that a test that fails several times (in a
# loop of subtests) keeps every report instead of overwriting the first.
_written_artifacts: set[Path] = set()


def live_settings(**options: Any) -> Settings:
    """Return the settings of a live job on ``main.tex`` with the fixed date and deadline.

    Args:
        **options: Settings that replace or add to the defaults of this function, such as
            ``layout="flat"`` or ``main="paper.tex"``.

    Returns:
        The settings, with ``main="main.tex"``, ``source_date_epoch=LIVE_BUILD_EPOCH`` and
        ``job_timeout_seconds=LIVE_JOB_TIMEOUT_SECONDS`` unless ``options`` say otherwise.
    """
    values: dict[str, Any] = {
        "main": "main.tex",
        "source_date_epoch": LIVE_BUILD_EPOCH,
        "job_timeout_seconds": LIVE_JOB_TIMEOUT_SECONDS,
    }
    values.update(options)
    return Settings(**values)


def run_job_sync(request: JobRequest) -> Report:
    """Run one job to its end on a new event loop, as the command line does.

    Args:
        request: The job to run.

    Returns:
        The report of the job.
    """
    return asyncio.run(run_job(request))


def _report_data(report: Report | Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a report in its JSON layout, whether it is a ``Report`` or already parsed JSON."""
    return report if isinstance(report, Mapping) else report.to_dict()


def _is_unresolved(finding: Mapping[str, Any]) -> bool:
    """Return whether a finding did not pass and is not an informational note.

    An incomplete or inconclusive finding counts at any severity: it names evidence that is
    missing, which is what explains an unexpected outcome.
    """
    if finding.get("status") == "passed":
        return False
    return (
        finding.get("severity") != "info"
        or bool(finding.get("incomplete"))
        or finding.get("status") == "inconclusive"
    )


def _uncertainty_text(details: object) -> str | None:
    """Return the text of ``details["uncertainty"]``, or ``None`` when the finding has none."""
    if not isinstance(details, Mapping):
        return None
    uncertainty = details.get("uncertainty")
    if uncertainty in (None, "", [], ()):
        return None
    if isinstance(uncertainty, str):
        return uncertainty
    if isinstance(uncertainty, (list, tuple)):
        return "; ".join(str(item) for item in uncertainty)
    return str(uncertainty)


def _finding_lines(finding: Mapping[str, Any]) -> list[str]:
    """Describe one finding on a line, plus a line for its uncertainty when it records one."""
    details = finding.get("details")
    stage = details.get("stage") if isinstance(details, Mapping) else None
    flags = " (incomplete)" if finding.get("incomplete") else ""
    lines = [
        f"  - {finding.get('code') or '-'} {finding.get('rule')} | stage: {stage or '-'} | "
        f"{finding.get('status')}/{finding.get('severity')}{flags} | {finding.get('message')}"
    ]
    if (uncertainty := _uncertainty_text(details)) is not None:
        lines.append(f"      uncertainty: {uncertainty}")
    return lines


def blocking_summary(report: Report | Mapping[str, Any]) -> str:
    """Describe the findings that keep a run from passing, for an assertion message.

    Args:
        report: The report of the job, or its parsed JSON.

    Returns:
        One line per finding that did not pass and is not informational, with its code, rule
        name, stage, status, severity, message and ``details.uncertainty``; a line saying so
        when there is none.
    """
    findings = [item for item in _report_data(report).get("findings", ()) if _is_unresolved(item)]
    if not findings:
        return "No finding is unresolved."
    lines = [f"Unresolved findings ({len(findings)}):"]
    for finding in findings[:_MAX_LISTED_FINDINGS]:
        lines.extend(_finding_lines(finding))
    if len(findings) > _MAX_LISTED_FINDINGS:
        lines.append(f"  ... and {len(findings) - _MAX_LISTED_FINDINGS} more")
    return "\n".join(lines)


def finding_statuses(report: Report, code: str, stage: str | None = None) -> list[str]:
    """List the status of every finding with ``code``; only those of ``stage`` when one is given.

    Args:
        report: The report of the job.
        code: The finding code.
        stage: The stage to select, or ``None`` for every stage.

    Returns:
        The statuses in report order.
    """
    return [
        item.status
        for item in report.findings
        if item.code == code and (stage is None or item.stage == stage)
    ]


def artifact_file_name(test_id: str) -> str:
    """Return a safe file name for the report of the test ``test_id``.

    Args:
        test_id: The dotted id of the test, as ``unittest.TestCase.id`` returns it.

    Returns:
        The id with every run of characters outside letters, digits, dot, underscore and
        hyphen replaced by one underscore, shortened, and ending in ``.json``.
    """
    stem = _UNSAFE_FILE_NAME_CHARACTERS.sub("_", test_id).strip("._")
    return f"{stem[:_MAX_FILE_NAME_CHARACTERS] or 'test'}.json"


def save_report_artifact(test_id: str, report: Report | Mapping[str, Any]) -> Path | None:
    """Write the whole report to the artifact directory, if the environment names one.

    The file is ``$FLEDGE_TEST_ARTIFACT_DIR/<test id>.json``, with the directory created as
    needed. A test that saves several reports keeps each one: a later file gets a numeric
    suffix instead of replacing an earlier one from the same process.

    Args:
        test_id: The dotted id of the test that failed.
        report: The report of the job, or its parsed JSON.

    Returns:
        The path of the file, or ``None`` when ``FLEDGE_TEST_ARTIFACT_DIR`` is unset or empty.

    Raises:
        OSError: The directory or the file could not be written.
    """
    directory = os.environ.get(ARTIFACT_DIRECTORY_VARIABLE)
    if not directory:
        return None
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / artifact_file_name(test_id)
    number = 1
    while path in _written_artifacts:
        number += 1
        path = folder / f"{artifact_file_name(test_id).removesuffix('.json')}-{number}.json"
    _written_artifacts.add(path)
    text = json.dumps(_report_data(report), indent=2, ensure_ascii=False, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def png_bytes(rgb: bytes = b"\xff\x00\x00") -> bytes:
    """Return a valid 1 x 1 RGB PNG whose one pixel has the color bytes ``rgb``.

    Args:
        rgb: The red, green and blue bytes of the pixel.

    Returns:
        The bytes of the PNG file.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0" + rgb))
        + chunk(b"IEND", b"")
    )


def pdf_text(pdf: Path, *, normalization: Literal["NFC", "NFKC"] = "NFC") -> str:
    """Return the text Poppler extracts from ``pdf``, normalized so that letters compare equal.

    Args:
        pdf: The PDF to read.
        normalization: ``"NFC"`` composes accents; ``"NFKC"`` also turns a ligature back into
            its letters, in case a Poppler version extracts the ff of "Off" as U+FB00.

    Returns:
        The text ``pdftotext`` extracts, decoded as UTF-8 and normalized.

    Raises:
        subprocess.CalledProcessError: ``pdftotext`` failed.
    """
    extracted = subprocess.run(
        [shutil.which("pdftotext") or "pdftotext", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return unicodedata.normalize(normalization, extracted.stdout.decode("utf-8"))


def find_tex_file(name: str) -> Path | None:
    """Find the TeX file ``name`` (a package, class, style or font) with ``kpsewhich``.

    Args:
        name: The file name to look up.

    Returns:
        Where ``kpsewhich`` finds the file, or ``None`` when ``kpsewhich`` is not installed or
        does not find it.
    """
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return None
    found = subprocess.run(
        [kpsewhich, name], capture_output=True, text=True, check=False, timeout=60
    )
    location = found.stdout.strip()
    return Path(location) if found.returncode == 0 and location else None


class LiveCaseSupport(unittest.TestCase):
    """What every live test case shares: a scratch directory, tool probes and outcome checks.

    Use :class:`LiveProjectTestCase` or :class:`AsyncLiveProjectTestCase`, which add
    ``prepare``.

    Attributes:
        base: The resolved temporary directory of the current test, created on first use and
            removed when the test ends.
    """

    def scratch_directory(self, topic: str) -> Path:
        """Create a temporary directory named after ``topic``, removed when the test ends.

        Args:
            topic: Words for the directory name, which help to find it while debugging.

        Returns:
            The resolved directory.
        """
        temporary = tempfile.TemporaryDirectory(prefix=f"fledge-{topic}-")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    @functools.cached_property
    def base(self) -> Path:
        """The scratch directory of the test, named after the test module."""
        return self.scratch_directory(
            type(self).__module__.rsplit(".", 1)[-1].removeprefix("test_")
        )

    def require_tools(self, *names: str) -> None:
        """Skip the test unless every one of ``names`` is on ``PATH``.

        Args:
            *names: The program names.
        """
        if missing := [name for name in names if shutil.which(name) is None]:
            self.skipTest(f"Not on PATH: {', '.join(missing)} (see docs/installation.md)")

    def installed_tex_file(self, name: str, install: str) -> Path:
        """Find the TeX file ``name``; skip the test, saying how to install it, when it is absent.

        Args:
            name: The TeX file to look up with ``kpsewhich``.
            install: How to install the file, which the reason for the skip states.

        Returns:
            Where ``kpsewhich`` finds the file.

        Raises:
            unittest.SkipTest: ``kpsewhich`` does not find the file.
        """
        location = find_tex_file(name)
        if location is None:
            self.skipTest(f"kpsewhich does not find {name}: {install}")
        return location

    def copy_nested_paper(self, name: str = "paper") -> Path:
        """Copy ``examples/nested-paper``, a BibTeX project that builds cleanly, to ``name``.

        Args:
            name: The name of the copy in the scratch directory.

        Returns:
            The fresh copy.
        """
        return Path(shutil.copytree(NESTED_PAPER, self.base / name))

    def assert_outcome(
        self,
        report: Report | Mapping[str, Any],
        expected: Outcome | str | Collection[Outcome | str] = PASSING_OUTCOMES,
    ) -> None:
        """Assert that the outcome of ``report`` is one of ``expected``, and explain a failure.

        The failure message lists the unresolved findings (see :func:`blocking_summary`).
        Before it raises, the whole report is saved as ``<test id>.json`` in the directory
        that ``FLEDGE_TEST_ARTIFACT_DIR`` names, if the variable is set.

        Args:
            report: The report of the job, or its parsed JSON.
            expected: The accepted outcome, or the accepted outcomes; the passing outcomes
                by default.

        Raises:
            AssertionError: The outcome is not accepted.
        """
        accepted = (
            {str(expected)} if isinstance(expected, str) else {str(item) for item in expected}
        )
        data = _report_data(report)
        outcome = str(data.get("outcome"))
        if outcome in accepted:
            return
        message = (
            f"Outcome {outcome!r} is not one of {', '.join(sorted(accepted))}.\n"
            f"{blocking_summary(data)}"
        )
        try:
            saved = save_report_artifact(self.id(), data)
        except OSError as error:
            message += f"\nThe report could not be saved: {error}"
        else:
            if saved is not None:
                message += f"\nThe full report is saved in {saved}."
        self.fail(message)


class LiveProjectTestCase(LiveCaseSupport):
    """A synchronous live test case that prepares projects into its scratch directory."""

    def prepare(
        self,
        source: Path,
        settings: Settings | None = None,
        output_name: str = "prepared",
        *,
        expect: Outcome | str | Collection[Outcome | str] | None = PASSING_OUTCOMES,
    ) -> Report:
        """Run ``prepare`` on ``source`` into the new directory ``output_name``.

        Args:
            source: The project folder or ZIP.
            settings: The settings of the job; :func:`live_settings` when ``None``.
            output_name: The name of the output directory in the scratch directory.
            expect: The accepted outcome or outcomes, or ``None`` to accept any.

        Returns:
            The report of the job.
        """
        output = self.base / output_name
        report = run_job_sync(
            JobRequest(JobCommand.PREPARE, source, settings or live_settings(), output)
        )
        if expect is not None:
            self.assert_outcome(report, expect)
        return report


class AsyncLiveProjectTestCase(LiveCaseSupport, unittest.IsolatedAsyncioTestCase):
    """An asynchronous live test case that prepares projects into its scratch directory."""

    async def prepare(
        self,
        source: Path,
        settings: Settings | None = None,
        output_name: str = "prepared",
        *,
        expect: Outcome | str | Collection[Outcome | str] | None = PASSING_OUTCOMES,
    ) -> Report:
        """Run ``prepare`` on ``source`` into the new directory ``output_name``.

        Args:
            source: The project folder or ZIP.
            settings: The settings of the job; :func:`live_settings` when ``None``.
            output_name: The name of the output directory in the scratch directory.
            expect: The accepted outcome or outcomes, or ``None`` to accept any.

        Returns:
            The report of the job.
        """
        output = self.base / output_name
        report = await run_job(
            JobRequest(JobCommand.PREPARE, source, settings or live_settings(), output)
        )
        if expect is not None:
            self.assert_outcome(report, expect)
        return report
