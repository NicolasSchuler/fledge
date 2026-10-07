"""Validated public check selectors, independent of checker configuration."""

from __future__ import annotations

from dataclasses import dataclass

from .models import PreparationError
from .rules import BY_CODE

_SELECTORS = frozenset(
    code[:length]
    for code in BY_CODE
    for length in range(len(code.rstrip("0123456789")), len(code) + 1)
) | {"ALL"}


@dataclass(frozen=True)
class CheckSelection:
    """Filter configured checks; selection never supplies missing options or consent."""

    select: tuple[str, ...] = ("ALL",)
    ignore: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("select", "ignore"):
            selectors = getattr(self, name)
            if not isinstance(selectors, tuple) or any(
                not isinstance(selector, str) for selector in selectors
            ):
                raise PreparationError(f"checks.{name} must be an array of check selectors")
            for selector in selectors:
                if selector not in _SELECTORS:
                    raise PreparationError(
                        f"Unknown checks.{name} selector {selector!r}; use ALL, a public "
                        "check code, or a matching family/number prefix from fledge rules"
                    )

    def enabled(self, code: str) -> bool:
        """Use the most specific selector; ignore wins equally specific matches."""
        if code not in BY_CODE:
            raise ValueError(f"Unknown public check code: {code}")

        def specificity(selectors: tuple[str, ...]) -> int:
            return max(
                (
                    0 if selector == "ALL" else len(selector)
                    for selector in selectors
                    if selector == "ALL" or code.startswith(selector)
                ),
                default=-1,
            )

        return specificity(self.select) > specificity(self.ignore)
