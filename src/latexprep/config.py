"""Validated settings: the ``Settings`` schema, and layering preset, file and command line.

Owns what the settings are called, how they are validated, and the precedence presets <
settings file < command-line overrides. Finding and reading the settings file is in
``config_discovery``.
"""

from __future__ import annotations

import re
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
from .online_options import CONTACT_EMAIL_PLACEHOLDER, OnlineOptions
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
from .resource_monitor import WORKSPACE_TOTAL_FILE_LIMITS
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
    """Turn the named field-edit tables of ``bibliography_transform`` into edit tuples.

    TOML has no null: named edit tables express absent fields explicitly.

    Returns:
        ``value`` with each table row of ``field_edits`` as a ``(key, field, expected,
        replacement)`` tuple; any other value is returned unchanged.

    Raises:
        PreparationError: A field-edit table lacks ``key`` or ``field``, has other keys,
            does not choose exactly one of ``expected`` and ``expected_absent = true`` or
            of ``replacement`` and ``remove = true``, or gives either flag a value other
            than true.
    """
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
    Construction raises ``SettingsError`` when an option group is not a settings table, a
    value is invalid, or two settings conflict.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to the numeric settings.
        checks: The check selection.
        structure_checks: The options of the structure checks.
        author_records: The options of the per-author field checks.
        metadata_privacy: The options of the image metadata privacy check.
        formatting_options: The formatting options.
        source_transforms: The options of the source transformations.
        bibliography_transform: The options of the bibliography transformation.
        rendered_pdf_artwork: The artwork policy for the rendered document.
        included_figure_artwork: The artwork policy for each included PDF figure.
        reporting: The review decisions and reporting options.
        workflow: The workflow options, including ``workflow.documents``.
        package: The options of the package contents.
        bibliography_checks: The options of the bibliography checks.
        build_checks: The options of the build checks.
        manuscript_checks: The options of the manuscript checks.
        online_checks: The options of the online checks.
        pdf_checks: The options of the configured PDF checks.
        submission_checks: The options of the submission checks.
        bbl_checks: The options of the ``.bbl`` checks.
        manuscript: The manuscript constraints.
        pdf: The PDF inspection constraints.
        match_source_pdf_metadata: The source-document fields (``title``, ``author``)
            that a PDF property must match.
        main: The main file, or ``None``.
        layout: The package layout.
        engine: The TeX engine.
        bibliography_backend: The bibliography tool, or ``auto``.
        apply_formatting: Whether preparation applies the formatting (``--format``).
        normalize_doi: Whether preparation normalizes DOIs.
        cleanup: Whether preparation removes debris (``--no-cleanup`` turns it off).
        jobs: The shared CPU slots.
        build_jobs: How many builds may overlap.
        render_jobs: How many page-pair renderers may run at once.
        memory_mb: The memory shared by all running tools, in MiB.
        build_memory_mb: The memory of one build command, in MiB.
        timeout_seconds: The time limit of one tool command, in seconds.
        job_timeout_seconds: The deadline of the whole job, in seconds.
        max_temporary_bytes: The temporary data the whole job may hold, in bytes.
        source_date_epoch: A fixed source date for the builds, in seconds since the
            epoch, or ``None``.
        max_files: How many files and folders together an import may hold.
        max_bytes: The fallback of the two byte limits below.
        max_import_bytes: The combined size of an import, or ``None`` for ``max_bytes``.
        max_generated_file_bytes: The size of any one file a tool writes or opens, or ``None``
            for ``max_bytes``.
        max_depth: How many folder levels an import may hold.
        max_pages: The page limit of the document, or ``None``.
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
        """Reject settings that contradict each other.

        Raises:
            SettingsError: Two settings conflict, or a byte or memory limit leaves too
                little room for another; ``settings`` names those at fault.
        """
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
        if self.import_byte_limit > WORKSPACE_TOTAL_FILE_LIMITS * self.generated_file_byte_limit:
            raise SettingsError(
                f"max_import_bytes ({self.import_byte_limit}) must not exceed "
                f"{WORKSPACE_TOTAL_FILE_LIMITS} times max_generated_file_bytes "
                f"({self.generated_file_byte_limit}): a tool workspace holds at most that much "
                "in total, and the copy of the sources counts toward it. Lower "
                "max_import_bytes or raise max_generated_file_bytes (each is max_bytes unless "
                "set)",
                ("max_import_bytes", "max_generated_file_bytes"),
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
        """Return the settings in their TOML layout, as reports record them.

        A configured ``online_contact_email`` is replaced by a placeholder: it is a personal
        address that a shared report, HTML file or diagnostics bundle must not carry.
        """
        values = asdict(self)
        if values["online_checks"]["online_contact_email"]:
            values["online_checks"]["online_contact_email"] = CONTACT_EMAIL_PLACEHOLDER
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
        """List every top-level TOML setting with its value; a table maps to its option object."""
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in _FLATTENED_GROUPS
        }
        for name, group in _TOP_LEVEL_FIELDS.items():
            values[name] = getattr(getattr(self, group), name)
        return values

    def with_top_level_values(self, updates: Mapping[str, object]) -> Settings:
        """Return a copy with top-level TOML settings replaced.

        Args:
            updates: The new values, named as ``top_level_values`` names them.

        Returns:
            The copy.
        """
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
    """Name the TOML table in which a settings file writes the option group ``group``."""
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
    """Build the error for unknown settings, suggesting the closest valid names.

    Args:
        unknown: The unknown names.
        known: The valid top-level settings.
        path: The settings file, which the message names, or ``None``.

    Returns:
        The error; its next step lists every valid setting when a name has no suggestion.
    """
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
    """Accept each former setting name as an alias of its replacement, but not both.

    Returns:
        The values under their current names.

    Raises:
        SettingsError: A setting is given under both names.
    """
    result: dict[str, Any] = {}
    for name, value in values.items():
        current = RENAMED_SETTINGS.get(name, name)
        if current in result:
            former = next(old for old, new in RENAMED_SETTINGS.items() if new == current)
            raise SettingsError(f"Set {current} only; {former} is its former name", (current,))
        result[current] = value
    return result


def _as_written(error: SettingsError, written: Mapping[str, str]) -> SettingsError:
    """Rename each renamed setting in ``error`` as the settings file spells it.

    Validation works with current names, so without this a file with ``[pdf_artwork]``
    would be told about ``rendered_pdf_artwork``, a key it never wrote.

    Args:
        error: The error of validation.
        written: The former name the file uses, by current setting name.

    Returns:
        The error with the former names, or ``error`` itself when nothing was renamed.
    """
    if not written:
        return error
    names = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, written)) + r")(?!\w)")
    return SettingsError(
        names.sub(lambda match: written[match.group()], str(error)),
        tuple(written.get(name, name) for name in error.settings),
    )


def _line_format(values: dict[str, Any], table: object) -> dict[str, Any]:
    """Take the top-level wrap_length and tab_size, which belong to the formatting options.

    Args:
        values: The top-level values; the two settings are removed from them.
        table: The ``formatting_options`` table as written.

    Returns:
        The two settings that were given.

    Raises:
        SettingsError: ``table`` sets either of them.
    """
    if isinstance(table, dict) and (nested := sorted(set(_FORMATTING_KEYS) & set(table))):
        raise SettingsError(
            f"Set {', '.join(nested)} at the top level, not in formatting_options",
            (*nested, "formatting_options"),
        )
    return {name: values.pop(name) for name in _FORMATTING_KEYS if name in values}


def _build_settings(values: Mapping[str, Any], path: Path | None) -> Settings:
    """Construct Settings from TOML-layout values that use only current names.

    Args:
        values: The values; left unchanged, because the keys are consumed from a copy.
        path: The settings file, from whose directory relative file references are
            resolved, or ``None``.

    Returns:
        The validated settings.

    Raises:
        SettingsError: A value is invalid.
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
        """Resolve ``name``, after expanding a leading ``~``, against ``directory``."""
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
    """Validate settings from the values of a file, a preset and overrides.

    The file values override the preset, and the overrides that are not ``None`` override
    both. Former setting names are accepted, and an error names each setting as the file
    spells it.

    Args:
        values: The raw values of the settings file.
        overrides: Values from the command line or the API; ``None`` means not given.
        path: The settings file, or ``None``.
        preset: The preset to start from, or ``None``.

    Returns:
        The validated settings.

    Raises:
        RequestError: A setting is unknown.
        SettingsError: A value is invalid or a setting is given under two names.
    """
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
    try:
        return _build_settings(selected, path)
    except SettingsError as error:
        # A setting the command line or the API supplied is named as it was given.
        written = {
            RENAMED_SETTINGS[name]: name
            for name in values
            if name in RENAMED_SETTINGS and RENAMED_SETTINGS[name] not in requested
        }
        raise _as_written(error, written) from error


def load_settings(
    path: Path | None, overrides: Mapping[str, object], *, preset: str | None = None
) -> Settings:
    """Layer an optional preset, then one selected file, then non-``None`` CLI/API overrides.

    Args:
        path: The selected settings file, or ``None``.
        overrides: Values from the command line or the API; ``None`` means not given.
        preset: The preset to start from, or ``None``.

    Returns:
        The validated settings.

    Raises:
        RequestError: The file does not exist, or a setting is unknown.
        SettingsError: A value is invalid.
        PreparationError: The file cannot be read or is not valid TOML.
    """
    values = read_settings_file(path) if path is not None else {}
    return _settings_from_values(values, overrides, path=path, preset=preset)


def parse_settings_text(text: str, path: Path) -> Settings:
    """Validate settings text as ``load_settings`` would read it from the file ``path``.

    Args:
        text: The TOML text.
        path: The settings file the text stands for; relative file references are
            resolved from its directory.

    Returns:
        The validated settings.

    Raises:
        PreparationError: The text is not valid TOML.
        RequestError: A setting is unknown.
        SettingsError: A value is invalid.
    """
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PreparationError(f"Cannot read settings {path}: {error}") from error
    return _settings_from_values(values, {}, path=path, preset=None)
