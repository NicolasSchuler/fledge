#!/usr/bin/env python3
"""Opt-in, time-bounded real-tool benchmark of the complete preparation workflow."""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
import statistics
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click
from rich.console import Console

from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Report

SOURCE_DATE_EPOCH = 1_700_000_000
CONFIGURATIONS = {"serial": (1, 1), "parallel": (4, 2)}
OBSERVED_METRICS = (
    "peak_total_rss_mb",
    "peak_parent_rss_mb",
    "peak_tool_rss_mb",
    "peak_command_rss_mb",
    "peak_temp_bytes",
    "peak_active_commands",
    "monitor_seconds",
    "commands",
    "version_probes",
)
_JOB_DIRECTORY = re.compile(r"/(?:[^/\s\"']+/)*latex-prep-[^/\s\"']+")
console = Console(stderr=True, markup=False, highlight=False)


@dataclass
class Corpus:
    name: str
    path: Path
    expected_pages: int
    original: dict[str, bytes]


@dataclass
class ExpectedResult:
    evidence: dict[str, Any]
    archive: bytes
    sources: dict[str, bytes]


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_figure(path: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    width = height = 128
    pixels = b"".join(
        b"\0" + bytes(channel for x in range(width) for channel in (2 * x, 2 * y, 128))
        for y in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _corpora(root: Path) -> list[Corpus]:
    paragraph = (
        "This neutral synthetic article exercises local document preparation. "
        "Its repeated prose supplies stable text for exact comparison while the "
        "source is formatted, packaged, extracted, and rebuilt. "
    )
    small = root / "single-file"
    small.mkdir()
    (small / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\section{Synthetic article}\n" + paragraph * 4 + "\n\\end{document}\n",
        encoding="utf-8",
    )
    multiple = root / "multiple-files"
    (multiple / "sections").mkdir(parents=True)
    (multiple / "figures").mkdir()
    _write_figure(multiple / "figures/pattern.png")
    pages = 6
    for page in range(1, pages + 1):
        (multiple / "sections" / f"part{page:02d}.tex").write_text(
            f"\\section{{Synthetic section {page}}}\n"
            + paragraph * 3
            + "\n\n\\begin{center}\n"
            + "\\includegraphics[width=3cm]{figures/pattern.png}\n"
            + "\\end{center}\n"
            + ("\\newpage\n" if page < pages else ""),
            encoding="utf-8",
        )
    (multiple / "main.tex").write_text(
        "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
        + "".join(f"\\input{{sections/part{page:02d}}}\n" for page in range(1, pages + 1))
        + "\\end{document}\n",
        encoding="utf-8",
    )
    return [
        Corpus("single-file", small, 1, _files(small)),
        Corpus("multiple-files", multiple, pages, _files(multiple)),
    ]


def _normalized(value: Any, benchmark_root: Path) -> Any:
    """Keep scientific/check evidence; replace only disposable workspace paths."""
    if isinstance(value, dict):
        return {key: _normalized(item, benchmark_root) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_normalized(item, benchmark_root) for item in value]
    if isinstance(value, str):
        return _JOB_DIRECTORY.sub("<job>", value.replace(str(benchmark_root), "<benchmark>"))
    return value


def _evidence(report: Report, root: Path) -> dict[str, Any]:
    serialized = report.to_dict()
    # Timing/queue statistics and runtime settings are retained in run records,
    # not compared as findings. Tool versions and check stage labels stay here.
    return _normalized(
        {
            "outcome": serialized["outcome"],
            "findings": serialized["findings"],
            "changes": serialized["changes"],
            "tools": serialized["tools"],
            "stages": [
                {
                    key: value
                    for key, value in stage.items()
                    if key not in {"elapsed_seconds", "queue_seconds"}
                }
                for stage in report.stages
            ],
        },
        root,
    )


def _settings(configuration: str, remaining: float) -> Settings:
    jobs, render_jobs = CONFIGURATIONS[configuration]
    return Settings(
        main="main.tex",
        format=True,
        jobs=jobs,
        build_jobs=1,
        render_jobs=render_jobs,
        memory_mb=2048,
        build_memory_mb=1024,
        timeout_seconds=30,
        job_timeout_seconds=max(1, min(60, math.floor(remaining))),
        source_date_epoch=SOURCE_DATE_EPOCH,
    )


async def _run_once(
    corpus: Corpus,
    configuration: str,
    phase: str,
    repetition: int,
    root: Path,
    deadline: float,
    expected: dict[str, ExpectedResult],
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise TimeoutError("The finite benchmark admission budget is exhausted")
    settings = _settings(configuration, remaining)
    output = root / f"output-{corpus.name}-{configuration}-{phase}-{repetition}"
    report = Report("prepare")
    started = time.monotonic()
    failure = None
    try:
        async with asyncio.timeout(remaining):
            await run_job(JobRequest("prepare", corpus.path, settings, output), report=report)
    except (Exception, asyncio.CancelledError) as error:
        failure = f"{type(error).__name__}: {error}"
    elapsed = time.monotonic() - started
    evidence = _evidence(report, root)
    checks: dict[str, bool] = {"original_sources_unchanged": _files(corpus.path) == corpus.original}
    if failure is None and report.outcome in {"passed", "passed_with_advisories"}:
        try:
            archive = (output / "submission.zip").read_bytes()
            sources = _files(output / "sources")
        except OSError as error:
            failure = f"Successful report lacked readable published artifacts: {error}"
        else:
            if corpus.name not in expected:
                expected[corpus.name] = ExpectedResult(evidence, archive, sources)
            previous = expected[corpus.name]
            measured_pages = [
                finding.details["pages"]
                for finding in report.findings
                if finding.rule == "pdf.pages"
            ]
            checks.update(
                normalized_evidence_equal=evidence == previous.evidence,
                archive_bytes_equal=archive == previous.archive,
                prepared_source_bytes_equal=sources == previous.sources,
                measured_page_count_equal=measured_pages == [corpus.expected_pages] * 2,
            )
            if not all(checks.values()):
                failure = "Correctness equivalence failed: " + ", ".join(
                    name for name, passed in checks.items() if not passed
                )
    else:
        failure = failure or f"Preparation outcome was {report.outcome}"
    execution = report.execution
    resources = cast(dict[str, Any], execution.get("resources", {}))
    operations = resources.get("operations", [])
    return {
        "corpus": corpus.name,
        "configuration": configuration,
        "phase": phase,
        "repetition": repetition,
        "elapsed_seconds": elapsed,
        "outcome": report.outcome,
        "failure": failure,
        "equivalence": checks,
        "execution": execution,
        "queue_wait_sum_seconds": sum(float(item["queue_seconds"]) for item in operations),
        "operation_time_sum_seconds": sum(float(item["elapsed_seconds"]) for item in operations),
        "stages": _normalized(report.stages, root),
        "evidence": evidence,
    }


def _range(values: list[float]) -> dict[str, Any]:
    return {
        "values": values,
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def _summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    groups = sorted({(item["corpus"], item["configuration"]) for item in records})
    for corpus, configuration in groups:
        selected = [
            item
            for item in records
            if (item["corpus"], item["configuration"]) == (corpus, configuration)
            and item["phase"] == "measured"
            and item["failure"] is None
        ]
        if not selected:
            continue
        summary: dict[str, Any] = {
            "corpus": corpus,
            "configuration": configuration,
            "repetitions": len(selected),
            "elapsed_seconds": _range([item["elapsed_seconds"] for item in selected]),
            "queue_wait_sum_seconds": _range([item["queue_wait_sum_seconds"] for item in selected]),
            "observed": {},
        }
        for metric in OBSERVED_METRICS:
            values = [item["execution"].get("observed", {}).get(metric) for item in selected]
            summary["observed"][metric] = (
                None if any(value is None for value in values) else _range(values)
            )
        summaries.append(summary)
    return summaries


async def benchmark(max_seconds: float, repetitions: int) -> dict[str, Any]:
    started = time.monotonic()
    # Stop admission five seconds before the requested bound for child reaping,
    # temporary-directory cleanup, and JSON assembly. Cleanup is never abandoned.
    deadline = started + max_seconds - 5
    result: dict[str, Any] = {
        "status": "completed",
        "started_utc": datetime.now(UTC).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "maximum_seconds": max_seconds,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "requested_repetitions": repetitions,
        "cache_conditions": (
            "Unpurged local caches; one pilot per corpus/configuration precedes measured "
            "repetitions. Each application job creates fresh build trees and an independent runner."
        ),
        "configuration_changes": {
            name: {"jobs": values[0], "render_jobs": values[1]}
            for name, values in CONFIGURATIONS.items()
        },
        "controlled_settings": {
            "command": "prepare",
            "engine": "pdflatex",
            "format": True,
            "build_jobs": 1,
            "memory_mb": 2048,
            "build_memory_mb": 1024,
            "timeout_seconds": 30,
            "job_timeout_seconds": "at most 60, reduced to remaining admission time",
        },
        "unavailable_metrics": {
            "time_to_first_useful_finding": (
                "The public report API has no timestamped finding callback."
            ),
            "cancellation_latency": (
                "No cancellation workload is injected into timed successful preparations."
            ),
        },
        "records": [],
        "skipped_corpora": [],
    }
    records: list[dict[str, Any]] = result["records"]
    expected: dict[str, ExpectedResult] = {}
    with tempfile.TemporaryDirectory(prefix="latexprep-benchmark-") as temporary:
        root = Path(temporary).resolve()
        corpora = _corpora(root)
        result["corpora"] = [
            {
                "name": corpus.name,
                "files": len(corpus.original),
                "source_bytes": sum(map(len, corpus.original.values())),
                "pages": corpus.expected_pages,
            }
            for corpus in corpora
        ]
        try:
            for index, corpus in enumerate(corpora):
                order = list(CONFIGURATIONS)
                if index % 2:
                    order.reverse()
                for configuration in order:
                    record = await _run_once(
                        corpus, configuration, "pilot", 0, root, deadline, expected
                    )
                    records.append(record)
                    console.print(
                        f"pilot {corpus.name} {configuration}: "
                        f"{record['elapsed_seconds']:.3f}s, {record['outcome']}",
                    )
                    if record["failure"]:
                        raise RuntimeError(f"Pilot stopped: {record['failure']}")
            for corpus in corpora:
                pilot_seconds = sum(
                    record["elapsed_seconds"]
                    for record in records
                    if record["corpus"] == corpus.name and record["phase"] == "pilot"
                )
                estimate = 1.25 * repetitions * pilot_seconds
                if deadline - time.monotonic() < estimate:
                    result["skipped_corpora"].append(
                        {
                            "corpus": corpus.name,
                            "reason": (
                                "Remaining time cannot admit the complete repetition set "
                                "with 25 percent pilot-based margin"
                            ),
                            "estimated_seconds": estimate,
                        }
                    )
                    continue
                for repetition in range(1, repetitions + 1):
                    order = list(CONFIGURATIONS)
                    if repetition % 2 == 0:
                        order.reverse()
                    for configuration in order:
                        record = await _run_once(
                            corpus, configuration, "measured", repetition, root, deadline, expected
                        )
                        records.append(record)
                        console.print(
                            f"repeat {repetition} {corpus.name} {configuration}: "
                            f"{record['elapsed_seconds']:.3f}s, {record['outcome']}",
                        )
                        if record["failure"]:
                            raise RuntimeError(f"Measured run stopped: {record['failure']}")
        except (RuntimeError, TimeoutError) as error:
            result["status"] = "stopped"
            result["failure"] = str(error)
    result["elapsed_budget_seconds"] = time.monotonic() - started
    result["summaries"] = _summaries(records)
    if result["skipped_corpora"] and result["status"] == "completed":
        result["status"] = "partial"
    return result


@click.command(context_settings={"help_option_names": ["-h", "--help"]}, help=__doc__)
@click.option(
    "--max-seconds",
    type=click.FloatRange(10, 180),
    default=180.0,
    show_default=True,
    help="Total finite budget including pilots; at most 180 seconds.",
)
@click.option(
    "--repetitions",
    type=click.IntRange(min=3),
    default=3,
    show_default=True,
    help="Measured repetitions per corpus/configuration; at least three.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Create this JSON report exclusively; default is JSON on stdout.",
)
def main(max_seconds: float, repetitions: int, output: Path | None) -> None:
    if not math.isfinite(max_seconds):
        raise click.BadParameter("must be finite", param_hint="--max-seconds")
    if output is not None and (output.exists() or not output.parent.is_dir()):
        raise click.BadParameter(
            "must be a new file in an existing directory", param_hint="--output"
        )
    result = asyncio.run(benchmark(max_seconds, repetitions))
    data = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if output is None:
        click.echo(data, nl=False)
    else:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(data)
        console.print(f"Results: {output}")
    click.get_current_context().exit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
