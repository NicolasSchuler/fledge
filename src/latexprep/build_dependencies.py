"""Bounded fresh build evidence for the files needed in a submission.

The TeX recorder includes generated files and omits backend-only inputs. Combine
it with latexmk's rule database, whose version 4 writer emits both generated and
rewritten sections for every rule. Neither format has an end-of-file checksum;
completion here means bounded, structurally complete current-build evidence.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .models import PreparationError

_MAX_ROWS = 50_000
_MAX_RULES = 256
_MAX_PATH = 4096
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_HEADER = re.compile(
    rf'\["([^"\r\n]+)"\] ({_NUMBER}) "([^"\r\n]*)" "([^"\r\n]*)" '
    rf'"([^"\r\n]*)" ({_NUMBER}) (-?\d+)'
)
_INPUT = re.compile(rf'"([^"\r\n]+)" ({_NUMBER}) (-?\d+) ([0-9a-fA-F]{{32}}|0) "([^"\r\n]*)"')
_PATH_ROW = re.compile(r'"([^"\r\n]+)"')


@dataclass
class _Rule:
    name: str
    source: str
    destination: str
    inputs: list[tuple[str, str]] = field(default_factory=list)
    generated: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class SubmissionDependencies:
    inputs: set[str] | None
    details: dict[str, object]
    # Generated files the build read, relative to the output directory or project
    # that holds them (for example main.bbl written by BibTeX or Biber).
    generated_reads: frozenset[str] = frozenset()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _regular(path: Path, root: Path) -> bool:
    """Check every owned component before resolution can hide a symlink."""
    if not _inside(path, root):
        return False
    try:
        return (
            stat.S_ISREG(path.lstat().st_mode)
            and not root.is_symlink()
            and all(not part.is_symlink() for part in path.parents if _inside(part, root))
        )
    except OSError:
        return False


def _read(path: Path, root: Path, max_bytes: int) -> list[str]:
    if not _regular(path, root):
        raise PreparationError(f"{path.name} is missing or is not an owned regular file")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PreparationError(f"{path.name} exceeds the dependency evidence size limit")
    if not data or not data.endswith(b"\n"):
        raise PreparationError(f"{path.name} is empty or has an incomplete final line")
    rows = data.decode("utf-8").splitlines()
    if len(rows) > _MAX_ROWS:
        raise PreparationError(f"{path.name} exceeds the dependency evidence row limit")
    return rows


def _path(value: str, cwd: Path) -> Path:
    if not value or len(value) > _MAX_PATH or any(ord(char) < 32 for char in value):
        raise PreparationError("Dependency evidence contains an invalid or oversized path")
    path = Path(value)
    return Path(os.path.abspath(path if path.is_absolute() else cwd / path))


def _database(rows: list[str]) -> list[_Rule]:
    if rows[0] != "# Fdb version 4":
        raise PreparationError("Only latexmk dependency database version 4 is supported")
    rules: list[_Rule] = []
    names: set[str] = set()
    state = "start"
    for number, raw in enumerate(rows[1:], 2):
        row = raw.strip()
        header = _HEADER.fullmatch(row)
        if header:
            if state not in {"start", "rewritten"}:
                raise PreparationError(f"Dependency database rule is incomplete at line {number}")
            name, run_time, source, destination, _base, _check_time, last_result = header.groups()
            if name in names or len(rules) >= _MAX_RULES:
                raise PreparationError("Dependency database contains duplicate or too many rules")
            if float(run_time) <= 0 or last_result != "0":
                raise PreparationError(f"Dependency rule {name!r} did not complete successfully")
            if not source or not destination:
                raise PreparationError(f"Dependency rule {name!r} has no source or destination")
            names.add(name)
            rules.append(_Rule(name, source, destination))
            state = "source"
        elif row == "(generated)" and state == "source":
            state = "generated"
        elif row == "(rewritten before read)" and state == "generated":
            state = "rewritten"
        elif state == "source" and (record := _INPUT.fullmatch(row)):
            filename, _time, size, _digest, producer = record.groups()
            if int(size) < -1:
                raise PreparationError(f"Invalid dependency input size at line {number}")
            rules[-1].inputs.append((filename, producer))
        elif state in {"generated", "rewritten"} and (record := _PATH_ROW.fullmatch(row)):
            rules[-1].generated.add(record[1])
        else:
            raise PreparationError(f"Malformed dependency database record at line {number}")
    if not rules or state != "rewritten":
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
    engine: str,
    recorder_inputs: set[Path],
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
        if not recorder_complete:
            raise PreparationError("The selected build has no complete TeX recorder evidence")
        stem = Path(main).stem
        rows = _read(output / f"{stem}.fls", output, max_bytes)
        observed_inputs: set[Path] = set()
        generated: set[Path] = set()
        for number, row in enumerate(rows, 1):
            kind, separator, value = row.partition(" ")
            if not separator or kind not in {"PWD", "INPUT", "OUTPUT"}:
                raise PreparationError(f"Malformed TeX recorder record at line {number}")
            if value.startswith('"') or value.endswith('"'):
                if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
                    raise PreparationError(f"Malformed quoted recorder path at line {number}")
                value = value[1:-1]
            path = _path(value, cwd)
            if kind == "PWD":
                if path != cwd:
                    raise PreparationError(
                        "TeX recorder working directory does not match the build"
                    )
            elif kind == "INPUT":
                observed_inputs.add(path)
            else:
                generated.add(path)
        if {path.resolve() for path in observed_inputs} != recorder_inputs:
            raise PreparationError("TeX recorder inputs disagree with the collected build trace")
        main_path = project / main
        if main_path not in observed_inputs:
            raise PreparationError("TeX recorder does not contain the selected main source")
        expected_primary = output / (stem + (".xdv" if engine == "xelatex" else ".pdf"))
        if expected_primary not in generated:
            raise PreparationError(
                "TeX recorder does not establish completion of the primary output"
            )
        rules = _database(_read(output / f"{stem}.fdb_latexmk", output, max_bytes))
        primary = next((rule for rule in rules if rule.name == engine), None)
        if primary is None or _path(primary.source, cwd) != main_path:
            raise PreparationError(
                "Dependency database does not identify the selected main and engine"
            )
        if _path(primary.destination, cwd) != expected_primary:
            raise PreparationError("Dependency database primary output does not match the build")
        if main_path not in {_path(path, cwd) for path, _producer in primary.inputs}:
            raise PreparationError("Dependency database does not record reading the selected main")
        for rule in rules:
            observed_inputs.add(_path(rule.source, cwd))
            destinations = {_path(path, cwd) for path in rule.generated}
            destination = _path(rule.destination, cwd)
            if destination not in destinations:
                raise PreparationError(f"Dependency rule {rule.name!r} lacks its generated output")
            generated.update(destinations)
            for filename, producer in rule.inputs:
                path = _path(filename, cwd)
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
            if _inside(path, project):
                relative = path.relative_to(project).as_posix()
                if path in generated:
                    generated_count += 1
                    generated_reads.add(relative)
                elif not _regular(path, project) or not _regular(source / relative, source):
                    raise PreparationError(
                        "Observed project input is missing, generated without evidence, "
                        f"or unsafe: {relative}"
                    )
                else:
                    inputs.add(relative)
            elif path in generated and _inside(path, output):
                generated_count += 1
                generated_reads.add(path.relative_to(output).as_posix())
            elif path in owned and _regular(path, project.parent):
                generated_count += 1
            elif any(_inside(path.resolve(), root) for root in roots):
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
