"""Settings of the offline bibliography checks, validated when they are constructed.

This module owns ``BibliographyOptions`` and the rules for its entry-type and field
requirement lists. It runs no check and reads no file; ``bibliography_checks`` and the
other bibliography modules read these options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from .bibtex_syntax import LOWERCASE_NAME
from .errors import SettingsError
from .option_config import (
    NONZERO_FRACTION,
    Bounds,
    TextFormat,
    require_pairs,
    require_text,
    require_unique_strings,
    validate_scalars,
)

_ENTRY_TYPE_OR_ANY = TextFormat(
    re.compile(rf"\*|{LOWERCASE_NAME.pattern}"), "a lowercase entry type or '*'"
)
# A field name, or alternatives of which one suffices, such as "doi|url".
_FIELD_OR_ALTERNATIVES = TextFormat(
    re.compile(rf"{LOWERCASE_NAME.pattern}(?:\|{LOWERCASE_NAME.pattern})*"),
    "a lowercase field name or alternatives such as 'doi|url'",
)


def _require_entry_type(name: str, value: object) -> None:
    require_text(
        name, value, expected=_ENTRY_TYPE_OR_ANY.description, pattern=_ENTRY_TYPE_OR_ANY.pattern
    )


def _require_field_alternatives(name: str, specs: object) -> None:
    require_unique_strings(name, specs, item_format=_FIELD_OR_ALTERNATIVES)
    if not isinstance(specs, tuple) or not specs:
        raise SettingsError(f"{name} must list at least one field", (name,))
    for spec in specs:
        alternatives = spec.split("|")
        if len(set(alternatives)) != len(alternatives):
            raise SettingsError(f"{name} entry {spec!r} repeats an alternative", (name,))


@dataclass(frozen=True)
class BibliographyOptions:
    """Which offline bibliography checks run and how strictly; all values are immutable."""

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {"fuzzy_duplicate_threshold": NONZERO_FRACTION}

    check_citation_coverage: bool = True
    check_uncited_entries: bool = True
    all_roots: bool = False
    required_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    doi_entry_types: tuple[str, ...] = ()
    check_page_ranges: bool = True
    check_urls: bool = True
    check_capitalization: bool = False
    check_fuzzy_duplicates: bool = False
    fuzzy_duplicate_threshold: float = 0.93

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings(
            "doi_entry_types", self.doi_entry_types, item_format=_ENTRY_TYPE_OR_ANY
        )
        require_pairs(
            "required_fields",
            self.required_fields,
            first=_require_entry_type,
            second=_require_field_alternatives,
        )

    @property
    def any_enabled(self) -> bool:
        """Whether any check runs; ``all_roots`` and the threshold only refine them."""
        return any(
            (
                self.check_citation_coverage,
                self.check_uncited_entries,
                self.required_fields,
                self.doi_entry_types,
                self.check_page_ranges,
                self.check_urls,
                self.check_capitalization,
                self.check_fuzzy_duplicates,
            )
        )
