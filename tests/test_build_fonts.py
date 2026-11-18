"""Fonts that a XeLaTeX or LuaLaTeX build loaded from outside the project and the TeX trees.

XeTeX's XDV postamble and luaotfload's font caches name the font files; a file outside the
project and the TeX distribution is reported, because the archive would not rebuild on a
computer without it, and the project's and the distribution's fonts pass.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.build_fonts import (
    host_font_files,
    host_fonts_finding,
    luaotfload_font_files,
    tex_distribution_trees,
    xdv_font_files,
)
from latexprep.errors import PreparationError

_POST, _POST_POST, _NATIVE, _FNT_DEF1, _NOP, _TRAILER = 248, 249, 252, 243, 138, 223


def _native_definition(font_number: int, path: str, flags: int = 0) -> bytes:
    """Encode an XDV native font definition for ``path`` with ``flags`` and no options."""
    name = path.encode()
    return (
        bytes([_NATIVE])
        + struct.pack(">IIH", font_number, 655360, flags)
        + bytes([len(name)])
        + name
        + struct.pack(">I", 0)
        + (struct.pack(">I", 0xFF0000FF) if flags & 0x0200 else b"")
    )


def _xdv(definitions: bytes) -> bytes:
    """Return a minimal XDV file: one page placeholder, a postamble and its trailer."""
    body = b"\xf7\x07" + b"\x00" * 64  # preamble opcode and padding standing in for pages
    post = len(body)
    postamble = bytes([_POST]) + b"\x00" * 28 + definitions + bytes([_POST_POST])
    postamble += struct.pack(">I", post) + bytes([7]) + bytes([_TRAILER]) * 6
    return body + postamble


class XdvFontTests(unittest.TestCase):
    """The native font files of an XDV postamble."""

    def setUp(self) -> None:
        """Create a temporary directory for XDV files."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, data: bytes) -> Path:
        """Write ``data`` as ``main.xdv`` and return its path."""
        path = self.root / "main.xdv"
        path.write_bytes(data)
        return path

    def test_native_definitions_are_listed_once_in_order(self) -> None:
        tfm = bytes([_FNT_DEF1, 1]) + b"\x00" * 12 + bytes([0, 5]) + b"cmr10"
        definitions = (
            _native_definition(1, "/System/Library/Fonts/Supplemental/Times New Roman.ttf")
            + bytes([_NOP])
            + tfm
            + _native_definition(2, "fonts/Local.otf", flags=0x0200)
            + _native_definition(3, "/System/Library/Fonts/Supplemental/Times New Roman.ttf")
        )
        self.assertEqual(
            xdv_font_files(self.write(_xdv(definitions))),
            ["/System/Library/Fonts/Supplemental/Times New Roman.ttf", "fonts/Local.otf"],
        )

    def test_damaged_files_are_refused(self) -> None:
        for data in (b"", b"not an xdv file", _xdv(bytes([99])), _xdv(b"")[:-12]):
            with self.subTest(data=data[:20]):
                with self.assertRaises(PreparationError):
                    xdv_font_files(self.write(data))
        with self.assertRaises(PreparationError):
            xdv_font_files(self.root / "missing.xdv")


class HostFontTests(unittest.TestCase):
    """Font files outside the project and the TeX distribution are reported."""

    def test_fonts_outside_the_trees_are_reported(self) -> None:
        distribution = tex_distribution_trees(
            (Path("/usr/local/texlive/2026"), Path("/System/Library"), Path("/Library/Fonts"))
        )
        self.assertEqual(
            distribution,
            (
                Path("/usr/local/texlive/2026"),
                Path("/usr/share/texlive"),
                Path("/usr/share/texmf"),
                Path("/var/lib/texmf"),
            ),
        )
        hosted = host_font_files(
            [
                "/System/Library/Fonts/Supplemental/Georgia.ttf",
                "/usr/local/texlive/2026/texmf-dist/fonts/opentype/public/lm/lmroman10-regular.otf",
                "/work/project/fonts/Local.otf",
                "fonts/Relative.otf",
                "/Users/someone/Library/Fonts/FiraCode-Regular.ttf",
            ],
            distribution=distribution,
            project=Path("/work/project"),
        )
        self.assertEqual(
            hosted,
            [
                "/System/Library/Fonts/Supplemental/Georgia.ttf",
                "/Users/someone/Library/Fonts/FiraCode-Regular.ttf",
            ],
        )
        finding = host_fonts_finding("XeLaTeX", hosted, "main.tex")
        self.assertEqual((finding.code, finding.severity), ("BLD108", "warning"))
        self.assertIn("Georgia.ttf", finding.message)
        self.assertIn("not on a computer that lacks these fonts", finding.message)
        self.assertEqual(finding.details["fonts"], hosted)

    def test_project_and_distribution_fonts_pass(self) -> None:
        packaged = tex_distribution_trees((Path("/usr/share/texmf"), Path("/usr/share/fonts")))
        self.assertEqual(
            packaged, (Path("/usr/share/texlive"), Path("/usr/share/texmf"), Path("/var/lib/texmf"))
        )
        hosted = host_font_files(
            ["/usr/share/texmf/fonts/opentype/public/lm/lmroman10-regular.otf"],
            distribution=packaged,
            project=Path("/work/project"),
        )
        self.assertEqual(hosted, [])
        finding = host_fonts_finding("LuaLaTeX", hosted, "main.tex")
        self.assertEqual(
            (finding.code, finding.severity, finding.status), ("BLD108", "info", "passed")
        )


class LinuxFontTests(unittest.TestCase):
    """Fonts that fontconfig finds on Linux are classified by their real path.

    Debian's TeX Live lives in ``/usr/share/texlive``, ``/usr/share/texmf`` and
    ``/var/lib/texmf``, and ``/usr/share/fonts`` may link into them; a font reached through
    such a link is a TeX font, while a font that only the system ships is not.
    """

    def test_debian_tree_fonts_pass_and_system_fonts_are_flagged(self) -> None:
        dejavu = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        hosted = host_font_files(
            [
                "/usr/share/texlive/texmf-dist/fonts/opentype/public/lm/lmroman10-regular.otf",
                "/usr/share/texmf/fonts/opentype/public/tex-gyre/texgyretermes-regular.otf",
                dejavu,
            ],
            distribution=tex_distribution_trees(()),
            project=Path("/work/project"),
        )
        self.assertEqual(hosted, [dejavu])

    def test_a_font_linked_into_a_tex_tree_is_a_tex_font(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            texmf = root / "usr/share/texmf"
            font = texmf / "fonts/opentype/public/lm/lmroman10-regular.otf"
            font.parent.mkdir(parents=True)
            font.write_bytes(b"font")
            fonts = root / "usr/share/fonts"
            linked_file = fonts / "opentype/lmodern/lmroman10-regular.otf"
            linked_file.parent.mkdir(parents=True)
            linked_file.symlink_to(font)
            linked_folder = fonts / "X11/lm"
            linked_folder.parent.mkdir(parents=True)
            linked_folder.symlink_to(font.parent, target_is_directory=True)
            dejavu = fonts / "truetype/dejavu/DejaVuSans.ttf"
            dejavu.parent.mkdir(parents=True)
            dejavu.write_bytes(b"font")
            hosted = host_font_files(
                [str(linked_file), str(linked_folder / font.name), str(dejavu)],
                distribution=(texmf,),
                project=root / "project",
            )
        self.assertEqual(hosted, [str(dejavu)])


class LuaotfloadFontTests(unittest.TestCase):
    """The font files that luaotfload's cache copies name."""

    def test_cache_copies_name_their_font_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "texmf-var/luatex-cache/generic/fonts/otl"
            cache.mkdir(parents=True)
            (cache / "georgia.luc").write_bytes(b"compiled")
            (cache / "georgia.lua").write_text(
                'return {\n ["resources"]={},\n ["filename"]="/System/Library/Fonts/'
                'Supplemental/Georgia.ttf",\n}\n'
            )
            unrelated = Path(directory) / "main.aux"
            files = luaotfload_font_files(
                [cache / "georgia.luc", unrelated], roots=(Path(directory),)
            )
        self.assertEqual(files, ["/System/Library/Fonts/Supplemental/Georgia.ttf"])

    def test_missing_font_evidence_is_inconclusive(self) -> None:
        root = Path("/example/work")
        cache = root / "luatex-cache/generic/fonts/otl/font.luc"
        with (
            patch("latexprep.build_fonts.read_regular_file", side_effect=FileNotFoundError),
            self.assertRaisesRegex(PreparationError, "no readable text copy"),
        ):
            luaotfload_font_files([cache], roots=(root,))

    def test_cache_outside_the_approved_roots_is_never_opened(self) -> None:
        cache = Path("/example/other/luatex-cache/generic/fonts/otl/font.luc")
        with (
            patch("latexprep.build_fonts.read_regular_file") as read,
            self.assertRaisesRegex(PreparationError, "outside the approved trees"),
        ):
            luaotfload_font_files([cache], roots=(Path("/example/work"),))
        read.assert_not_called()
