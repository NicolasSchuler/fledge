"""Finding and reading the settings file of a job.

Owns the search for one settings file beside the original input, and reading a TOML or
``pyproject.toml`` file into raw values. It does not interpret the values; ``config``
turns them into validated ``Settings``.
"""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path

from .errors import PreparationError, RequestError

# Discovered per directory in this order; the latex-prep names remain supported.
CONFIG_FILENAMES = (".fledge.toml", "fledge.toml", ".latex-prep.toml", "latex-prep.toml")
PYPROJECT_TABLES = ("fledge", "latex-prep")
_PYPROJECT = "pyproject.toml"
# A directory holding one of these is the root of a repository, where the search ends.
_REPOSITORY_MARKERS = (".git", ".hg")


def load_toml(path: Path) -> dict[str, object]:
    """Parse the TOML file ``path``; a missing, unreadable or invalid file is an error."""
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


def read_settings_file(path: Path) -> dict[str, object]:
    """The raw settings in ``path``; for ``pyproject.toml`` only its Fledge table."""
    values = load_toml(path)
    if path.name != _PYPROJECT:
        return values
    settings = _pyproject_settings(path, values)
    if settings is None:
        raise PreparationError(f"Settings {path} has no [tool.fledge] or [tool.latex-prep] table")
    return settings


def _search_boundaries() -> frozenset[Path]:
    """Directories whose own configuration is never a project's: home and temporary space.

    A file in a shared directory such as ``/tmp`` could have been put there by anyone.
    """
    return frozenset(
        Path(directory).resolve() for directory in (Path.home(), tempfile.gettempdir(), "/tmp")
    )


def _is_search_boundary(directory: Path, boundaries: frozenset[Path]) -> bool:
    """Whether the search must not look in or above ``directory``."""
    return directory in boundaries or directory == directory.parent


def _is_repository_root(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in _REPOSITORY_MARKERS)


def _settings_file_in(directory: Path) -> Path | None:
    """The settings file in ``directory`` that wins, or None; an unrelated pyproject is skipped."""
    for name in CONFIG_FILENAMES:
        if (candidate := directory / name).is_file():
            return candidate
    candidate = directory / _PYPROJECT
    if candidate.is_file() and _pyproject_settings(candidate, load_toml(candidate)) is not None:
        return candidate
    return None


def resolve_config_path(
    source: Path, explicit: Path | None = None, *, isolated: bool = False
) -> Path | None:
    """Choose one config beside the original input; never inspect extracted ZIPs.

    Stop after a repository root, before the home, temporary or filesystem-root
    ancestor, or after the starting directory when the input itself is at such a
    boundary.
    """
    if explicit is not None:
        if isolated:
            raise PreparationError("--isolated cannot be combined with --config")
        return explicit.expanduser().resolve()
    if isolated:
        return None
    source = source.expanduser().resolve()
    start = source if source.is_dir() else source.parent
    boundaries = _search_boundaries()
    for directory in (start, *start.parents):
        at_boundary = _is_search_boundary(directory, boundaries)
        if at_boundary and directory != start:
            break
        if (found := _settings_file_in(directory)) is not None:
            return found
        if at_boundary or _is_repository_root(directory):
            break
    return None
