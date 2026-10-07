"""Validated TOML settings and bounded project configuration discovery."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, cast

from .bibliography_checks import BibliographyOptions
from .bibliography_transform import BibliographyTransformOptions
from .build_checks import BuildCheckOptions
from .check_selection import CheckSelection
from .formatting import FormattingOptions
from .manuscript import ManuscriptOptions
from .manuscript_checks import ManuscriptCheckOptions
from .metadata_privacy import MetadataPrivacyOptions
from .models import PreparationError
from .online_checks import OnlineOptions
from .option_config import read_options
from .pdf import PdfOptions
from .pdf_artwork import PdfArtworkOptions
from .pdf_checks import PdfCheckOptions
from .reporting import ReportingOptions
from .source_transform import SourceTransformOptions
from .structure_checks import StructureOptions
from .submission_checks import SubmissionOptions
from .workflow_options import WorkflowOptions

PARENT_MEMORY_MB = 128


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


@dataclass(frozen=True)
class Settings:
    checks: CheckSelection = field(default_factory=CheckSelection)
    structure_checks: StructureOptions = field(default_factory=StructureOptions)
    metadata_privacy: MetadataPrivacyOptions = field(default_factory=MetadataPrivacyOptions)
    formatting_options: FormattingOptions = field(default_factory=FormattingOptions)
    source_transforms: SourceTransformOptions = field(default_factory=SourceTransformOptions)
    bibliography_transform: BibliographyTransformOptions = field(
        default_factory=BibliographyTransformOptions
    )
    pdf_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    figure_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    reporting: ReportingOptions = field(default_factory=ReportingOptions)
    workflow: WorkflowOptions = field(default_factory=WorkflowOptions)
    bibliography_checks: BibliographyOptions = field(default_factory=BibliographyOptions)
    build_checks: BuildCheckOptions = field(default_factory=BuildCheckOptions)
    manuscript_checks: ManuscriptCheckOptions = field(default_factory=ManuscriptCheckOptions)
    online_checks: OnlineOptions = field(default_factory=OnlineOptions)
    pdf_checks: PdfCheckOptions = field(default_factory=PdfCheckOptions)
    submission_checks: SubmissionOptions = field(default_factory=SubmissionOptions)
    match_source_pdf_metadata: tuple[str, ...] = ()
    main: str | None = None
    layout: str = "preserve"
    engine: str = "pdflatex"
    bibliography_backend: str = "auto"
    format: bool = False
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
    wrap_length: int = 80
    tab_size: int = 2
    expected_document_class: str | None = None
    required_class_options: tuple[str, ...] = ()
    forbidden_class_options: tuple[str, ...] = ()
    forbidden_packages: tuple[str, ...] = ()
    require_abstract: bool = False
    abstract_min_words: int | None = None
    abstract_max_words: int | None = None
    keywords_min_count: int | None = None
    keywords_max_count: int | None = None
    required_sections: tuple[str, ...] = ()
    expected_page_width_pt: float | None = None
    expected_page_height_pt: float | None = None
    page_size_tolerance_pt: float = 1.0
    require_consistent_page_size: bool = False
    min_image_dpi: float | None = None
    require_embedded_fonts: bool = False
    forbid_type3_fonts: bool = False
    forbid_encryption: bool = False
    forbid_forms: bool = False
    forbid_javascript: bool = False
    forbid_attachments: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def manuscript_options(self) -> ManuscriptOptions:
        return ManuscriptOptions(
            **{item.name: getattr(self, item.name) for item in fields(ManuscriptOptions)}
        )

    def pdf_options(self) -> PdfOptions:
        return PdfOptions(**{item.name: getattr(self, item.name) for item in fields(PdfOptions)})


def _read_settings(path: Path, *, required: bool = True) -> dict[str, object] | None:
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PreparationError(f"Cannot read settings {path}: {error}") from error
    if path.name != "pyproject.toml":
        return values
    tool = values.get("tool", {})
    if not isinstance(tool, dict) or "latex-prep" not in tool:
        if required:
            raise PreparationError(f"Settings {path} has no [tool.latex-prep] table")
        return None
    settings = tool["latex-prep"]
    if not isinstance(settings, dict):
        raise PreparationError(f"[tool.latex-prep] in {path} must be a settings table")
    return settings


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
        for name in (".latex-prep.toml", "latex-prep.toml", "pyproject.toml"):
            candidate = directory / name
            if candidate.is_file() and (
                name != "pyproject.toml" or _read_settings(candidate, required=False) is not None
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


def load_settings(path: Path | None, overrides: dict[str, object]) -> Settings:
    """Load one selected file and apply non-None CLI/API overrides last."""
    values = _read_settings(path) if path is not None else {}
    assert values is not None
    known = {item.name for item in fields(Settings)}
    unknown = (set(values) | set(overrides)) - known
    if unknown:
        raise PreparationError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    values.update({key: value for key, value in overrides.items() if value is not None})
    defaults = Settings().to_dict()
    defaults.update(values)
    grouped = {
        "checks": CheckSelection,
        "structure_checks": StructureOptions,
        "metadata_privacy": MetadataPrivacyOptions,
        "formatting_options": FormattingOptions,
        "source_transforms": SourceTransformOptions,
        "bibliography_transform": BibliographyTransformOptions,
        "pdf_artwork": PdfArtworkOptions,
        "figure_artwork": PdfArtworkOptions,
        "reporting": ReportingOptions,
        "workflow": WorkflowOptions,
        "bibliography_checks": BibliographyOptions,
        "build_checks": BuildCheckOptions,
        "manuscript_checks": ManuscriptCheckOptions,
        "online_checks": OnlineOptions,
        "pdf_checks": PdfCheckOptions,
        "submission_checks": SubmissionOptions,
    }
    for name, kind in grouped.items():
        if name == "bibliography_transform":
            defaults[name] = _bibliography_edit_tables(defaults[name])
        defaults[name] = read_options(kind, defaults[name], name)
    match_metadata = defaults["match_source_pdf_metadata"]
    if (
        not isinstance(match_metadata, (tuple, list))
        or any(
            not isinstance(item, str) or item not in {"title", "author"} for item in match_metadata
        )
        or len(set(match_metadata)) != len(match_metadata)
    ):
        raise PreparationError("match_source_pdf_metadata must list unique title/author fields")
    defaults["match_source_pdf_metadata"] = tuple(match_metadata)
    if path is not None:
        workflow = cast(WorkflowOptions, defaults["workflow"])
        defaults["workflow"] = replace(
            workflow,
            documents=tuple(
                replace(
                    document,
                    reference_pdf=str(
                        (path.parent / Path(document.reference_pdf).expanduser()).resolve()
                    ),
                )
                if document.reference_pdf is not None
                else document
                for document in workflow.documents
            ),
        )
        submission = cast(SubmissionOptions, defaults["submission_checks"])
        defaults["submission_checks"] = replace(
            submission,
            template_references=tuple(
                (project, str((path.parent / Path(reference).expanduser()).resolve()))
                for project, reference in submission.template_references
            ),
        )
    constraint_names = {item.name for item in fields(ManuscriptOptions)} | {
        item.name for item in fields(PdfOptions)
    }
    for key in (
        "required_class_options",
        "forbidden_class_options",
        "forbidden_packages",
        "required_sections",
    ):
        value = defaults[key]
        if not isinstance(value, (tuple, list)):
            raise PreparationError(f"{key} must be an array of unique nonempty strings")
        defaults[key] = tuple(value)
    for key, value in defaults.items():
        if key in constraint_names or key in grouped or key == "match_source_pdf_metadata":
            # The typed option objects validate direct API and TOML use identically.
            continue
        elif key == "main":
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise PreparationError("main must be a nonempty relative filename")
        elif key in {"layout", "engine", "bibliography_backend"}:
            allowed = {
                "layout": {"preserve", "flat"},
                "engine": {"pdflatex", "xelatex", "lualatex"},
                "bibliography_backend": {"auto", "bibtex", "biber"},
            }[key]
            if not isinstance(value, str) or value not in allowed:
                raise PreparationError(f"{key} must be one of {', '.join(sorted(allowed))}")
        elif key in {"format", "normalize_doi", "cleanup"}:
            if type(value) is not bool:
                raise PreparationError(f"{key} must be a boolean")
        elif key == "max_pages" and value is None:
            continue
        elif key == "source_date_epoch":
            if value is not None and (type(value) is not int or not 0 <= value <= 253402300799):
                raise PreparationError(
                    "source_date_epoch must be a nonnegative supported Unix timestamp"
                )
        elif type(value) is not int or value < 1:
            raise PreparationError(f"{key} must be a positive integer")
    settings = Settings(**cast(dict[str, Any], defaults))
    if settings.workflow.documents and settings.main is not None:
        raise PreparationError("Choose workflow.documents or one main, not both")
    if settings.source_transforms.filename_overrides and settings.layout != "flat":
        raise PreparationError("filename_overrides requires layout='flat'")
    if {name.title() for name in settings.match_source_pdf_metadata} & {
        name for name, _ in settings.pdf_checks.expected_metadata
    }:
        raise PreparationError(
            "Choose source matching or an explicit expected PDF value for each metadata field"
        )
    settings.manuscript_options()
    settings.pdf_options()
    if settings.jobs > 64:
        raise PreparationError("jobs must be at most 64")
    if settings.build_jobs > 64 or settings.render_jobs > 64:
        raise PreparationError("build_jobs and render_jobs must be at most 64")
    if settings.memory_mb < 256 or settings.build_memory_mb < 128:
        raise PreparationError("memory_mb must be >=256 and build_memory_mb must be >=128")
    if settings.build_memory_mb + PARENT_MEMORY_MB > settings.memory_mb:
        raise PreparationError(
            f"memory_mb must allow build_memory_mb plus {PARENT_MEMORY_MB} MiB for the coordinator"
        )
    if settings.wrap_length < 20 or settings.tab_size > 16:
        raise PreparationError("wrap_length must be >=20 and tab_size must be <=16")
    return settings
