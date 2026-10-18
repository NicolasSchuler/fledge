"""Which bibliography backend a build's fresh control files require.

BibTeX leaves a ``\\bibdata`` line in the ``.aux`` file and Biber a ``.bcf`` control
file. Reading them as literal text shows which backend the document needs, without
running either one: this is evidence of a requirement, not proof that a backend ran.
A control file that is linked, too large, malformed or declares an entity is reported
as uncertainty rather than trusted. The latexmk rule database and the recorder trace
are ``build_dependencies``'s concern.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .path_safety import is_contained
from .tex_lexer import ParseLimitExceeded, scan_source_commands

_MAX_CONTROL_FILES = 1000
_BCF_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_BIBLIOGRAPHY_COVERAGE = "Fresh .bcf and .aux backend requirements, not proof of backend execution."

ObservedBackend = Literal["bibtex", "biber"]


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
            if not is_contained(output, path, regular_file=True):
                raise PreparationError("control file is not an owned regular file")
            try:
                data = read_regular_file(path, limit - total)
            except FileTooLargeError:
                raise PreparationError(
                    "combined control evidence exceeds the diagnostic size limit"
                    if total
                    else "control file exceeds the diagnostic size limit"
                ) from None
            total += len(data)
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
