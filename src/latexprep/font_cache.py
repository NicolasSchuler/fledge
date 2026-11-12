r"""A sealed per-job luaotfload font database that LuaLaTeX builds read instead of rebuilding it.

Every build starts with an empty ``TEXMFVAR``, so luaotfload would scan every font of the TeX
distribution and of the system on each LuaLaTeX build, which takes seconds. Once per job, a
sandboxed warm-up writes the font names database into a runtime-owned folder, which is then
made read-only. Each LuaLaTeX build gets a copy of the database in its own writable cache,
because luaotfload reads the database only from there, and reads the sealed folder's font
caches through ``TEXMFCACHE``. Running the warm-up is ``runtime``'s work.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PreparationError

# A name lookup makes luaotfload build its names database; the font itself need not exist.
WARM_UP_DOCUMENT = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\font\\probe={name:Latin Modern Roman} at 10pt \\probe Fledge\n\\end{document}\n"
)
WARM_UP_MAIN = "font-cache-warm-up.tex"
# Where luaotfload keeps its names database below a TEXMFVAR.
_NAMES_DIRECTORY = Path("luatex-cache", "generic", "names")
_NAMES_DATABASE_PREFIX = "luaotfload-names."
# A runtime path reaches TEXMFCACHE, whose entries ":" separates.
_CACHE_PATH_SEPARATOR = ":"


@dataclass
class FontCachePreparation:
    """The font cache of one runner: prepared once, or failed once.

    Attributes:
        lock: Serializes the preparation, so concurrent builds wait for one warm-up.
        directory: The sealed ``TEXMFVAR`` tree that holds the cache, once prepared.
        failure: Why the preparation failed, once it did; builds then run without it.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    directory: Path | None = None
    failure: str | None = None


def has_names_database(texmf_var: Path) -> bool:
    """Return whether ``texmf_var`` holds a luaotfload names database."""
    names = texmf_var / _NAMES_DIRECTORY
    return names.is_dir() and any(
        entry.name.startswith(_NAMES_DATABASE_PREFIX) for entry in names.iterdir()
    )


def seal_font_cache(texmf_var: Path) -> Path:
    """Make the warmed cache read-only and return it.

    Args:
        texmf_var: The ``TEXMFVAR`` tree the warm-up wrote.

    Returns:
        The same tree, which no tool may now change.

    Raises:
        PreparationError: The tree holds no names database, a link, or a path that
            ``TEXMFCACHE`` cannot carry.
    """
    if not has_names_database(texmf_var):
        raise PreparationError("The font cache warm-up wrote no luaotfload names database")
    if _CACHE_PATH_SEPARATOR in str(texmf_var):
        raise PreparationError("The font cache path cannot be listed in TEXMFCACHE")
    for folder, directories, files in os.walk(texmf_var, topdown=False):
        for name in files:
            path = Path(folder, name)
            if path.is_symlink():
                raise PreparationError("The font cache warm-up left a link")
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for name in directories:
            Path(folder, name).chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    texmf_var.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    return texmf_var


def copy_names_database(sealed: Path, texmf_var: Path) -> None:
    """Give a build's writable cache its own copy of the sealed names database.

    luaotfload reads its names database only from the writable cache, so each build needs
    a copy; the font caches themselves are read from the sealed tree. A cache that already
    has a database keeps it. The copy runs outside the sandbox and follows links, so the
    runner makes it only before any tool has run with ``texmf_var``.

    Args:
        sealed: The sealed ``TEXMFVAR`` tree.
        texmf_var: The build's writable ``TEXMFVAR`` tree.
    """
    target = texmf_var / _NAMES_DIRECTORY
    if target.is_dir():
        return
    target.mkdir(parents=True)
    for entry in (sealed / _NAMES_DIRECTORY).iterdir():
        if entry.is_file() and not entry.is_symlink():
            copied = target / entry.name
            shutil.copyfile(entry, copied)
            copied.chmod(stat.S_IRUSR | stat.S_IWUSR)
