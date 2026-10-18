"""Options of the metadata privacy checks: selected source edits and identity terms.

Owns ``MetadataEdit`` (one explicit literal replacement in a TeX source), the
``metadata_privacy`` settings table, and the text normalization both checks match with.

Does not own the checks themselves: ``metadata_source_edits`` proposes the edits and
rechecks the final PDF properties, and ``image_metadata`` scans PNG and JPEG metadata for
the identity terms.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal, NamedTuple

from .errors import PreparationError, SettingsError
from .option_config import (
    require_records,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .path_safety import is_relative_posix_path
from .tex_commands import ROOT_SUFFIXES
from .text_safety import is_unsafe_character

MetadataField = Literal[
    "title",
    "author",
    "email",
    "affiliation",
    "hypersetup.pdftitle",
    "hypersetup.pdfauthor",
    "hypersetup.pdfsubject",
    "hypersetup.pdfkeywords",
]

_MAX_EDITS: Final = 256  # selected source metadata edits
_MAX_IDENTITY_TERMS: Final = 256
_MAX_TERM_CHARACTERS: Final = 1024  # characters in one identity term
_MAX_EDIT_TEXT_CHARACTERS: Final = 65536  # characters in an edit's expected or replacement text
_TEX_SPECIAL_CHARACTERS: Final = "\\{}%#$&^~"
_ALLOWED_WHITESPACE: Final = "\r\n\t"


class FieldTarget(NamedTuple):
    """Where a selected field lives in the source and in the final PDF."""

    command: str | None = None  # a TeX command such as \author
    hyperref_key: str | None = None  # a key inside \hypersetup{...}
    pdf_property: str | None = None  # the PDF document-information property it sets


FIELD_TARGETS: Final[dict[str, FieldTarget]] = {
    "title": FieldTarget(command="title"),
    "author": FieldTarget(command="author"),
    "email": FieldTarget(command="email"),
    "affiliation": FieldTarget(command="affiliation"),
    "hypersetup.pdftitle": FieldTarget(hyperref_key="pdftitle", pdf_property="Title"),
    "hypersetup.pdfauthor": FieldTarget(hyperref_key="pdfauthor", pdf_property="Author"),
    "hypersetup.pdfsubject": FieldTarget(hyperref_key="pdfsubject", pdf_property="Subject"),
    "hypersetup.pdfkeywords": FieldTarget(hyperref_key="pdfkeywords", pdf_property="Keywords"),
}


def normalize_for_matching(value: str) -> str:
    """Compatibility-fold, casefold and collapse whitespace, for comparing text to a term."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_literal_metadata_text(value: str) -> bool:
    """Whether ``value`` is plain text: no TeX specials and no control characters but line ends."""
    return not any(char in value for char in _TEX_SPECIAL_CHARACTERS) and not any(
        is_unsafe_character(char) and char not in _ALLOWED_WHITESPACE for char in value
    )


@dataclass(frozen=True)
class MetadataEdit:
    """One explicit replacement of a metadata declaration in a TeX source file.

    The edit applies only when the declaration holds ``expected_before`` exactly.
    """

    path: str
    field: MetadataField
    expected_before: str
    replacement: str

    def __post_init__(self) -> None:
        require_text("path", self.path, expected="a safe relative TeX manuscript path")
        if not is_relative_posix_path(self.path, characters="literal") or (
            PurePosixPath(self.path).suffix.lower() not in ROOT_SUFFIXES
        ):
            raise SettingsError("path must be a safe relative TeX manuscript path", ("path",))
        validate_scalars(self)
        for name in ("expected_before", "replacement"):
            value = getattr(self, name)
            require_text(
                name,
                value,
                expected="bounded plain literal text",
                max_length=_MAX_EDIT_TEXT_CHARACTERS,
                allow_blank=True,
            )
            if not is_literal_metadata_text(value):
                raise SettingsError(
                    f"{name} must be plain literal text without TeX commands or control characters",
                    (name,),
                )

    @property
    def target(self) -> FieldTarget:
        return FIELD_TARGETS[self.field]

    @property
    def command(self) -> str | None:
        """The TeX command that declares the field, or None for a hyperref key."""
        return self.target.command

    @property
    def hyperref_key(self) -> str | None:
        """The ``\\hypersetup`` key that declares the field, or None for a command."""
        return self.target.hyperref_key

    @property
    def pdf_property(self) -> str | None:
        """The PDF property the field sets (``Title``, ``Author`` ...), if it sets one."""
        return self.target.pdf_property

    @property
    def label(self) -> str:
        """How messages name the field: its command or hyperref key."""
        key = self.hyperref_key
        return f"hyperref key {key}" if key is not None else f"\\{self.command}"


@dataclass(frozen=True)
class MetadataPrivacyOptions:
    """Selected source edits, and the identity terms that image metadata must not contain."""

    edits: tuple[MetadataEdit, ...] = ()
    image_identity_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_records(
            "edits",
            self.edits,
            MetadataEdit,
            max_items=_MAX_EDITS,
            distinct_by=lambda edit: (edit.path, edit.field),
            distinct_description="source metadata field",
        )
        require_unique_strings(
            "image_identity_terms",
            self.image_identity_terms,
            max_items=_MAX_IDENTITY_TERMS,
            max_length=_MAX_TERM_CHARACTERS,
            allow_surrounding_whitespace=True,
        )
        expected: dict[str, str] = {}
        for edit in self.edits:
            if edit.pdf_property is not None:
                if edit.field in expected and expected[edit.field] != edit.replacement:
                    raise PreparationError("Selected edits disagree on one final PDF property")
                expected[edit.field] = edit.replacement
