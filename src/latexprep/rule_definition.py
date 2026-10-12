"""The shape of one catalogued check; data-only ``*_rules.py`` modules build these."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class Rule:
    code: str
    name: str
    title: str
    description: str
    tests: tuple[str, ...]
    fix: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
