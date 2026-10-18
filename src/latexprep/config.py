"""Validated settings: the ``Settings`` schema, and layering preset, file and command line.

Owns what the settings are called, how they are validated, and the precedence presets <
settings file < command-line overrides. Finding and reading the settings file is in
``config_discovery``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, get_type_hints

from .author_records import AuthorRecordOptions
from .bbl_checks import BblOptions
from .bibliography_options import BibliographyOptions
from .bibliography_transform import BibliographyTransformOptions
from .build_checks import BuildCheckOptions
from .check_policy import unselected_policy_updates
from .check_selection import CheckSelection
from .config_discovery import read_settings_file
from .errors import PreparationError, RequestError, SettingsError
from .formatting import FormattingOptions
from .limits import (
    DEFAULT_BUILD_MEMORY_MB,
    DEFAULT_MAX_FOLDER_LEVELS,
    DEFAULT_MAX_PROJECT_BYTES,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    MAX_PROJECT_ENTRIES,
)
from .manuscript import ManuscriptOptions
from .manuscript_check_options import ManuscriptCheckOptions
from .metadata_privacy import MetadataPrivacyOptions
from .online_options import OnlineOptions
from .option_config import (
    POSITIVE_INTEGER,
    Bounds,
    closest_names,
    read_option_groups,
    read_options,
    read_top_level_options,
    require_text,
    require_unique_strings,
    validate_scalars,
    with_suggestions,
)
from .pdf_artwork_options import PdfArtworkOptions
from .pdf_check_options import PdfCheckOptions
from .pdf_inspection import PdfInspectionOptions
from .pdf_structure_checks import StructureOptions
from .presets import preset_values
from .review_policy import ReportingOptions
from .setting_choices import BibliographyBackend, Engine, Layout
from .source_transform import SourceTransformOptions
from .submission_options import SubmissionOptions
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

# Source-document fields whose value a PDF property may be required to match.
_PDF_METADATA_SOURCES = ("title", "author")


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
    ``author_records`` and ``bbl_checks`` are written inside the ``structure_checks`` and
    ``submission_checks`` tables (see ``SHARED_TABLES``), and reports record them there.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "jobs": Bounds(1, MAX_CONCURRENCY),
        "build_jobs": Bounds(1, MAX_CONCURRENCY),
        "render_jobs": Bounds(1, MAX_CONCURRENCY),
        "memory_mb": Bounds(MIN_MEMORY_MB),
        "build_memory_mb": Bounds(MIN_BUILD_MEMORY_MB),
        "timeout_seconds": POSITIVE_INTEGER,
        "job_timeout_seconds": POSITIVE_INTEGER,
        "max_temporary_bytes": POSITIVE_INTEGER,
        "source_date_epoch": Bounds(0, MAX_SOURCE_DATE_EPOCH),
        "max_files": POSITIVE_INTEGER,
        "max_bytes": POSITIVE_INTEGER,
        "max_import_bytes": POSITIVE_INTEGER,
        "max_generated_file_bytes": POSITIVE_INTEGER,
        "max_depth": POSITIVE_INTEGER,
        "max_pages": POSITIVE_INTEGER,
    }

    checks: CheckSelection = field(default_factory=CheckSelection)
    structure_checks: StructureOptions = field(default_factory=StructureOptions)
    author_records: AuthorRecordOptions = field(default_factory=AuthorRecordOptions)
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
    bbl_checks: BblOptions = field(default_factory=BblOptions)
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
    build_memory_mb: int = DEFAULT_BUILD_MEMORY_MB
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS
    job_timeout_seconds: int = 600
    max_temporary_bytes: int = 2_147_483_648
    source_date_epoch: int | None = None
    max_files: int = MAX_PROJECT_ENTRIES
    # ``max_bytes`` is the default of both byte limits below; read them through
    # ``import_byte_limit`` and ``generated_file_byte_limit``, which apply that fallback.
    max_bytes: int = DEFAULT_MAX_PROJECT_BYTES
    max_import_bytes: int | None = None
    max_generated_file_bytes: int | None = None
    max_depth: int = DEFAULT_MAX_FOLDER_LEVELS
    max_pages: int | None = None

    def __post_init__(self) -> None:
        for name, kind in _OPTION_TYPES.items():
            if not isinstance(getattr(self, name), kind):
                raise SettingsError(f"{name} must be a settings table", (name,))
        validate_scalars(self)
        require_unique_strings(
            "match_source_pdf_metadata",
            self.match_source_pdf_metadata,
            choices=_PDF_METADATA_SOURCES,
        )
        require_text("main", self.main, expected="a nonempty relative filename", allow_none=True)
        self._validate_combinations()

    def _validate_combinations(self) -> None:
        if self.workflow.documents and self.main is not None:
            raise SettingsError(
                "Choose workflow.documents or one main, not both", ("workflow", "main")
            )
        if self.source_transforms.filename_overrides and self.layout != "flat":
            raise SettingsError(
                "filename_overrides requires layout='flat'", ("source_transforms", "layout")
            )
        if {name.title() for name in self.match_source_pdf_metadata} & {
            name for name, _ in self.pdf_checks.expected_metadata
        }:
            raise SettingsError(
                "Choose source matching or an explicit expected PDF value for each metadata field",
                ("match_source_pdf_metadata", "pdf_checks"),
            )
        if self.build_memory_mb + COORDINATOR_MEMORY_MB > self.memory_mb:
            raise SettingsError(
                f"memory_mb must allow build_memory_mb plus {COORDINATOR_MEMORY_MB} MiB "
                "for the coordinator",
                ("memory_mb", "build_memory_mb"),
            )

    @property
    def import_byte_limit(self) -> int:
        """Combined size of the files of one project or archive import and of its build copy.

        ``max_import_bytes`` when set, otherwise ``max_bytes``.
        """
        return self.max_bytes if self.max_import_bytes is None else self.max_import_bytes

    @property
    def generated_file_byte_limit(self) -> int:
        """Size of the largest single file a tool command may write or read.

        ``max_generated_file_bytes`` when set, otherwise ``max_bytes``.
        """
        if self.max_generated_file_bytes is None:
            return self.max_bytes
        return self.max_generated_file_bytes

    def to_dict(self) -> dict[str, object]:
        """The settings in their TOML layout, as reports record them."""
        values = asdict(self)
        formatting = values["formatting_options"]
        line_format = {name: formatting.pop(name) for name in _FORMATTING_KEYS}
        for group, table in SHARED_TABLES.items():
            values[table] = {**values[table], **values[group]}
        return {
            **{
                name: value
                for name, value in values.items()
                if name not in _FLATTENED_GROUPS and name not in SHARED_TABLES
            },
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
# Option groups that a settings file writes inside another group's table, mapped to that
# table: the author-record options of the author_records module are in ``[structure_checks]``
# and the .bbl options of bbl_checks in ``[submission_checks]``. Each module keeps the
# options of its own feature; only this schema knows where the file spells them.
SHARED_TABLES: dict[str, str] = {
    "author_records": "structure_checks",
    "bbl_checks": "submission_checks",
}
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
    name: kind
    for name, kind in _OPTION_TYPES.items()
    if name not in _FLATTENED_GROUPS and name not in SHARED_TABLES
}
# The top-level TOML settings, in their current spelling.
SETTING_NAMES: frozenset[str] = frozenset(
    {
        item.name
        for item in fields(Settings)
        if item.name not in _FLATTENED_GROUPS and item.name not in SHARED_TABLES
    }
    | set(_TOP_LEVEL_FIELDS)
)


def toml_table(group: str) -> str:
    """The TOML table in which a settings file writes the option group ``group``."""
    return SHARED_TABLES.get(group, group)


def effective_settings(settings: Settings) -> Settings:
    """Disable unselected configured policies without changing the user's Settings.

    Unchanged objects are returned by identity, including the default ALL policy.
    """
    updates = unselected_policy_updates(settings.top_level_values(), settings.checks)
    return settings.with_top_level_values(updates) if updates else settings


def _unknown_settings_error(
    unknown: list[str], known: frozenset[str], path: Path | None
) -> RequestError:
    guesses = closest_names(unknown, known)
    where = f" in {path}" if path is not None else ""
    message = f"Unknown setting(s){where}: " + with_suggestions(unknown, guesses)
    fix = f"Rename or remove {'it' if len(unknown) == 1 else 'them'}{where}."
    if len(guesses) < len(unknown):
        fix += f" Valid top-level settings: {', '.join(sorted(known))}."
    return RequestError(message, fix)


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
            raise SettingsError(f"Set {current} only; {former} is its former name", (current,))
        result[current] = value
    return result


def _line_format(values: dict[str, Any], table: object) -> dict[str, Any]:
    """Top-level wrap_length and tab_size, which belong to the formatting options."""
    if isinstance(table, dict) and (nested := sorted(set(_FORMATTING_KEYS) & set(table))):
        raise SettingsError(
            f"Set {', '.join(nested)} at the top level, not in formatting_options",
            (*nested, "formatting_options"),
        )
    return {name: values.pop(name) for name in _FORMATTING_KEYS if name in values}


def _build_settings(values: Mapping[str, Any], path: Path | None) -> Settings:
    """Construct Settings from TOML-layout values that use only current names.

    ``values`` is left unchanged; the keys are consumed from a copy.
    """
    remaining = dict(values)
    line_format = _line_format(remaining, remaining.get("formatting_options"))
    tables: dict[str, Any] = {}
    for name, kind in _OPTION_TABLES.items():
        if name in remaining:
            value = remaining.pop(name)
            if name == "bibliography_transform":
                value = _bibliography_edit_tables(value)
            sharing = [group for group, table in SHARED_TABLES.items() if table == name]
            if sharing:
                kinds = [kind, *(_OPTION_TYPES[group] for group in sharing)]
                tables.update(
                    zip([name, *sharing], read_option_groups(kinds, value, name), strict=True)
                )
            else:
                tables[name] = read_options(kind, value, name)
    if line_format:
        # The options validate these values with the messages of top-level settings.
        formatting = tables.get("formatting_options", FormattingOptions())
        tables["formatting_options"] = replace(formatting, **line_format)
    grouped: dict[str, dict[str, Any]] = {group: {} for group in _FLATTENED_GROUPS}
    for name in [name for name in remaining if _TOP_LEVEL_FIELDS.get(name) in grouped]:
        grouped[_TOP_LEVEL_FIELDS[name]][name] = remaining.pop(name)
    if isinstance(metadata := remaining.get("match_source_pdf_metadata"), list):
        remaining["match_source_pdf_metadata"] = tuple(metadata)
    settings = Settings(
        **remaining,
        **tables,
        manuscript=read_top_level_options(ManuscriptOptions, grouped["manuscript"]),
        pdf=read_top_level_options(PdfInspectionOptions, grouped["pdf"]),
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
    values = read_settings_file(path) if path is not None else {}
    return _settings_from_values(values, overrides, path=path, preset=preset)


def parse_settings_text(text: str, path: Path) -> Settings:
    """Validate settings text as ``load_settings`` would read it from the file ``path``."""
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PreparationError(f"Cannot read settings {path}: {error}") from error
    return _settings_from_values(values, {}, path=path, preset=None)
