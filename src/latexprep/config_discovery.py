"""Finding and reading the settings file of a job.

Owns the search for one settings file beside the original input, and reading a TOML or
``pyproject.toml`` file into raw values. It does not interpret the values; ``config``
turns them into validated ``Settings``.
"""

from __future__ import annotations

import logging
import tempfile
import tomllib
from pathlib import Path

from .errors import PreparationError, RequestError

# Discovered per directory in this order; the latex-prep names remain supported.
CONFIG_FILENAMES = (".fledge.toml", "fledge.toml", ".latex-prep.toml", "latex-prep.toml")
_LEGACY_CONFIG_FILENAMES = frozenset({".latex-prep.toml", "latex-prep.toml"})
PYPROJECT_TABLES = ("fledge", "latex-prep")
_LEGACY_PYPROJECT_TABLE = "latex-prep"
_PYPROJECT = "pyproject.toml"
# A directory holding one of these is the root of a repository, where the search ends.
_REPOSITORY_MARKERS = (".git", ".hg")

# Log lines name the file, never the directory that holds it.
logger = logging.getLogger(__name__)


def load_toml(path: Path) -> dict[str, object]:
    """Parse the TOML file ``path``; a missing, unreadable or invalid file is an error.

    Returns:
        The parsed values.

    Raises:
        RequestError: The file does not exist.
        PreparationError: The file cannot be read or is not valid TOML.
    """
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
    """Return the [tool.fledge] or [tool.latex-prep] table, or ``None`` when neither exists.

    Args:
        path: The ``pyproject.toml`` file, named in messages.
        values: The parsed contents of ``path``.

    Returns:
        The settings table, or ``None`` when neither table exists.

    Raises:
        PreparationError: Both tables exist, or the one that exists is not a table.
    """
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
    if tables[0] == _LEGACY_PYPROJECT_TABLE:
        logger.info("settings: %s uses the legacy [tool.latex-prep] table", path.name)
    if not isinstance(settings, dict):
        raise PreparationError(f"[tool.{tables[0]}] in {path} must be a settings table")
    return settings


def read_settings_file(path: Path) -> dict[str, object]:
    """Read the raw settings in ``path``; for ``pyproject.toml`` only its Fledge table.

    Returns:
        The raw values.

    Raises:
        RequestError: The file does not exist.
        PreparationError: The file cannot be read or is not valid TOML, or a
            ``pyproject.toml`` has no Fledge table, both tables, or a table that is not a
            settings table.
    """
    values = load_toml(path)
    if path.name != _PYPROJECT:
        return values
    settings = _pyproject_settings(path, values)
    if settings is None:
        raise PreparationError(f"Settings {path} has no [tool.fledge] or [tool.latex-prep] table")
    return settings


def _search_boundaries() -> frozenset[Path]:
    """List the home and temporary directories, whose own configuration is never a project's.

    A file in a shared directory such as ``/tmp`` could have been put there by anyone.
    """
    return frozenset(
        Path(directory).resolve() for directory in (Path.home(), tempfile.gettempdir(), "/tmp")
    )


def _is_search_boundary(directory: Path, boundaries: frozenset[Path]) -> bool:
    """Return whether the search must not look in or above ``directory``.

    Args:
        directory: The resolved directory to test.
        boundaries: The directories from :func:`_search_boundaries`; the filesystem root is
            a boundary too.

    Returns:
        Whether ``directory`` is a boundary.
    """
    return directory in boundaries or directory == directory.parent


def _is_repository_root(directory: Path) -> bool:
    """Return whether ``directory`` holds a repository marker (``.git`` or ``.hg``)."""
    return any((directory / marker).exists() for marker in _REPOSITORY_MARKERS)


def _settings_file_in(directory: Path) -> Path | None:
    """Find the settings file in ``directory`` that wins.

    The names in ``CONFIG_FILENAMES`` are tried in order before ``pyproject.toml``; an
    unrelated ``pyproject.toml``, one without a Fledge table, is skipped.

    Returns:
        The winning file, or ``None`` when ``directory`` holds none.
    """
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

    The search starts in the input's directory and goes upwards. It stops after a
    repository root, before the home, temporary or filesystem-root ancestor, or after the
    starting directory when the input itself is at such a boundary.

    Args:
        source: The original input, a file or a directory.
        explicit: The file named by ``--config``; when given, it is resolved and returned
            without a search.
        isolated: Whether ``--isolated`` asks for no settings file at all.

    Returns:
        The settings file, or ``None`` when none is found or ``isolated`` is set.

    Raises:
        PreparationError: ``isolated`` is combined with ``explicit``, or a
            ``pyproject.toml`` met during the search cannot be read or holds an invalid
            Fledge table.
    """
    if explicit is not None:
        if isolated:
            raise PreparationError("--isolated cannot be combined with --config")
        logger.debug("settings: using the file named by --config (%s)", explicit.name)
        return explicit.expanduser().resolve()
    if isolated:
        logger.debug("settings: --isolated, so no settings file is discovered")
        return None
    source = source.expanduser().resolve()
    start = source if source.is_dir() else source.parent
    boundaries = _search_boundaries()
    for directory in (start, *start.parents):
        at_boundary = _is_search_boundary(directory, boundaries)
        if at_boundary and directory != start:
            break
        if (found := _settings_file_in(directory)) is not None:
            _log_discovery(found, levels_up=len(start.parts) - len(directory.parts))
            return found
        if at_boundary or _is_repository_root(directory):
            break
    logger.debug("settings: no settings file found; the defaults apply")
    return None


def _log_discovery(found: Path, *, levels_up: int) -> None:
    """Log which settings file won, and that it carries a name kept for compatibility."""
    where = f"{levels_up} level(s) above the input" if levels_up else "beside the input"
    logger.debug("settings: discovered %s %s", found.name, where)
    if found.name in _LEGACY_CONFIG_FILENAMES:
        logger.info("settings: %s is a legacy name; fledge.toml is preferred", found.name)
