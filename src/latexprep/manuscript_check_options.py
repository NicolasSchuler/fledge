"""The ``[manuscript_checks]`` settings: which opt-in manuscript policies run.

Each option switches on one literal-source policy (package allowlist, abstract
rules, heading structure, metadata, floats, line numbers) or supplies the value a
policy compares against. This module owns only the options and their validation; the
policies themselves are in the ``manuscript_*_checks`` modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from .errors import SettingsError
from .literal_text import HEADING_DEPTHS, literal_list
from .manuscript_vocabulary import METADATA_COMMANDS
from .option_config import Bounds, require_unique_strings, validate_scalars

AbbreviationPolicy = Literal["forbid", "define"]


@dataclass(frozen=True)
class ManuscriptCheckOptions:
    r"""Which opt-in manuscript policies run, and the values they compare against.

    Construction raises ``SettingsError`` when a value is invalid or repeated, when
    ``allowed_packages`` holds anything but single literal package names, or when line
    numbers are both required and forbidden.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to ``max_heading_depth``,
            which must be a heading depth.
        allowed_packages: The direct packages the allowlist policy accepts, or ``None`` for
            no allowlist; an allowlist also produces the package inventory.
        inventory_packages: Whether to report the direct package inventory.
        check_layout_manipulation: Whether to report layout override candidates.
        forbid_abstract_citations: Whether citation commands in the abstract fail.
        abstract_abbreviation_policy: ``forbid`` reports every uppercase abbreviation in
            the abstract, ``define`` only those without a definition candidate; ``None``
            switches the advisory off.
        abstract_abbreviation_exceptions: Abbreviations the abstract policy never reports.
        max_heading_depth: The deepest heading level allowed, or ``None`` for no limit.
        check_empty_sections: Whether to report section subtrees with no observable content.
        check_hardcoded_references: Whether to report hard-coded figure, table, section and
            equation numbers.
        required_metadata: The metadata fields that must have a literal value.
        check_orcid: Whether to check the syntax and checksum of literal ORCID identifiers.
        required_declarations: The names of the declarations that must be present as a
            nonempty section or ``\declaration{name}{text}``.
        require_float_captions: Whether every figure and table needs a nonempty caption.
        require_float_labels: Whether every figure and table needs a label.
        check_float_label_order: Whether a float label must follow the caption.
        require_float_references: Whether every float needs an external reference.
        check_float_reference_order: Whether floats must be first referenced in source order.
        require_figure_descriptions: Whether every figure needs a usable ``Description``.
        require_line_numbers: Whether literal line-number activation is required.
        forbid_line_numbers: Whether literal line-number activation is forbidden.
    """

    # Heading depths run from part (-1) to subparagraph (5).
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "max_heading_depth": Bounds(min(HEADING_DEPTHS.values()), max(HEADING_DEPTHS.values()))
    }

    allowed_packages: tuple[str, ...] | None = None
    inventory_packages: bool = False
    check_layout_manipulation: bool = False
    forbid_abstract_citations: bool = False
    abstract_abbreviation_policy: AbbreviationPolicy | None = None
    abstract_abbreviation_exceptions: tuple[str, ...] = ()
    max_heading_depth: int | None = None
    check_empty_sections: bool = False
    check_hardcoded_references: bool = False
    required_metadata: tuple[str, ...] = ()
    check_orcid: bool = False
    required_declarations: tuple[str, ...] = ()
    require_float_captions: bool = False
    require_float_labels: bool = False
    check_float_label_order: bool = False
    require_float_references: bool = False
    check_float_reference_order: bool = False
    require_figure_descriptions: bool = False
    require_line_numbers: bool = False
    forbid_line_numbers: bool = False

    def __post_init__(self) -> None:
        validate_scalars(self)
        if self.require_line_numbers and self.forbid_line_numbers:
            raise SettingsError(
                "Choose require_line_numbers or forbid_line_numbers, not both",
                ("require_line_numbers", "forbid_line_numbers"),
            )
        require_unique_strings("allowed_packages", self.allowed_packages, allow_none=True)
        for name in ("abstract_abbreviation_exceptions", "required_declarations"):
            require_unique_strings(name, getattr(self, name))
        require_unique_strings(
            "required_metadata", self.required_metadata, choices=METADATA_COMMANDS
        )
        if self.allowed_packages is not None and any(
            literal_list(value) != [value] for value in self.allowed_packages
        ):
            raise SettingsError(
                "allowed_packages must contain single literal package names", ("allowed_packages",)
            )
