r"""Fonts that a XeLaTeX or LuaLaTeX build loaded from this computer rather than the project.

``fontspec`` finds a font by name in the TeX distribution and in the system's font folders.
A font from ``/System/Library/Fonts`` or ``/usr/share/fonts`` builds here, and the archive
rebuilds here too, but the receiving service lacks it. The evidence is what the engine wrote:
XeTeX names the file of every native font in the postamble of its XDV output, and luaotfload
names it in the font cache it wrote for the build (``["filename"]="…"`` in the plain-text
copy beside each cached font). The recorder lists neither font file.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Iterable
from pathlib import Path

from .errors import PreparationError
from .file_safety import open_regular_file, read_regular_file
from .findings import Finding
from .toolchain import texlive_root

HOST_FONTS_RULE = "build.host_fonts"
# The postamble that lists the fonts sits at the end of the XDV file; a document would need
# tens of thousands of fonts to need more than this.
_MAX_XDV_TAIL_BYTES = 4 * 1024 * 1024
_MAX_FONT_CACHE_BYTES = 64 * 1024 * 1024
_MAX_LISTED_FONTS = 20
# DVI and XDV opcodes (XeTeX's xdv format, identification byte 5 to 7).
_NOP = 138
_FONT_DEFINITIONS = {243: 1, 244: 2, 245: 3, 246: 4}  # fnt_def1..4 and their k sizes
_POST = 248
_POST_POST = 249
_NATIVE_FONT_DEFINITION = 252
_TRAILER = 223
_XDV_IDENTIFICATIONS = frozenset({5, 6, 7})
_POST_PARAMETERS = 28  # p[4] num[4] den[4] mag[4] l[4] u[4] s[2] t[2]
# Flags of a native font definition that add parameters after its face index.
_FLAG_COLORED = 0x0200
_FLAG_VARIATIONS = 0x0800
_FLAG_EXTEND = 0x1000
_FLAG_SLANT = 0x2000
_FLAG_EMBOLDEN = 0x4000
_CACHE_FONT_FILE = re.compile(rb'\["filename"\]="([^"\n]{1,4096})"')
_FONT_CACHE_SUFFIXES = (".luc", ".lua")
_FONT_CACHE_FOLDER = "/luatex-cache/generic/fonts/otl/"
# Where a TeX distribution packaged for a Linux system keeps its trees.
_PACKAGED_TEX_TREES = (Path("/usr/share/texlive"), Path("/usr/share/texmf"), Path("/var/lib/texmf"))


class _Reader:
    """A bounded cursor over the bytes of an XDV postamble.

    Attributes:
        data: The bytes.
        position: The index of the next byte to read.
    """

    def __init__(self, data: bytes, position: int) -> None:
        self.data = data
        self.position = position

    def take(self, size: int) -> bytes:
        """Return the next ``size`` bytes.

        Raises:
            PreparationError: Fewer than ``size`` bytes are left.
        """
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise PreparationError("The XDV postamble is truncated")
        chunk = self.data[self.position : end]
        self.position = end
        return chunk

    def number(self, size: int) -> int:
        """Return the next big-endian unsigned number of ``size`` bytes."""
        return int.from_bytes(self.take(size), "big")


def _postamble_start(data: bytes, file_size: int) -> int:
    """Return the index of the ``post`` opcode in ``data``, the last bytes of the file.

    Args:
        data: The tail of the XDV file.
        file_size: The size of the whole file, which the postamble pointer counts in.

    Returns:
        The index of the ``post`` opcode within ``data``.

    Raises:
        PreparationError: The trailer, identification or pointer is not that of an XDV file
            whose postamble lies in ``data``.
    """
    end = len(data)
    while end > 0 and data[end - 1] == _TRAILER:
        end -= 1
    if end < 6 or data[end - 1] not in _XDV_IDENTIFICATIONS or data[end - 6] != _POST_POST:
        raise PreparationError("The file has no XDV postamble trailer")
    (pointer,) = struct.unpack(">I", data[end - 5 : end - 1])
    start = pointer - (file_size - len(data))
    if not 0 <= start < end or data[start] != _POST:
        raise PreparationError("The XDV postamble pointer is invalid")
    return start


def _native_font_file(reader: _Reader, identification: int) -> str:
    """Read one native font definition after its opcode and return its font file.

    Args:
        reader: The cursor, just after the opcode.
        identification: The XDV identification byte, which decides the optional fields.

    Returns:
        The font file as XeTeX recorded it.
    """
    reader.take(8)  # font number and size
    flags = reader.number(2)
    name = reader.take(reader.number(1)).decode("utf-8", errors="replace")
    reader.take(4)  # face index
    if flags & _FLAG_COLORED:
        reader.take(4)
    if flags & _FLAG_VARIATIONS and identification < 7:
        axes = reader.number(2)
        reader.take(8 * axes)
    for flag in (_FLAG_EXTEND, _FLAG_SLANT, _FLAG_EMBOLDEN):
        if flags & flag:
            reader.take(4)
    return name


def xdv_font_files(xdv: Path, *, root: Path | None = None) -> list[str]:
    """List the font files of the native fonts that an XDV file defines.

    Args:
        xdv: The XDV file that XeTeX wrote.
        root: The trusted ancestor for a descriptor-relative read, if given.

    Returns:
        The font files, in the order of their definitions, without repeats.

    Raises:
        PreparationError: The file cannot be read, or its postamble is not one this reader
            understands.
    """
    try:
        with open_regular_file(xdv, root=root) as (stream, size):
            stream.seek(max(0, size - _MAX_XDV_TAIL_BYTES))
            data = stream.read(_MAX_XDV_TAIL_BYTES)
    except OSError as error:
        raise PreparationError("The XDV file cannot be read") from error
    reader = _Reader(data, _postamble_start(data, size) + 1)
    identification = data[len(data.rstrip(bytes([_TRAILER]))) - 1]
    reader.take(_POST_PARAMETERS)
    files: dict[str, None] = {}
    while (opcode := reader.number(1)) != _POST_POST:
        if opcode == _NOP:
            continue
        if opcode in _FONT_DEFINITIONS:
            reader.take(_FONT_DEFINITIONS[opcode] + 12)  # k, checksum, size, design size
            reader.take(reader.number(1) + reader.number(1))  # area and name
        elif opcode == _NATIVE_FONT_DEFINITION:
            files[_native_font_file(reader, identification)] = None
        else:
            raise PreparationError(f"The XDV postamble holds an unexpected opcode {opcode}")
    return list(files)


def luaotfload_font_files(
    recorder_inputs: Iterable[Path], *, roots: tuple[Path, ...], workspace: Path | None = None
) -> list[str]:
    """List the font files whose luaotfload caches a LuaLaTeX build read.

    Each cached font has a plain-text copy (``.lua``) beside its compiled form (``.luc``);
    the copy names the font file.

    Args:
        recorder_inputs: The files the build's recorder lists as read.
        roots: The only cache trees that may supply evidence.
        workspace: The trusted ancestor of any writable cache tree, if present.

    Returns:
        The font files, without repeats.

    Raises:
        PreparationError: No approved font cache was observed, or a recorded
            cache lacks bounded, readable text evidence of its font.
    """
    files: dict[str, None] = {}
    caches = {
        path.with_suffix(".lua")
        for path in recorder_inputs
        if path.suffix in _FONT_CACHE_SUFFIXES and _FONT_CACHE_FOLDER in path.as_posix()
    }
    if not caches:
        raise PreparationError("The recorder did not identify any luaotfload font caches")
    for cache in sorted(caches):
        root = next((root for root in roots if cache.is_relative_to(root)), None)
        if root is None:
            raise PreparationError("The recorder named a font cache outside the approved trees")
        read_root = workspace if workspace is not None and root.is_relative_to(workspace) else root
        try:
            data = read_regular_file(cache, _MAX_FONT_CACHE_BYTES, root=read_root)
        except OSError as error:
            raise PreparationError("A recorded font cache has no readable text copy") from error
        match = _CACHE_FONT_FILE.search(data)
        if not match:
            raise PreparationError("A recorded font cache does not identify a font file")
        files[match[1].decode("utf-8", errors="replace")] = None
    return list(files)


def tex_distribution_trees(roots: Iterable[Path]) -> tuple[Path, ...]:
    """Return the directories of the TeX distribution: the declared one and Debian's trees.

    A TeX Live year directory (``/usr/local/texlive/2026``) among the declared roots holds a
    TUG distribution; a distribution packaged for Linux spreads over fixed system
    directories, which count whether or not they exist here. The system's font folders are
    declared roots too, but they are not part of the distribution.

    Args:
        roots: The build's declared resource roots.

    Returns:
        The directories whose fonts the TeX distribution ships.
    """
    year = texlive_root(frozenset(roots))
    return _PACKAGED_TEX_TREES if year is None else (year, *_PACKAGED_TEX_TREES)


def _spellings(path: Path) -> set[Path]:
    """Return ``path`` as written and with every link resolved (``realpath``)."""
    return {path, Path(os.path.realpath(path))}


def host_font_files(
    font_files: Iterable[str], *, distribution: Iterable[Path], project: Path
) -> list[str]:
    """Select the font files that lie outside the project and the TeX distribution.

    A font and the trees are compared as written and with links resolved, so a font in a
    system font folder that links into a TeX tree, as Debian links ``/usr/share/fonts``
    entries into ``/usr/share/texmf``, counts as a TeX font.

    Args:
        font_files: The font files a build loaded; a relative one lies in the project.
        distribution: The TeX distribution's directories.
        project: The build's copy of the project.

    Returns:
        The absolute font files outside ``project`` and every ``distribution`` directory.
    """
    trees = {spelling for tree in (project, *distribution) for spelling in _spellings(tree)}
    return [
        name
        for name in font_files
        if Path(name).is_absolute()
        and not any(
            spelling.is_relative_to(tree) for spelling in _spellings(Path(name)) for tree in trees
        )
    ]


def host_fonts_finding(engine: str, host_fonts: list[str], main: str) -> Finding:
    """Report the fonts outside the project and the TeX distribution, or that there are none.

    Args:
        engine: The engine's display name, such as XeLaTeX.
        host_fonts: The font files outside the project and the TeX distribution.
        main: The main file, the finding's path.

    Returns:
        A warning that lists the fonts, or an informational finding that passed.
    """
    if not host_fonts:
        return Finding(
            HOST_FONTS_RULE,
            f"Every font that {engine} loaded came from the project or the TeX distribution.",
            severity="info",
            status="passed",
            path=main,
        )
    listed = ", ".join(host_fonts[:_MAX_LISTED_FONTS])
    if len(host_fonts) > _MAX_LISTED_FONTS:
        listed += f" and {len(host_fonts) - _MAX_LISTED_FONTS} more"
    return Finding(
        HOST_FONTS_RULE,
        f"{engine} loaded fonts that exist only on this computer, outside the project and the "
        f"TeX distribution: {listed}. The archive rebuilds here, but not on a computer that "
        "lacks these fonts, such as the receiving service's.",
        severity="warning",
        path=main,
        suggestion=(
            "Copy the font files into the project and load them by file name, for example "
            "\\setmainfont{TimesNewRoman.ttf}[Path=fonts/], if their license allows it; or "
            "use a font that TeX Live ships, such as TeX Gyre Termes instead of Times."
        ),
        details={"fonts": host_fonts},
    )
