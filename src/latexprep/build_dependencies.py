"""Bounded fresh build evidence: recorder trace, rule database, packages and backends.

The TeX recorder includes generated files and omits backend-only inputs. Combine
it with latexmk's rule database, whose version 4 writer emits both generated and
rewritten sections for every rule. Neither format has an end-of-file checksum;
completion here means bounded, structurally complete current-build evidence.
Class/package headers and bibliography control files are read as literal text;
no project or package source is executed.
"""

from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from enum import Enum, auto
from pathlib import Path
from typing import Literal, NamedTuple

from .file_safety import is_contained_regular_file
from .models import Engine, PreparationError
from .tex_lexer import ParseLimitExceeded, mask_literals, scan_commands, scan_source_commands

_MAX_ROWS = 50_000
_MAX_RULES = 256
_MAX_PATH = 4096
_MAX_PACKAGE_HEADER_BYTES = 32 * 1024
_MAX_CONTROL_FILES = 1000
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_HEADER = re.compile(
    rf'\["([^"\r\n]+)"\] ({_NUMBER}) "([^"\r\n]*)" "([^"\r\n]*)" '
    rf'"([^"\r\n]*)" ({_NUMBER}) (-?\d+)'
)
_INPUT = re.compile(rf'"([^"\r\n]+)" ({_NUMBER}) (-?\d+) ([0-9a-fA-F]{{32}}|0) "([^"\r\n]*)"')
_PATH_ROW = re.compile(r'"([^"\r\n]+)"')
_RECORDER_KINDS = frozenset({"PWD", "INPUT", "OUTPUT"})
# A literal class or package filename, as the recorder and the kernel report it.
PACKAGE_FILENAME = re.compile(r"[A-Za-z0-9_.-]+\.(?:cls|sty)")
# LaTeX release dates are written YYYY/MM/DD or YYYY-MM-DD.
_DATE_TEXT = r"\d{4}[-/]\d{2}[-/]\d{2}"
_DECLARED_DATE = re.compile(_DATE_TEXT)
_HEADER_RELEASE = re.compile(rf"\s*({_DATE_TEXT})(?:\s+(\S+))?")
_ORDINARY_DECLARATION = re.compile(r"\\Provides(?:Package|Class)\s*\{([^{}]+)\}\s*\[([^\[\]]*)\]")
_EXPL3_DECLARATION = re.compile(
    r"\\ProvidesExpl(?:Package|Class)\s*\{([^{}]+)\}\s*\{([^{}]+)\}\s*\{([^{}]+)\}"
)
_BCF_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_BIBLIOGRAPHY_COVERAGE = "Fresh .bcf and .aux backend requirements, not proof of backend execution."

PackageOrigin = Literal["project", "toolchain"]
ObservedBackend = Literal["bibtex", "biber"]


def parse_declared_date(value: str) -> date | None:
    """A LaTeX release date such as 2025/01/31; ``None`` when absent or invalid."""
    if not _DECLARED_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None


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
    inputs: set[str] | None
    details: dict[str, object]
    # Generated files the build read, relative to the output directory or project
    # that holds them (for example main.bbl written by BibTeX or Biber).
    generated_reads: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class LoadedPackage:
    """A class or package file the build read, with its literal declared release."""

    name: str
    path: str
    origin: PackageOrigin
    declared_date: str | None = None
    declared_version: str | None = None


@dataclass(frozen=True, slots=True)
class BibliographyControl:
    path: str
    backend: ObservedBackend
    evidence: Literal["bcf", "aux"]


@dataclass(frozen=True, slots=True)
class BibliographyEvidence:
    """Backends that fresh control files require; this does not assert a backend ran."""

    observed_backends: tuple[ObservedBackend, ...]
    control_files: tuple[BibliographyControl, ...]
    uncertainty: tuple[str, ...]

    def to_details(self) -> dict[str, object]:
        return {
            "observed_backends": list(self.observed_backends),
            "control_files": [asdict(control) for control in self.control_files],
            "uncertainty": list(self.uncertainty),
            "coverage": _BIBLIOGRAPHY_COVERAGE,
        }


def _is_owned_regular_file(path: Path, root: Path) -> bool:
    """Check every owned component before resolution can hide a symlink."""
    if not path.is_relative_to(root):
        return False
    try:
        return (
            stat.S_ISREG(path.lstat().st_mode)
            and not root.is_symlink()
            and all(not part.is_symlink() for part in path.parents if part.is_relative_to(root))
        )
    except OSError:
        return False


def _read_owned_bytes(path: Path, root: Path, max_bytes: int) -> bytes:
    if not _is_owned_regular_file(path, root):
        raise PreparationError(f"{path.name} is missing or is not an owned regular file")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PreparationError(f"{path.name} exceeds the dependency evidence size limit")
    return data


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
    if not value or len(value) > _MAX_PATH or any(ord(char) < 32 for char in value):
        raise PreparationError("Dependency evidence contains an invalid or oversized path")
    return _unchecked_absolute_path(value, cwd)


def read_recorder_trace(path: Path, root: Path, cwd: Path, max_bytes: int) -> RecorderTrace:
    """Read the recorder once; only an absent, foreign or oversized file is refused.

    Every INPUT row is kept even when the trace is malformed, so build checks
    still see what was read; the first defect withholds submission evidence.
    """
    data = _read_owned_bytes(path, root, max_bytes)
    defect: str | None = None

    def note(problem: str) -> None:
        nonlocal defect
        defect = defect or problem

    if not data or not data.endswith(b"\n"):
        note(f"{path.name} is empty or has an incomplete final line")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        note(str(error))
        text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if len(rows) > _MAX_ROWS:
        note(f"{path.name} exceeds the dependency evidence row limit")
    inputs: list[RecordedPath] = []
    outputs: set[Path] = set()
    for number, row in enumerate(rows, 1):
        kind, separator, value = row.partition(" ")
        if not separator or kind not in _RECORDER_KINDS:
            note(f"Malformed TeX recorder record at line {number}")
            continue
        if value.startswith('"') or value.endswith('"'):
            if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
                note(f"Malformed quoted recorder path at line {number}")
                value = value.strip('"')
            else:
                value = value[1:-1]
        try:
            absolute = _absolute_path(value, cwd)
        except PreparationError as error:
            note(str(error))
            absolute = _unchecked_absolute_path(value, cwd)
        if kind == "PWD":
            if absolute != cwd:
                note("TeX recorder working directory does not match the build")
        elif kind == "INPUT":
            inputs.append(RecordedPath(value, absolute))
        else:
            outputs.add(absolute)
    return RecorderTrace(tuple(inputs), frozenset(outputs), defect)


def _parse_latexmk_database(rows: list[str]) -> list[_LatexmkRule]:
    if rows[0] != "# Fdb version 4":
        raise PreparationError("Only latexmk dependency database version 4 is supported")
    rules: list[_LatexmkRule] = []
    names: set[str] = set()
    section = _DatabaseSection.START
    for number, raw in enumerate(rows[1:], 2):
        row = raw.strip()
        if header := _HEADER.fullmatch(row):
            if section not in {_DatabaseSection.START, _DatabaseSection.REWRITTEN}:
                raise PreparationError(f"Dependency database rule is incomplete at line {number}")
            name, run_time, source, destination, _base, _check_time, last_result = header.groups()
            if name in names or len(rules) >= _MAX_RULES:
                raise PreparationError("Dependency database contains duplicate or too many rules")
            if float(run_time) <= 0 or last_result != "0":
                raise PreparationError(f"Dependency rule {name!r} did not complete successfully")
            if not source or not destination:
                raise PreparationError(f"Dependency rule {name!r} has no source or destination")
            names.add(name)
            rules.append(_LatexmkRule(name, source, destination))
            section = _DatabaseSection.SOURCES
            continue
        match section:
            case _DatabaseSection.SOURCES if row == "(generated)":
                section = _DatabaseSection.GENERATED
            case _DatabaseSection.GENERATED if row == "(rewritten before read)":
                section = _DatabaseSection.REWRITTEN
            case _DatabaseSection.SOURCES if record := _INPUT.fullmatch(row):
                filename, _time, size, _digest, producer = record.groups()
                if int(size) < -1:
                    raise PreparationError(f"Invalid dependency input size at line {number}")
                rules[-1].inputs.append((filename, producer))
            case _DatabaseSection.GENERATED | _DatabaseSection.REWRITTEN if (
                record := _PATH_ROW.fullmatch(row)
            ):
                rules[-1].generated.add(record[1])
            case _:
                raise PreparationError(f"Malformed dependency database record at line {number}")
    if not rules or section is not _DatabaseSection.REWRITTEN:
        raise PreparationError("Dependency database lacks complete rule sections")
    for rule in rules:
        if not rule.inputs:
            raise PreparationError(f"Dependency rule {rule.name!r} has no recorded inputs")
        for _filename, producer in rule.inputs:
            if producer and producer not in names:
                raise PreparationError(f"Dependency database references missing rule {producer!r}")
    return rules


def collect_submission_dependencies(
    *,
    source: Path,
    project: Path,
    cwd: Path,
    output: Path,
    main: str,
    engine: Engine,
    recorder: RecorderTrace | None,
    recorder_complete: bool,
    resource_roots: tuple[Path, ...],
    owned_inputs: tuple[Path, ...] = (),
    max_bytes: int,
) -> SubmissionDependencies:
    """Return original project inputs, or None with an actionable uncertainty."""
    details: dict[str, object] = {
        "complete": False,
        "evidence": ["TeX recorder", "latexmk dependency database"],
        "coverage": "Original project inputs observed by TeX and latexmk build rules.",
    }
    try:
        if recorder is None or not recorder_complete:
            raise PreparationError("The selected build has no complete TeX recorder evidence")
        if recorder.defect is not None:
            raise PreparationError(recorder.defect)
        observed_inputs = {item.absolute for item in recorder.inputs}
        generated = set(recorder.outputs)
        stem = Path(main).stem
        main_path = project / main
        if main_path not in observed_inputs:
            raise PreparationError("TeX recorder does not contain the selected main source")
        expected_primary = output / (stem + (".xdv" if engine == "xelatex" else ".pdf"))
        if expected_primary not in generated:
            raise PreparationError(
                "TeX recorder does not establish completion of the primary output"
            )
        rules = _parse_latexmk_database(
            _read_evidence_lines(output / f"{stem}.fdb_latexmk", output, max_bytes)
        )
        primary = next((rule for rule in rules if rule.name == engine), None)
        if primary is None or _absolute_path(primary.source, cwd) != main_path:
            raise PreparationError(
                "Dependency database does not identify the selected main and engine"
            )
        if _absolute_path(primary.destination, cwd) != expected_primary:
            raise PreparationError("Dependency database primary output does not match the build")
        if main_path not in {_absolute_path(path, cwd) for path, _producer in primary.inputs}:
            raise PreparationError("Dependency database does not record reading the selected main")
        for rule in rules:
            observed_inputs.add(_absolute_path(rule.source, cwd))
            destinations = {_absolute_path(path, cwd) for path in rule.generated}
            if _absolute_path(rule.destination, cwd) not in destinations:
                raise PreparationError(f"Dependency rule {rule.name!r} lacks its generated output")
            generated.update(destinations)
            for filename, producer in rule.inputs:
                path = _absolute_path(filename, cwd)
                observed_inputs.add(path)
                if producer:
                    generated.add(path)
        if main_path in generated:
            raise PreparationError("The selected main source was marked as a generated file")
        roots = tuple(root.resolve() for root in resource_roots)
        owned = set(owned_inputs)
        inputs: set[str] = set()
        generated_reads: set[str] = set()
        generated_count = system_count = 0
        for path in sorted(observed_inputs):
            if path.is_relative_to(project):
                relative = path.relative_to(project).as_posix()
                if path in generated:
                    generated_count += 1
                    generated_reads.add(relative)
                elif not _is_owned_regular_file(path, project) or not _is_owned_regular_file(
                    source / relative, source
                ):
                    raise PreparationError(
                        "Observed project input is missing, generated without evidence, "
                        f"or unsafe: {relative}"
                    )
                else:
                    inputs.add(relative)
            elif path in generated and path.is_relative_to(output):
                generated_count += 1
                generated_reads.add(path.relative_to(output).as_posix())
            elif path in owned and _is_owned_regular_file(path, project.parent):
                generated_count += 1
            elif any(path.resolve().is_relative_to(root) for root in roots):
                system_count += 1
            else:
                raise PreparationError(
                    f"Observed input is outside declared source/toolchain paths: {path}"
                )
        details.update(
            complete=True,
            rules=[rule.name for rule in rules],
            project_inputs=sorted(inputs),
            generated_inputs=generated_count,
            toolchain_inputs=system_count,
            uncertainty=[],
        )
        return SubmissionDependencies(inputs, details, frozenset(generated_reads))
    except (OSError, UnicodeError, ValueError, PreparationError) as error:
        details["uncertainty"] = [str(error)]
        return SubmissionDependencies(None, details)


class _Release(NamedTuple):
    date: str
    version: str | None


def read_loaded_package(path: Path, project: Path) -> LoadedPackage:
    """Record bounded literal header metadata, never execute package source."""
    local = path.is_relative_to(project)
    package = LoadedPackage(
        name=path.name,
        path=path.relative_to(project).as_posix() if local else str(path),
        origin="project" if local else "toolchain",
    )
    release = _declared_release(path)
    if release is None:
        return package
    return replace(package, declared_date=release.date, declared_version=release.version)


def _declared_release(path: Path) -> _Release | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(_MAX_PACKAGE_HEADER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    header = mask_literals(header)
    ordinary_declarations = list(_ORDINARY_DECLARATION.finditer(header))
    expl3_declarations = list(_EXPL3_DECLARATION.finditer(header))
    declarations = [*ordinary_declarations, *expl3_declarations]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    try:
        # Only an unconditional, top-level literal header can establish the date.
        # In particular, declarations after endinput or inside definitions cannot.
        commands = scan_commands(header[: declaration.end()])
    except ParseLimitExceeded:
        return None
    if any(
        command.depth or command.name != "NeedsTeXFormat"
        for command in commands
        if command.start < declaration.start()
    ) or not any(
        command.start == declaration.start() and command.depth == 0 for command in commands
    ):
        return None
    if declaration[1].strip() != path.stem:
        return None
    # An expl3 declaration gives its date and version as separate arguments.
    release = declaration[2] if ordinary_declarations else f"{declaration[2]} {declaration[3]}"
    found = _HEADER_RELEASE.match(release)
    return _Release(found[1], found[2]) if found else None


def read_bibliography_evidence(output: Path, limit: int) -> BibliographyEvidence:
    """Read fresh backend control files; this does not assert a backend ran."""
    backends: set[ObservedBackend] = set()
    controls: list[BibliographyControl] = []
    uncertainty: list[str] = []
    files: list[Path] = []
    for path in output.rglob("*"):
        if path.suffix not in {".bcf", ".aux"}:
            continue
        if len(files) >= _MAX_CONTROL_FILES:
            uncertainty.append(f"Bibliography control evidence exceeds {_MAX_CONTROL_FILES} files")
            break
        files.append(path)
    biber_stems: set[Path] = set()
    total = 0
    for path in sorted(files, key=lambda item: (item.suffix != ".bcf", str(item))):
        relative = path.relative_to(output).as_posix()
        try:
            if not is_contained_regular_file(output, path):
                raise PreparationError("control file is not an owned regular file")
            total += path.stat().st_size
            if total > limit:
                raise PreparationError(
                    "combined control evidence exceeds the diagnostic size limit"
                )
            data = path.read_bytes()
            if len(data) > limit:
                raise PreparationError("control file exceeds the diagnostic size limit")
            text = data.decode("utf-8")
            if path.suffix == ".bcf":
                _require_biber_control(text)
                biber_stems.add(path.with_suffix(""))
                backends.add("biber")
                controls.append(BibliographyControl(relative, "biber", "bcf"))
            elif path.with_suffix("") not in biber_stems and _declares_bibtex_data(text):
                backends.add("bibtex")
                controls.append(BibliographyControl(relative, "bibtex", "aux"))
        except (
            OSError,
            UnicodeError,
            ET.ParseError,
            ParseLimitExceeded,
            PreparationError,
        ) as error:
            uncertainty.append(f"{relative}: {error}")
    return BibliographyEvidence(tuple(sorted(backends)), tuple(controls), tuple(uncertainty))


def _require_biber_control(text: str) -> None:
    if _BCF_DECLARATION.search(text):
        raise PreparationError("BCF declarations are unsupported")
    document = ET.fromstring(text)
    if document.tag.rsplit("}", 1)[-1] != "controlfile":
        raise PreparationError("BCF root is not a bibliography control file")


def _declares_bibtex_data(text: str) -> bool:
    commands = scan_source_commands(text)
    resources = [command for command in commands if command.name == "bibdata"]
    if any(not command.arguments or not command.arguments[0].value for command in resources):
        raise PreparationError("AUX bibliography data declaration is incomplete")
    return bool(resources)
