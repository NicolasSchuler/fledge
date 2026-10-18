"""Bounded fresh build evidence: the recorder trace and latexmk's rule database.

The TeX recorder includes generated files and omits backend-only inputs. Combine
it with latexmk's rule database, whose version 4 writer emits both generated and
rewritten sections for every rule. Neither format has an end-of-file checksum;
completion here means bounded, structurally complete current-build evidence.
``collect_submission_dependencies`` joins the two into the original project inputs a
submission needs, or says why it cannot. Class and package headers are
``package_headers``'s work and bibliography control files ``bibliography_controls``'s;
all are read as literal text, and no project or package source is executed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import NamedTuple

from .build_workspace import BuildLayout
from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .path_safety import is_link_free_regular_file
from .setting_choices import Engine

_MAX_ROWS = 50_000
_MAX_RULES = 256
# Characters in one path of the evidence files; the file system's own limit is similar.
_MAX_PATH_CHARS = 4096
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_HEADER = re.compile(
    rf'\["([^"\r\n]+)"\] ({_NUMBER}) "([^"\r\n]*)" "([^"\r\n]*)" '
    rf'"([^"\r\n]*)" ({_NUMBER}) (-?\d+)'
)
_INPUT = re.compile(rf'"([^"\r\n]+)" ({_NUMBER}) (-?\d+) ([0-9a-fA-F]{{32}}|0) "([^"\r\n]*)"')
_PATH_ROW = re.compile(r'"([^"\r\n]+)"')
_RECORDER_KINDS = frozenset({"PWD", "INPUT", "OUTPUT"})


class _DatabaseSection(Enum):
    START = auto()
    SOURCES = auto()
    GENERATED = auto()
    REWRITTEN = auto()


@dataclass
class _LatexmkRule:
    name: str
    source: str
    destination: str
    inputs: list[tuple[str, str]] = field(default_factory=list)
    generated: set[str] = field(default_factory=set)


class RecordedPath(NamedTuple):
    """A recorder path as written, and as an absolute path from the build directory."""

    recorded: str
    absolute: Path


@dataclass(frozen=True, slots=True)
class RecorderTrace:
    """The TeX recorder (.fls) of one build, read once.

    ``inputs`` keeps the recorded order. ``defect`` names the first deviation from
    complete, well-formed evidence: the paths still describe what the build read,
    but they cannot establish the files needed for a submission.
    """

    inputs: tuple[RecordedPath, ...]
    outputs: frozenset[Path]
    defect: str | None


@dataclass(frozen=True, slots=True)
class SubmissionDependencies:
    """The original project inputs a submission needs, or why they cannot be established.

    ``inputs`` is ``None`` when the evidence is incomplete, and ``uncertainty`` then
    says what is missing or inconsistent.
    """

    inputs: set[str] | None
    rules: tuple[str, ...] = ()
    generated_inputs: int = 0
    toolchain_inputs: int = 0
    uncertainty: tuple[str, ...] = ()
    # Generated files the build read, relative to the output directory or project
    # that holds them (for example main.bbl written by BibTeX or Biber).
    generated_reads: frozenset[str] = frozenset()

    def to_details(self) -> dict[str, object]:
        """The report entry: what was consulted, and the counts or the uncertainty."""
        details: dict[str, object] = {
            "complete": self.inputs is not None,
            "evidence": ["TeX recorder", "latexmk dependency database"],
            "coverage": "Original project inputs observed by TeX and latexmk build rules.",
        }
        if self.inputs is not None:
            details.update(
                rules=list(self.rules),
                project_inputs=sorted(self.inputs),
                generated_inputs=self.generated_inputs,
                toolchain_inputs=self.toolchain_inputs,
            )
        details["uncertainty"] = list(self.uncertainty)
        return details


@dataclass(frozen=True, slots=True)
class DependencyRequest:
    """One finished build, as ``collect_submission_dependencies`` needs to see it."""

    layout: BuildLayout
    main: str
    engine: Engine
    # The recorder trace, and whether the build's checks accepted it as complete.
    recorder: RecorderTrace | None
    recorder_complete: bool
    # Directories the toolchain may read from, and inputs the build's own tooling wrote.
    resource_roots: tuple[Path, ...]
    max_bytes: int
    owned_inputs: tuple[Path, ...] = ()


def _read_owned_bytes(path: Path, root: Path, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` of a regular file reached through no link below ``root``."""
    if not is_link_free_regular_file(root, path):
        raise PreparationError(f"{path.name} is missing or is not an owned regular file")
    try:
        return read_regular_file(path, max_bytes)
    except FileTooLargeError:
        raise PreparationError(f"{path.name} exceeds the dependency evidence size limit") from None
    except OSError:
        raise PreparationError(f"{path.name} is missing or is not an owned regular file") from None


def _read_evidence_lines(path: Path, root: Path, max_bytes: int) -> list[str]:
    data = _read_owned_bytes(path, root, max_bytes)
    if not data or not data.endswith(b"\n"):
        raise PreparationError(f"{path.name} is empty or has an incomplete final line")
    rows = data.decode("utf-8").splitlines()
    if len(rows) > _MAX_ROWS:
        raise PreparationError(f"{path.name} exceeds the dependency evidence row limit")
    return rows


def _unchecked_absolute_path(value: str, cwd: Path) -> Path:
    path = Path(value)
    return Path(os.path.abspath(path if path.is_absolute() else cwd / path))


def _absolute_path(value: str, cwd: Path) -> Path:
    if not value or len(value) > _MAX_PATH_CHARS or any(ord(char) < 32 for char in value):
        raise PreparationError("Dependency evidence contains an invalid or oversized path")
    return _unchecked_absolute_path(value, cwd)


class _FirstDefect:
    """Remembers the first deviation from complete, well-formed evidence."""

    def __init__(self) -> None:
        self.problem: str | None = None

    def note(self, problem: str) -> None:
        self.problem = self.problem or problem


class _RecorderRow(NamedTuple):
    kind: str
    value: str
    absolute: Path


def _unquoted(value: str, number: int, defects: _FirstDefect) -> str:
    """A recorder path without the quotes TeX adds around names that contain spaces."""
    if not (value.startswith('"') or value.endswith('"')):
        return value
    if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
        defects.note(f"Malformed quoted recorder path at line {number}")
        return value.strip('"')
    return value[1:-1]


def _parse_recorder_row(
    row: str, number: int, cwd: Path, defects: _FirstDefect
) -> _RecorderRow | None:
    """One recorder line as kind, path and absolute path; ``None`` for a line to skip."""
    kind, separator, value = row.partition(" ")
    if not separator or kind not in _RECORDER_KINDS:
        defects.note(f"Malformed TeX recorder record at line {number}")
        return None
    value = _unquoted(value, number, defects)
    try:
        absolute = _absolute_path(value, cwd)
    except PreparationError as error:
        defects.note(str(error))
        absolute = _unchecked_absolute_path(value, cwd)
    return _RecorderRow(kind, value, absolute)


def read_recorder_trace(path: Path, root: Path, cwd: Path, max_bytes: int) -> RecorderTrace:
    """Read the recorder once; only an absent, foreign or oversized file is refused.

    Every INPUT row is kept even when the trace is malformed, so build checks
    still see what was read; the first defect withholds submission evidence.
    """
    data = _read_owned_bytes(path, root, max_bytes)
    defects = _FirstDefect()
    if not data or not data.endswith(b"\n"):
        defects.note(f"{path.name} is empty or has an incomplete final line")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        defects.note(str(error))
        text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if len(rows) > _MAX_ROWS:
        defects.note(f"{path.name} exceeds the dependency evidence row limit")
    inputs: list[RecordedPath] = []
    outputs: set[Path] = set()
    for number, row in enumerate(rows, 1):
        parsed = _parse_recorder_row(row, number, cwd, defects)
        if parsed is None:
            continue
        if parsed.kind == "PWD":
            if parsed.absolute != cwd:
                defects.note("TeX recorder working directory does not match the build")
        elif parsed.kind == "INPUT":
            inputs.append(RecordedPath(parsed.value, parsed.absolute))
        else:
            outputs.add(parsed.absolute)
    return RecorderTrace(tuple(inputs), frozenset(outputs), defects.problem)


def _rule_from_header(header: re.Match[str], rules: list[_LatexmkRule]) -> _LatexmkRule:
    """The rule a database header line starts, if it is a new rule that ran successfully."""
    name, run_time, source, destination, _base, _check_time, last_result = header.groups()
    if any(rule.name == name for rule in rules) or len(rules) >= _MAX_RULES:
        raise PreparationError("Dependency database contains duplicate or too many rules")
    if float(run_time) <= 0 or last_result != "0":
        raise PreparationError(f"Dependency rule {name!r} did not complete successfully")
    if not source or not destination:
        raise PreparationError(f"Dependency rule {name!r} has no source or destination")
    return _LatexmkRule(name, source, destination)


def _read_rule_row(
    rule: _LatexmkRule, section: _DatabaseSection, row: str, number: int
) -> _DatabaseSection:
    """Apply one line inside a rule to it, and return the section the next line is in."""
    match section:
        case _DatabaseSection.SOURCES if row == "(generated)":
            return _DatabaseSection.GENERATED
        case _DatabaseSection.GENERATED if row == "(rewritten before read)":
            return _DatabaseSection.REWRITTEN
        case _DatabaseSection.SOURCES if record := _INPUT.fullmatch(row):
            filename, _time, size, _digest, producer = record.groups()
            if int(size) < -1:
                raise PreparationError(f"Invalid dependency input size at line {number}")
            rule.inputs.append((filename, producer))
        case _DatabaseSection.GENERATED | _DatabaseSection.REWRITTEN if (
            record := _PATH_ROW.fullmatch(row)
        ):
            rule.generated.add(record[1])
        case _:
            raise PreparationError(f"Malformed dependency database record at line {number}")
    return section


def _check_rule_graph(rules: list[_LatexmkRule], section: _DatabaseSection) -> None:
    """Every rule has inputs, and every producer an input names is a rule of the database."""
    if not rules or section is not _DatabaseSection.REWRITTEN:
        raise PreparationError("Dependency database lacks complete rule sections")
    names = {rule.name for rule in rules}
    for rule in rules:
        if not rule.inputs:
            raise PreparationError(f"Dependency rule {rule.name!r} has no recorded inputs")
        for _filename, producer in rule.inputs:
            if producer and producer not in names:
                raise PreparationError(f"Dependency database references missing rule {producer!r}")


def _parse_latexmk_database(rows: list[str]) -> list[_LatexmkRule]:
    if rows[0] != "# Fdb version 4":
        raise PreparationError("Only latexmk dependency database version 4 is supported")
    rules: list[_LatexmkRule] = []
    section = _DatabaseSection.START
    for number, raw in enumerate(rows[1:], 2):
        row = raw.strip()
        if header := _HEADER.fullmatch(row):
            if section not in {_DatabaseSection.START, _DatabaseSection.REWRITTEN}:
                raise PreparationError(f"Dependency database rule is incomplete at line {number}")
            rules.append(_rule_from_header(header, rules))
            section = _DatabaseSection.SOURCES
        elif not rules:
            raise PreparationError(f"Malformed dependency database record at line {number}")
        else:
            section = _read_rule_row(rules[-1], section, row, number)
    _check_rule_graph(rules, section)
    return rules


@dataclass(slots=True)
class _ObservedPaths:
    """Absolute paths the build read and the paths it generated, built up from the evidence."""

    inputs: set[Path]
    generated: set[Path]


class _ClassifiedInputs(NamedTuple):
    project_inputs: set[str]
    generated_reads: set[str]
    generated_count: int
    toolchain_count: int


def _expected_primary(request: DependencyRequest) -> Path:
    """The file the engine writes first: ``.xdv`` for XeLaTeX, ``.pdf`` otherwise."""
    suffix = ".xdv" if request.engine == "xelatex" else ".pdf"
    return request.layout.output / (Path(request.main).stem + suffix)


def _verify_primary_output(request: DependencyRequest) -> _ObservedPaths:
    """Check the recorder shows the main file read and the primary output written."""
    recorder = request.recorder
    if recorder is None or not request.recorder_complete:
        raise PreparationError("The selected build has no complete TeX recorder evidence")
    if recorder.defect is not None:
        raise PreparationError(recorder.defect)
    observed = _ObservedPaths({item.absolute for item in recorder.inputs}, set(recorder.outputs))
    if request.layout.project / request.main not in observed.inputs:
        raise PreparationError("TeX recorder does not contain the selected main source")
    if _expected_primary(request) not in observed.generated:
        raise PreparationError("TeX recorder does not establish completion of the primary output")
    return observed


def _read_primary_rule(request: DependencyRequest) -> list[_LatexmkRule]:
    """Read latexmk's rule database and check its engine rule is this build's main to PDF."""
    layout = request.layout
    stem = Path(request.main).stem
    main_path = layout.project / request.main
    rules = _parse_latexmk_database(
        _read_evidence_lines(
            layout.output / f"{stem}.fdb_latexmk", layout.output, request.max_bytes
        )
    )
    primary = next((rule for rule in rules if rule.name == request.engine), None)
    if primary is None or _absolute_path(primary.source, layout.cwd) != main_path:
        raise PreparationError("Dependency database does not identify the selected main and engine")
    if _absolute_path(primary.destination, layout.cwd) != _expected_primary(request):
        raise PreparationError("Dependency database primary output does not match the build")
    if main_path not in {_absolute_path(path, layout.cwd) for path, _ in primary.inputs}:
        raise PreparationError("Dependency database does not record reading the selected main")
    return rules


def _merge_rule_evidence(
    request: DependencyRequest, rules: list[_LatexmkRule], observed: _ObservedPaths
) -> None:
    """Add every rule's sources, inputs and outputs to what the recorder showed."""
    cwd = request.layout.cwd
    for rule in rules:
        observed.inputs.add(_absolute_path(rule.source, cwd))
        destinations = {_absolute_path(path, cwd) for path in rule.generated}
        if _absolute_path(rule.destination, cwd) not in destinations:
            raise PreparationError(f"Dependency rule {rule.name!r} lacks its generated output")
        observed.generated.update(destinations)
        for filename, producer in rule.inputs:
            path = _absolute_path(filename, cwd)
            observed.inputs.add(path)
            if producer:
                observed.generated.add(path)
    if request.layout.project / request.main in observed.generated:
        raise PreparationError("The selected main source was marked as a generated file")


def _classify_inputs(request: DependencyRequest, observed: _ObservedPaths) -> _ClassifiedInputs:
    """Sort every input into an original project file, a generated file or a toolchain file.

    An input that is none of these, or an original that is missing or linked, makes
    the evidence unusable.
    """
    layout = request.layout
    roots = tuple(root.resolve() for root in request.resource_roots)
    owned = set(request.owned_inputs)
    project_inputs: set[str] = set()
    generated_reads: set[str] = set()
    generated_count = toolchain_count = 0
    for path in sorted(observed.inputs):
        if path.is_relative_to(layout.project):
            relative = path.relative_to(layout.project).as_posix()
            if path in observed.generated:
                generated_count += 1
                generated_reads.add(relative)
            elif is_link_free_regular_file(layout.project, path) and is_link_free_regular_file(
                layout.source, layout.source / relative
            ):
                project_inputs.add(relative)
            else:
                raise PreparationError(
                    "Observed project input is missing, generated without evidence, "
                    f"or unsafe: {relative}"
                )
        elif path in observed.generated and path.is_relative_to(layout.output):
            generated_count += 1
            generated_reads.add(path.relative_to(layout.output).as_posix())
        elif path in owned and is_link_free_regular_file(layout.project.parent, path):
            generated_count += 1
        elif any(path.resolve().is_relative_to(root) for root in roots):
            toolchain_count += 1
        else:
            raise PreparationError(
                f"Observed input is outside declared source/toolchain paths: {path}"
            )
    return _ClassifiedInputs(project_inputs, generated_reads, generated_count, toolchain_count)


def collect_submission_dependencies(request: DependencyRequest) -> SubmissionDependencies:
    """Return the original project inputs of a build, or ``None`` with an actionable uncertainty."""
    try:
        observed = _verify_primary_output(request)
        rules = _read_primary_rule(request)
        _merge_rule_evidence(request, rules, observed)
        classified = _classify_inputs(request, observed)
    except (OSError, UnicodeError, ValueError, PreparationError) as error:
        return SubmissionDependencies(None, uncertainty=(str(error),))
    return SubmissionDependencies(
        classified.project_inputs,
        rules=tuple(rule.name for rule in rules),
        generated_inputs=classified.generated_count,
        toolchain_inputs=classified.toolchain_count,
        generated_reads=frozenset(classified.generated_reads),
    )
