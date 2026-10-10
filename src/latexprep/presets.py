"""Opt-in example settings files shipped as package data.

A preset is an ordinary commented settings file that only switches on existing
generic checks and options. It is a starting point the user selects explicitly,
not a maintained venue policy: limits and identity terms remain placeholders the
user fills in. ``--preset`` applies one beneath the selected settings file and
command-line options; ``fledge init --preset`` copies one for editing.
"""

from __future__ import annotations

import tomllib
from importlib import resources

from .models import PreparationError

# One-line descriptions for `fledge init --list`; each name has a <name>.toml file.
PRESETS: dict[str, str] = {
    "anonymous-review": "Double-blind review: identity hints, your identity terms, float structure",
    "arxiv": "Public source upload: shipped .bbl, private comments, secrets, unused files, fonts",
    "camera-ready": "Final version: strict PDF fonts and page size, metadata, float structure",
}


def preset_names() -> tuple[str, ...]:
    return tuple(sorted(PRESETS))


def _unknown(name: str) -> PreparationError:
    return PreparationError(
        f"Unknown preset {name!r}; choose one of {', '.join(preset_names())} "
        "(fledge init --list describes them)"
    )


def preset_text(name: str) -> str:
    """Return the commented TOML text of preset ``name``."""
    if name not in PRESETS:
        raise _unknown(name)
    resource = resources.files("latexprep").joinpath("presets", f"{name}.toml")
    try:
        return resource.read_text(encoding="utf-8")
    except OSError as error:
        raise PreparationError(f"Cannot read preset {name!r}: {error}") from error


def preset_values(name: str) -> dict[str, object]:
    """Parse preset ``name`` into raw settings values for the configuration loader."""
    try:
        return tomllib.loads(preset_text(name))
    except tomllib.TOMLDecodeError as error:
        raise PreparationError(f"Preset {name!r} is not valid TOML: {error}") from error
