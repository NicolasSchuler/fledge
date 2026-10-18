"""Validated TOML settings and bounded project configuration discovery."""

from __future__ import annotations

import difflib
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, get_type_hints

from .bibliography_checks import BibliographyOptions
from .bibliography_transform import BibliographyTransformOptions
from .build_checks import BuildCheckOptions
from .check_policy import unselected_policy_updates
from .check_selection import CheckSelection
from .formatting import FormattingOptions
from .manuscript import ManuscriptOptions
from .manuscript_checks import ManuscriptCheckOptions
from .metadata_privacy import MetadataPrivacyOptions
from .models import (
    BIBLIOGRAPHY_BACKENDS,
    ENGINES,
    LAYOUTS,
    BibliographyBackend,
    Engine,
    Layout,
    PreparationError,
    RequestError,
)
from .online_checks import OnlineOptions
from .option_config import read_options, require_booleans
from .pdf_artwork import PdfArtworkOptions
from .pdf_checks import PdfCheckOptions
from .pdf_inspection import PdfInspectionOptions
from .pdf_structure_checks import StructureOptions
from .presets import preset_values
from .review_policy import ReportingOptions
from .source_transform import SourceTransformOptions
from .submission_checks import SubmissionOptions
from .workflow_options import PackageOptions, WorkflowOptions

# Memory reserved for the coordinating process beside every build reservation.
COORDINATOR_MEMORY_MB = 128
# Upper bound of jobs, build_jobs and render_jobs.
MAX_CONCURRENCY = 64
MIN_MEMORY_MB = 256
MIN_BUILD_MEMORY_MB = 128
# 9999-12-31T23:59:59Z, the last second with a four-digit year.
MAX_SOURCE_DATE_EPOCH = 253_402_300_799

# Former setting names, still accepted and normalized to their replacements.
RENAMED_SETTINGS: dict[str, str] = {
    "pdf_artwork": "rendered_pdf_artwork",
    "figure_artwork": "included_figure_artwork",
    "format": "apply_formatting",
}

_CHOICES: dict[str, tuple[str, ...]] = {
    "layout": LAYOUTS,
    "engine": ENGINES,
    "bibliography_backend": BIBLIOGRAPHY_BACKENDS,
}
_SWITCHES = ("apply_formatting", "normalize_doi", "cleanup")
_POSITIVE_INTEGERS = (
    "jobs",
    "build_jobs",
    "render_jobs",
    "memory_mb",
    "build_memory_mb",
    "timeout_seconds",
    "job_timeout_seconds",
    "max_temporary_bytes",
    "max_files",
    "max_bytes",
    "max_depth",
)
_PDF_METADATA_SOURCES = frozenset({"title", "author"})


def _bibliography_edit_tables(value: object) -> object:
    """TOML has no null: named edit tables express absent fields explicitly."""
    if not isinstance(value, dict) or not isinstance(value.get("field_edits"), list):
        return value
    rows = []
    for row in value["field_edits"]:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        allowed = {"key", "field", "expected", "expected_absent", "replacement", "remove"}
        if set(row) - allowed or not {"key", "field"} <= set(row):
            raise PreparationError(
                "Bibliography field-edit tables require key, field "
                "and explicit before/after choices"
            )
        if ("expected" in row) == (row.get("expected_absent") is True):
            raise PreparationError("Field edit requires expected or expected_absent=true")
        if ("replacement" in row) == (row.get("remove") is True):
            raise PreparationError("Field edit requires replacement or remove=true")
        for name in ("expected_absent", "remove"):
            if name in row and row[name] is not True:
                raise PreparationError(f"Field edit {name} must be true when specified")
        rows.append((row["key"], row["field"], row.get("expected"), row.get("replacement")))
    return {**value, "field_edits": rows}


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Validated settings of one job.

    ``manuscript`` and ``pdf`` hold constraints that TOML and reports spell as
    top-level keys, as do ``formatting_options.wrap_length`` and ``tab_size``.
    """

    checks: CheckSelection = field(default_factory=CheckSelection)
    structure_checks: StructureOptions = field(default_factory=StructureOptions)
    metadata_privacy: MetadataPrivacyOptions = field(default_factory=MetadataPrivacyOptions)
    formatting_options: FormattingOptions = field(default_factory=FormattingOptions)
    source_transforms: SourceTransformOptions = field(default_factory=SourceTransformOptions)
    bibliography_transform: BibliographyTransformOptions = field(
        default_factory=BibliographyTransformOptions
    )
    # Artwork policies for the rendered document and, separately, each included PDF figure.
    rendered_pdf_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    included_figure_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    reporting: ReportingOptions = field(default_factory=ReportingOptions)
    workflow: WorkflowOptions = field(default_factory=WorkflowOptions)
    package: PackageOptions = field(default_factory=PackageOptions)
    bibliography_checks: BibliographyOptions = field(default_factory=BibliographyOptions)
    build_checks: BuildCheckOptions = field(default_factory=BuildCheckOptions)
    manuscript_checks: ManuscriptCheckOptions = field(default_factory=ManuscriptCheckOptions)
    online_checks: OnlineOptions = field(default_factory=OnlineOptions)
    pdf_checks: PdfCheckOptions = field(default_factory=PdfCheckOptions)
    submission_checks: SubmissionOptions = field(default_factory=SubmissionOptions)
    manuscript: ManuscriptOptions = field(default_factory=ManuscriptOptions)
    pdf: PdfInspectionOptions = field(default_factory=PdfInspectionOptions)
    match_source_pdf_metadata: tuple[str, ...] = ()
    main: str | None = None
    layout: Layout = "preserve"
    engine: Engine = "pdflatex"
    bibliography_backend: BibliographyBackend = "auto"
    apply_formatting: bool = False
    normalize_doi: bool = False
    cleanup: bool = True
    jobs: int = 2
    build_jobs: int = 1
    render_jobs: int = 1
    memory_mb: int = 2048
    build_memory_mb: int = 1024
    timeout_seconds: int = 120
    job_timeout_seconds: int = 600
    max_temporary_bytes: int = 2_147_483_648
    source_date_epoch: int | None = None
    max_files: int = 10000
    max_bytes: int = 268435456
    max_depth: int = 32
    max_pages: int | None = None

    def __post_init__(self) -> None:
        for name, kind in _OPTION_TYPES.items():
            if not isinstance(getattr(self, name), kind):
                raise PreparationError(f"{name} must be a settings table")
        metadata = self.match_source_pdf_metadata
        if (
            not isinstance(metadata, tuple)
            or any(
                not isinstance(item, str) or item not in _PDF_METADATA_SOURCES for item in metadata
            )
            or len(set(metadata)) != len(metadata)
        ):
            raise PreparationError("match_source_pdf_metadata must list unique title/author fields")
        if self.main is not None and (not isinstance(self.main, str) or not self.main.strip()):
            raise PreparationError("main must be a nonempty relative filename")
        for name, allowed in _CHOICES.items():
            value = getattr(self, name)
            if not isinstance(value, str) or value not in allowed:
                raise PreparationError(f"{name} must be one of {', '.join(sorted(allowed))}")
        require_booleans(self, *_SWITCHES)
        for name in _POSITIVE_INTEGERS:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise PreparationError(f"{name} must be a positive integer")
        epoch = self.source_date_epoch
        if epoch is not None and (
            type(epoch) is not int or not 0 <= epoch <= MAX_SOURCE_DATE_EPOCH
        ):
            raise PreparationError(
                "source_date_epoch must be a nonnegative supported Unix timestamp"
            )
        if self.max_pages is not None and (type(self.max_pages) is not int or self.max_pages < 1):
            raise PreparationError("max_pages must be a positive integer")
        self._validate_combinations()

    def _validate_combinations(self) -> None:
        if self.workflow.documents and self.main is not None:
            raise PreparationError("Choose workflow.documents or one main, not both")
        if self.source_transforms.filename_overrides and self.layout != "flat":
            raise PreparationError("filename_overrides requires layout='flat'")
        if {name.title() for name in self.match_source_pdf_metadata} & {
            name for name, _ in self.pdf_checks.expected_metadata
        }:
            raise PreparationError(
                "Choose source matching or an explicit expected PDF value for each metadata field"
            )
        if self.jobs > MAX_CONCURRENCY:
            raise PreparationError(f"jobs must be at most {MAX_CONCURRENCY}")
        if self.build_jobs > MAX_CONCURRENCY or self.render_jobs > MAX_CONCURRENCY:
            raise PreparationError(f"build_jobs and render_jobs must be at most {MAX_CONCURRENCY}")
        if self.memory_mb < MIN_MEMORY_MB or self.build_memory_mb < MIN_BUILD_MEMORY_MB:
            raise PreparationError(
                f"memory_mb must be >={MIN_MEMORY_MB} and "
                f"build_memory_mb must be >={MIN_BUILD_MEMORY_MB}"
            )
        if self.build_memory_mb + COORDINATOR_MEMORY_MB > self.memory_mb:
            raise PreparationError(
                f"memory_mb must allow build_memory_mb plus {COORDINATOR_MEMORY_MB} MiB "
                "for the coordinator"
            )

    def to_dict(self) -> dict[str, object]:
        """The settings in their TOML layout, as reports record them."""
        values = asdict(self)
        formatting = values["formatting_options"]
        line_format = {name: formatting.pop(name) for name in _FORMATTING_KEYS}
        return {
            **{name: value for name, value in values.items() if name not in _FLATTENED_GROUPS},
            **line_format,
            **values["manuscript"],
            **values["pdf"],
        }

    def top_level_values(self) -> dict[str, object]:
        """Every top-level TOML setting with its value; a table maps to its option object."""
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in _FLATTENED_GROUPS
        }
        for name, group in _TOP_LEVEL_FIELDS.items():
            values[name] = getattr(getattr(self, group), name)
        return values

    def with_top_level_values(self, updates: Mapping[str, object]) -> Settings:
        """A copy with top-level TOML settings replaced, as ``top_level_values`` names them."""
        changes: dict[str, Any] = {
            name: value for name, value in updates.items() if name not in _TOP_LEVEL_FIELDS
        }
        grouped: dict[str, dict[str, object]] = {}
        for name, value in updates.items():
            if name in _TOP_LEVEL_FIELDS:
                grouped.setdefault(_TOP_LEVEL_FIELDS[name], {})[name] = value
        for group, values in grouped.items():
            changes[group] = replace(changes.get(group, getattr(self, group)), **values)
        return replace(self, **changes)


# Option groups whose fields are top-level TOML keys rather than a table.
_FLATTENED_GROUPS = ("manuscript", "pdf")
# Formatting options spelled as top-level TOML keys.
_FORMATTING_KEYS = ("wrap_length", "tab_size")
_TOP_LEVEL_FIELDS: dict[str, str] = {
    **{item.name: "manuscript" for item in fields(ManuscriptOptions)},
    **{item.name: "pdf" for item in fields(PdfInspectionOptions)},
    **dict.fromkeys(_FORMATTING_KEYS, "formatting_options"),
}
# Every option-object field of Settings, and those read from a TOML table.
_OPTION_TYPES: dict[str, type] = {
    name: kind
    for name, kind in get_type_hints(Settings).items()
    if isinstance(kind, type) and is_dataclass(kind)
}
_OPTION_TABLES = {
    name: kind for name, kind in _OPTION_TYPES.items() if name not in _FLATTENED_GROUPS
}
# Manuscript name lists that TOML supplies as arrays.
_NAME_LISTS = (
    "required_class_options",
    "forbidden_class_options",
    "forbidden_packages",
    "required_sections",
)
# The top-level TOML settings, in their current spelling.
SETTING_NAMES: frozenset[str] = frozenset(
    {item.name for item in fields(Settings) if item.name not in _FLATTENED_GROUPS}
    | set(_TOP_LEVEL_FIELDS)
)


def effective_settings(settings: Settings) -> Settings:
    """Disable unselected configured policies without changing the user's Settings.

    Unchanged objects are returned by identity, including the default ALL policy.
    """
    updates = unselected_policy_updates(settings.top_level_values(), settings.checks)
    return settings.with_top_level_values(updates) if updates else settings


# Discovered per directory in this order; the latex-prep names remain supported.
CONFIG_FILENAMES = (".fledge.toml", "fledge.toml", ".latex-prep.toml", "latex-prep.toml")
PYPROJECT_TABLES = ("fledge", "latex-prep")


def _load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as error:
        raise RequestError(
            f"Settings file {path} does not exist",
            "Check the spelling of the --config path, or run fledge init to write a starter "
            "fledge.toml.",
        ) from error
    except OSError as error:
        # strerror is the plain reason ("Permission denied"); str(error) adds an errno code.
        raise PreparationError(f"Cannot read settings {path}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise PreparationError(f"Cannot read settings {path}: {error}") from error


def _pyproject_settings(path: Path, values: dict[str, object]) -> dict[str, object] | None:
    """The [tool.fledge] or [tool.latex-prep] table, or None when neither exists."""
    tool = values.get("tool", {})
    if not isinstance(tool, dict):
        return None
    tables = [name for name in PYPROJECT_TABLES if name in tool]
    if not tables:
        return None
    if len(tables) > 1:
        raise PreparationError(
            f"{path} contains both [tool.fledge] and [tool.latex-prep]; keep only one"
        )
    settings = tool[tables[0]]
    if not isinstance(settings, dict):
        raise PreparationError(f"[tool.{tables[0]}] in {path} must be a settings table")
    return settings


def _read_settings_file(path: Path) -> dict[str, object]:
    values = _load_toml(path)
    if path.name != "pyproject.toml":
        return values
    settings = _pyproject_settings(path, values)
    if settings is None:
        raise PreparationError(f"Settings {path} has no [tool.fledge] or [tool.latex-prep] table")
    return settings


def _unknown_settings_error(
    unknown: list[str], known: frozenset[str], path: Path | None
) -> RequestError:
    guesses = {
        name: close[0]
        for name in unknown
        if (close := difflib.get_close_matches(name, sorted(known), n=1))
    }
    where = f" in {path}" if path is not None else ""
    message = f"Unknown setting(s){where}: " + ", ".join(
        f"{name} (did you mean {guesses[name]}?)" if name in guesses else name for name in unknown
    )
    fix = f"Rename or remove {'it' if len(unknown) == 1 else 'them'}{where}."
    if len(guesses) < len(unknown):
        fix += f" Valid top-level settings: {', '.join(sorted(known))}."
    return RequestError(message, fix)


def resolve_config_path(
    source: Path, explicit: Path | None = None, *, isolated: bool = False
) -> Path | None:
    """Choose one config beside the original input; never inspect extracted ZIPs.

    Stop after a repository root, before the home/filesystem-root ancestor, or
    after the starting directory when the input itself is at either boundary.
    """
    if explicit is not None:
        if isolated:
            raise PreparationError("--isolated cannot be combined with --config")
        return explicit.expanduser().resolve()
    if isolated:
        return None
    source = source.expanduser().resolve()
    start = source if source.is_dir() else source.parent
    home = Path.home().resolve()
    for directory in (start, *start.parents):
        if directory != start and (directory == home or directory == directory.parent):
            break
        for name in (*CONFIG_FILENAMES, "pyproject.toml"):
            candidate = directory / name
            if candidate.is_file() and (
                name != "pyproject.toml"
                or _pyproject_settings(candidate, _load_toml(candidate)) is not None
            ):
                return candidate
        if (
            directory == home
            or directory == directory.parent
            or (directory / ".git").exists()
            or (directory / ".hg").exists()
        ):
            break
    return None


def _layer_preset(preset: dict[str, object], selected: dict[str, object]) -> dict[str, object]:
    """Let the settings file override the preset; a table overrides it key by key."""
    merged = dict(preset)
    for key, value in selected.items():
        base = merged.get(key)
        if isinstance(base, dict) and isinstance(value, dict):
            value = {**base, **value}
        merged[key] = value
    return merged


def _current_names(values: Mapping[str, object]) -> dict[str, Any]:
    """Accept each former setting name as an alias of its replacement, but not both."""
    result: dict[str, Any] = {}
    for name, value in values.items():
        current = RENAMED_SETTINGS.get(name, name)
        if current in result:
            former = next(old for old, new in RENAMED_SETTINGS.items() if new == current)
            raise PreparationError(f"Set {current} only; {former} is its former name")
        result[current] = value
    return result


def _line_format(values: dict[str, Any], table: object) -> dict[str, Any]:
    """Top-level wrap_length and tab_size, which belong to the formatting options."""
    if isinstance(table, dict) and (nested := sorted(set(_FORMATTING_KEYS) & set(table))):
        raise PreparationError(
            f"Set {', '.join(nested)} at the top level, not in formatting_options"
        )
    return {name: values.pop(name) for name in _FORMATTING_KEYS if name in values}


def _build_settings(values: dict[str, Any], path: Path | None) -> Settings:
    """Construct Settings from TOML-layout values that use only current names."""
    line_format = _line_format(values, values.get("formatting_options"))
    tables: dict[str, Any] = {}
    for name, kind in _OPTION_TABLES.items():
        if name in values:
            value = values.pop(name)
            if name == "bibliography_transform":
                value = _bibliography_edit_tables(value)
            tables[name] = read_options(kind, value, name)
    if line_format:
        # The options validate these values with the messages of top-level settings.
        formatting = tables.get("formatting_options", FormattingOptions())
        tables["formatting_options"] = replace(formatting, **line_format)
    grouped: dict[str, dict[str, Any]] = {group: {} for group in _FLATTENED_GROUPS}
    for name in [name for name in values if _TOP_LEVEL_FIELDS.get(name) in grouped]:
        grouped[_TOP_LEVEL_FIELDS[name]][name] = values.pop(name)
    for name in _NAME_LISTS:
        if name in grouped["manuscript"]:
            value = grouped["manuscript"][name]
            if not isinstance(value, (tuple, list)):
                raise PreparationError(f"{name} must be an array of unique nonempty strings")
            grouped["manuscript"][name] = tuple(value)
    if isinstance(metadata := values.get("match_source_pdf_metadata"), list):
        values["match_source_pdf_metadata"] = tuple(metadata)
    settings = Settings(
        **values,
        **tables,
        manuscript=ManuscriptOptions(**grouped["manuscript"]),
        pdf=PdfInspectionOptions(**grouped["pdf"]),
    )
    return settings if path is None else _resolve_relative_paths(settings, path.parent)


def _resolve_relative_paths(settings: Settings, directory: Path) -> Settings:
    """Resolve file references in a settings file from that file's directory."""

    def resolved(name: str) -> str:
        return str((directory / Path(name).expanduser()).resolve())

    workflow, submission = settings.workflow, settings.submission_checks
    return replace(
        settings,
        workflow=replace(
            workflow,
            documents=tuple(
                replace(document, reference_pdf=resolved(document.reference_pdf))
                if document.reference_pdf is not None
                else document
                for document in workflow.documents
            ),
        ),
        submission_checks=replace(
            submission,
            template_references=tuple(
                (project, resolved(reference))
                for project, reference in submission.template_references
            ),
        ),
    )


def _settings_from_values(
    values: Mapping[str, object],
    overrides: Mapping[str, object],
    *,
    path: Path | None,
    preset: str | None,
) -> Settings:
    selected = _current_names(values)
    if preset is not None:
        selected = _layer_preset(_current_names(preset_values(preset)), selected)
    # An override of None means "not given", so it cannot conflict with its alias.
    requested = _current_names(
        {key: value for key, value in overrides.items() if value is not None}
    )
    named = {RENAMED_SETTINGS.get(key, key) for key in overrides}
    unknown = (set(selected) | named) - SETTING_NAMES
    if unknown:
        raise _unknown_settings_error(sorted(unknown), SETTING_NAMES, path)
    selected.update(requested)
    return _build_settings(selected, path)


def load_settings(
    path: Path | None, overrides: Mapping[str, object], *, preset: str | None = None
) -> Settings:
    """Layer an optional preset, then one selected file, then non-None CLI/API overrides."""
    values = _read_settings_file(path) if path is not None else {}
    return _settings_from_values(values, overrides, path=path, preset=preset)


def parse_settings_text(text: str, path: Path) -> Settings:
    """Validate settings text as ``load_settings`` would read it from the file ``path``."""
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PreparationError(f"Cannot read settings {path}: {error}") from error
    return _settings_from_values(values, {}, path=path, preset=None)
