from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.models import PreparationError
from latexprep.runtime import (
    CommandResult,
    ToolRunner,
    _distribution_roots,
    _linux_process_groups,
    _ToolContext,
    build_project,
)
from tests.test_runtime import FakeBuildRunner

_BCF = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<bcf:controlfile xmlns:bcf="https://sourceforge.net/projects/biblatex" version="3.11"/>'
)


class HomebrewRuntimeTests(unittest.TestCase):
    def test_declared_upgraded_nss_alias_is_granted_without_broad_homebrew_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory).resolve()
            cellar = prefix / "Cellar"
            poppler = cellar / "poppler" / "26.10.0"
            nss = cellar / "nss" / "3.131"
            nspr = cellar / "nspr" / "4.40"
            for package in (poppler, nss, nspr):
                package.mkdir(parents=True)
            (prefix / "opt").mkdir()
            for package in (poppler, nss, nspr):
                (prefix / "opt" / package.parent.name).symlink_to(package, target_is_directory=True)
            (poppler / "INSTALL_RECEIPT.json").write_text(
                json.dumps({"runtime_dependencies": [{"full_name": "nss", "pkg_version": "3.130"}]})
            )
            (nss / "INSTALL_RECEIPT.json").write_text(
                json.dumps({"runtime_dependencies": [{"full_name": "nspr", "version": "4.40"}]})
            )
            binary = poppler / "bin" / "pdfinfo"
            binary.parent.mkdir()
            binary.write_bytes(b"installed tool fixture")
            roots = _distribution_roots(binary)
            self.assertIn(nss, roots)
            self.assertIn(prefix / "opt/nss", roots)
            self.assertIn(nspr, roots)
            for broad in (prefix, cellar, prefix / "opt", cellar / "nss"):
                self.assertNotIn(broad, roots)
            outside = prefix / "private"
            outside.mkdir()
            (prefix / "opt/nss").unlink()
            (prefix / "opt/nss").symlink_to(outside, target_is_directory=True)
            roots = _distribution_roots(binary)
            self.assertNotIn(outside, roots)
            self.assertNotIn(prefix / "opt/nss", roots)


class LinuxSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.context = _ToolContext(
            Path("/usr/bin/perl"),
            frozenset({Path("/usr/bin/perl"), Path("/bin/sh")}),
            frozenset({Path("/usr/lib")}),
            ("/usr/bin", "/bin"),
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def sandbox_command(
        self, runner: ToolRunner, inputs: tuple[Path, ...] = (), context: _ToolContext | None = None
    ) -> list[str]:
        with (
            patch("latexprep.runtime.sys.platform", "linux"),
            patch("latexprep.runtime.shutil.which", return_value="/usr/bin/bwrap"),
            patch.object(runner, "_tool_context", return_value=context or self.context),
        ):
            return runner._sandbox_command(["/usr/bin/perl", "-v"], self.workspace, inputs)

    def test_linux_mounts_only_leaf_writes_and_exact_frozen_inputs(self) -> None:
        runner = ToolRunner()
        source = self.root / "input.pdf"
        source.write_bytes(b"%PDF-exact-input")
        frozen = runner.freeze_input(source, self.root / "frozen")
        command = self.sandbox_command(runner, (frozen,))
        self.assertEqual(command[0], "/usr/bin/bwrap")
        for option in (
            "--unshare-user",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
        ):
            self.assertIn(option, command)
        writes = [
            command[index + 1 : index + 3] for index, arg in enumerate(command) if arg == "--bind"
        ]
        reads = [
            command[index + 1 : index + 3]
            for index, arg in enumerate(command)
            if arg == "--ro-bind"
        ]
        self.assertEqual(writes, [[str(self.workspace), str(self.workspace)]])
        self.assertIn([str(frozen), str(frozen)], reads)
        for broad in (str(frozen.parent), str(self.root), "/", "/home", "/etc", "/usr/bin"):
            self.assertNotIn([broad, broad], reads)
        self.assertNotIn("--share-net", command)
        self.assertNotIn("--unshare-user-try", command)
        self.assertEqual(command[-4:], ["/", "--", "/usr/bin/perl", "-v"])
        with self.assertRaisesRegex(PreparationError, "exact runtime-frozen"):
            self.sandbox_command(runner, (source,))
        frozen.chmod(0o600)
        frozen.write_bytes(b"changed")
        with self.assertRaisesRegex(PreparationError, "modified or replaced"):
            self.sandbox_command(runner, (frozen,))

    def test_linux_rejects_private_executables_and_broad_resources(self) -> None:
        runner = ToolRunner()
        private = self.root / "private-program"
        private.write_text("private")
        private_context = _ToolContext(private, frozenset({private}), frozenset(), ())
        with self.assertRaisesRegex(PreparationError, "private-home and project"):
            self.sandbox_command(runner, context=private_context)
        broad = _ToolContext(
            self.context.executable, self.context.executables, frozenset({Path("/")}), ()
        )
        with self.assertRaisesRegex(PreparationError, "declared system paths"):
            self.sandbox_command(runner, context=broad)
        with (
            patch("latexprep.runtime.sys.platform", "linux"),
            patch("latexprep.runtime.shutil.which", return_value="/usr/bin/bwrap"),
            patch.object(runner, "_tool_context", return_value=self.context),
            self.assertRaisesRegex(PreparationError, "overlap"),
        ):
            runner._sandbox_command(["/usr/bin/perl"], Path("/usr/lib/job"))

    async def test_missing_linux_backend_never_launches_command(self) -> None:
        runner = ToolRunner()
        execute = AsyncMock()
        with (
            patch("latexprep.runtime.sys.platform", "linux"),
            patch("latexprep.runtime.shutil.which", return_value=None),
            patch.object(runner, "_resolve_tool", return_value=Path("/usr/bin/perl")),
            patch.object(runner, "_tool_context", return_value=self.context),
            patch.object(runner, "_execute", execute),
            self.assertRaisesRegex(PreparationError, "No unrestricted fallback"),
        ):
            await runner.run(["perl", "-e", 'print "must not run"'], self.workspace, self.workspace)
        execute.assert_not_awaited()

    def test_linux_descendants_keep_memory_accounting_after_new_session(self) -> None:
        rows = _linux_process_groups(
            b"90 1 90 100\n100 90 100 10\n101 100 101 600\n"
            b"102 101 101 500\n103 102 777 100\n200 90 200 40\n",
            {100},
        )
        self.assertEqual(
            [group for pid, group, _ in rows if pid in {100, 101, 102, 103}], [100] * 4
        )
        memory, failure = ToolRunner._group_usage(rows, 100, 1)
        self.assertEqual(memory, 1210)
        self.assertIn("exceeded 1 MiB", failure or "")
        for malformed in (b"", b"1 2 3\n", b"1 invalid 1 1\n", b"1 0 1 -2\n"):
            with self.subTest(malformed=malformed), self.assertRaises(PreparationError):
                _linux_process_groups(malformed, {1})

    @unittest.skipUnless(
        sys.platform == "linux" and os.environ.get("LATEXPREP_RUN_SANDBOX_TESTS") == "1",
        "opt-in live Linux isolation check; requires operational Bubblewrap user namespaces",
    )
    async def test_live_linux_denies_sibling_reads_and_writes(self) -> None:
        perl = shutil.which("perl")
        self.assertIsNotNone(perl)
        outside = self.root / "private.txt"
        outside.write_text("private")
        runner = ToolRunner()
        program = (
            'my $f; print(open($f,"<",$ARGV[0]) ? "READ" : "DENIED"); '
            'print(open($f,">",$ARGV[1]) ? " WRITE" : " BLOCKED");'
        )
        result = await runner.run(
            [str(perl), "-e", program, str(outside), str(self.root / "outside-write")],
            self.workspace,
            self.workspace,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DENIED BLOCKED")
        self.assertFalse((self.root / "outside-write").exists())


class ControlBuildRunner(FakeBuildRunner):
    def __init__(self, controls: dict[str, str], unavailable: str | None = None) -> None:
        super().__init__()
        self.controls = controls
        self.unavailable = unavailable

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        if argv[0] == self.unavailable:
            raise PreparationError(f"Required tool is unavailable: {self.unavailable}")
        result = await super().run(argv, cwd, workspace)
        if argv[-1].endswith(".tex"):
            for name, text in self.controls.items():
                path = workspace / "output" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        return result


class BibliographyBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text("source")
        self.build_count = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def build(self, backend: str, controls: dict[str, str], unavailable: str | None = None):
        self.build_count += 1
        runner = ControlBuildRunner(controls, unavailable)
        result = await build_project(
            self.source,
            "main.tex",
            self.root / f"build-{self.build_count}",
            "pdflatex",
            runner,
            bibliography_backend=backend,
        )
        return result, runner

    async def test_requested_backend_matches_fresh_controls(self) -> None:
        for backend, controls in (
            ("bibtex", {"main.aux": "\\relax\n\\bibdata{references}\n"}),
            ("biber", {"main.bcf": _BCF, "main.aux": "\\relax\n"}),
            ("auto", {"main.bcf": _BCF, "sub/other.aux": "\\bibdata{references}\n"}),
        ):
            with self.subTest(backend=backend):
                result, runner = await self.build(backend, controls)
                self.assertTrue(result.success, result.findings)
                finding = next(
                    item for item in result.findings if item.rule == "build.bibliography_backend"
                )
                self.assertEqual(finding.code, "BLD201")
                self.assertEqual(finding.status, "passed")
                self.assertEqual(finding.details["requested_backend"], backend)
                build = next(command for command in runner.commands if command[-1].endswith(".tex"))
                rules = build[build.index("-e") + 1]
                if backend != "auto":
                    other = "biber" if backend == "bibtex" else "bibtex"
                    self.assertIn(f"${other}='internal latexprep_backend_blocked'", rules)
                    self.assertIn("return 1", rules)
                else:
                    self.assertNotIn("latexprep_backend_blocked", rules)
        result, _ = await self.build("bibtex", {"main.aux": "\\relax\n"})
        finding = next(
            item for item in result.findings if item.rule == "build.bibliography_backend"
        )
        self.assertTrue(result.success)
        self.assertEqual(finding.status, "not_applicable")

    async def test_mismatching_and_malformed_controls_block(self) -> None:
        for backend, controls, status in (
            ("bibtex", {"main.bcf": _BCF}, "failed"),
            ("biber", {"main.aux": "\\bibdata{refs}"}, "failed"),
            ("auto", {"main.bcf": "broken XML"}, "inconclusive"),
            ("biber", {"main.bcf": "<!DOCTYPE doc><controlfile/>"}, "inconclusive"),
            ("bibtex", {"main.aux": "\\bibdata{}"}, "inconclusive"),
        ):
            with self.subTest(backend=backend, controls=controls):
                result, _ = await self.build(backend, controls)
                self.assertFalse(result.success)
                self.assertIsNone(result.pdf)
                finding = next(
                    item for item in result.findings if item.rule == "build.bibliography_backend"
                )
                self.assertEqual(finding.code, "BLD201")
                self.assertEqual(finding.status, status)
        with self.assertRaisesRegex(PreparationError, "Bibliography backend"):
            await self.build("bibtex;unexpected-command", {})

    async def test_required_backend_version_is_reported_and_unavailable_is_inconclusive(
        self,
    ) -> None:
        result, runner = await self.build("biber", {"main.bcf": _BCF})
        tool = next(item for item in result.findings if item.rule == "build.bibliography_tool")
        self.assertEqual(tool.code, "BLD202")
        self.assertEqual(tool.status, "passed")
        self.assertIn("biber", result.tools)
        self.assertIn(["biber", "--noconf", "--version"], runner.commands)
        missing, _ = await self.build("biber", {"main.bcf": _BCF}, unavailable="biber")
        self.assertFalse(missing.success)
        tool = next(item for item in missing.findings if item.rule == "build.bibliography_tool")
        self.assertEqual(tool.code, "BLD202")
        self.assertEqual(tool.status, "inconclusive")


if __name__ == "__main__":
    unittest.main()
