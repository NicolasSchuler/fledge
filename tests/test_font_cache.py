"""The sealed luaotfload font cache, kept TeX caches and remembered Seatbelt profiles.

A job warms one font names database, seals it and gives every LuaLaTeX build a copy in its
kept writable cache; a backend without the capability or a failed warm-up leaves builds as
before. The Seatbelt backend generates each distinct profile once and grants read-only trees
as readable subpaths.
"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError
from latexprep.font_cache import copy_names_database, has_names_database, seal_font_cache
from latexprep.runtime import ToolRunner
from latexprep.sandbox import SeatbeltSandbox
from latexprep.tool_launch import create_tool_environment, kept_tex_cache_directory
from latexprep.toolchain import ToolContext
from tests.fakes import FixtureToolchain, UnconfinedSandbox

_NAMES = Path("luatex-cache/generic/names")
_CONTEXT = ToolContext(
    Path("/usr/bin/perl"), frozenset({Path("/usr/bin/perl")}), frozenset(), ("/usr/bin",)
)


def _write_names_database(texmf_var: Path) -> None:
    """Write a stand-in luaotfload names database below ``texmf_var``."""
    names = texmf_var / _NAMES
    names.mkdir(parents=True)
    (names / "luaotfload-names.luc.gz").write_bytes(b"names")
    (names / "luaotfload-lookup-cache.luc").write_bytes(b"lookups")


class FontCacheFileTests(unittest.TestCase):
    """Sealing a warmed cache and copying its names database into a build's cache."""

    def setUp(self) -> None:
        """Create a temporary directory."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_sealing_requires_a_database_and_leaves_nothing_writable(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(PreparationError, "no luaotfload names database"):
            seal_font_cache(empty)
        warmed = self.root / "warmed"
        _write_names_database(warmed)
        self.assertEqual(seal_font_cache(warmed), warmed)
        for path in [warmed, *warmed.rglob("*")]:
            self.assertFalse(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def test_a_path_that_texmfcache_cannot_carry_is_refused(self) -> None:
        warmed = self.root / "a:b"
        _write_names_database(warmed)
        with self.assertRaisesRegex(PreparationError, "TEXMFCACHE"):
            seal_font_cache(warmed)

    def test_each_build_gets_its_own_writable_copy_once(self) -> None:
        sealed = self.root / "sealed"
        _write_names_database(sealed)
        build = self.root / "build-var"
        copy_names_database(sealed, build)
        self.assertTrue(has_names_database(build))
        copied = build / _NAMES / "luaotfload-names.luc.gz"
        self.assertTrue(copied.stat().st_mode & stat.S_IWUSR)
        copied.write_bytes(b"updated by the build")
        copy_names_database(sealed, build)
        self.assertEqual(copied.read_bytes(), b"updated by the build")


class ToolEnvironmentCacheTests(unittest.TestCase):
    """The environment of a command whose workspace keeps its TeX cache."""

    def test_kept_cache_and_sealed_cache_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            kept = kept_tex_cache_directory(workspace)
            sealed = workspace.parent / "sealed"
            tool = create_tool_environment(
                workspace, _CONTEXT, source_date_epoch="0", texmf_var=kept, font_cache=sealed
            )
            variables = tool.variables
            self.assertTrue(kept.is_dir())
            self.assertEqual(variables["TEXMFVAR"], str(kept))
            self.assertEqual(variables["TEXMFCACHE"], f"{sealed}:{kept}")
            self.assertTrue(tool.state_directory.is_relative_to(workspace / ".tool-state"))
            plain = create_tool_environment(workspace, _CONTEXT, source_date_epoch="0")
            self.assertNotIn("TEXMFCACHE", plain.variables)
            self.assertEqual(plain.variables["TEXMFVAR"], str(plain.state_directory / "texmf-var"))


class _CapableSandbox(UnconfinedSandbox):
    """An unconfined backend that offers the sealed font cache.

    Attributes:
        supports_font_cache: ``True``: the runner may prepare the cache.
    """

    supports_font_cache = True


class _WarmingRunner(ToolRunner):
    """A runner whose warm-up writes a names database instead of running LuaLaTeX.

    Attributes:
        warm_ups: How often the warm-up ran.
        writes_database: Whether the warm-up writes the database.
    """

    def __init__(self, *, writes_database: bool = True) -> None:
        super().__init__(sandbox=_CapableSandbox(), toolchain=FixtureToolchain(Path("/tools")))
        self.warm_ups = 0
        self.writes_database = writes_database

    async def run(
        self, argv: list[str], cwd: Path, workspace: Path, **options: object
    ) -> CommandResult:
        """Write the warm-up's names database into the workspace's kept cache.

        The arguments are those of :meth:`ToolRunner.run`; nothing is executed.
        """
        self.warm_ups += 1
        if self.writes_database:
            _write_names_database(kept_tex_cache_directory(workspace.resolve()))
        return CommandResult(0, "", "", resolved_command=argv)


class FontCachePreparationTests(unittest.IsolatedAsyncioTestCase):
    """Preparing the cache once per runner, and what a build's command then receives."""

    async def asyncSetUp(self) -> None:
        """Create a temporary job directory."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    async def test_cache_is_warmed_once_and_copied_into_kept_build_caches(self) -> None:
        runner = _WarmingRunner()
        sealed = await runner.prepare_font_cache(self.root)
        self.assertIsNotNone(sealed)
        self.assertEqual(await runner.prepare_font_cache(self.root), sealed)
        self.assertEqual(runner.warm_ups, 1)
        self.assertEqual(runner.font_cache, sealed)
        build = self.root / "build"
        build.mkdir()
        kept = runner.keep_tex_cache(build)
        environment = runner._tool_environment(build, _CONTEXT)
        self.assertEqual(environment.variables["TEXMFCACHE"], f"{sealed}:{kept}")
        self.assertTrue(has_names_database(kept))
        other = self.root / "other"
        other.mkdir()
        self.assertNotIn("TEXMFCACHE", runner._tool_environment(other, _CONTEXT).variables)

    async def test_failed_warm_up_or_unsupported_backend_builds_without_cache(self) -> None:
        failing = _WarmingRunner(writes_database=False)
        self.assertIsNone(await failing.prepare_font_cache(self.root))
        self.assertIsNone(await failing.prepare_font_cache(self.root))
        self.assertEqual(failing.warm_ups, 1)
        plain = ToolRunner(sandbox=UnconfinedSandbox(), toolchain=FixtureToolchain(Path("/t")))
        self.assertIsNone(await plain.prepare_font_cache(self.root))


class SeatbeltProfileTests(unittest.TestCase):
    """Remembered profiles and read-only trees in the generated Seatbelt profile."""

    def test_profiles_are_remembered_per_grant_and_trees_are_readable(self) -> None:
        sandbox = SeatbeltSandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, tree = root / "work", root / "toolchain" / "font-cache"
            workspace.mkdir()
            tree.mkdir(parents=True)
            with patch("latexprep.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec"):
                first = sandbox.wrap(
                    ["/usr/bin/perl"],
                    workspace,
                    context=_CONTEXT,
                    readonly_inputs=(),
                    biber=None,
                    readonly_trees=(tree,),
                )
                again = sandbox.wrap(
                    ["/usr/bin/perl", "-v"],
                    workspace,
                    context=_CONTEXT,
                    readonly_inputs=(),
                    biber=None,
                    readonly_trees=(tree,),
                )
                without = sandbox.wrap(
                    ["/usr/bin/perl"], workspace, context=_CONTEXT, readonly_inputs=(), biber=None
                )
        self.assertIs(first[2], again[2])
        self.assertIn(f"(subpath {json.dumps(str(tree))})", first[2])
        self.assertIn(f"(literal {json.dumps(str(tree.parent))})", first[2])
        self.assertNotIn(str(tree), without[2])
        self.assertNotIn(f"(allow file-write* (subpath {json.dumps(str(tree))}))", first[2])
