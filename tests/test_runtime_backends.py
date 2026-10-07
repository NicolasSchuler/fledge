from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.models import PreparationError
from latexprep.runtime import (
    CommandResult,
    ToolRunner,
    _BiberToolchain,
    _distribution_roots,
    _host_macho_slice,
    _linux_process_groups,
    _seal_toolchain,
    _ToolContext,
    build_project,
)
from tests.support import live_tests_enabled
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
        sys.platform == "linux" and live_tests_enabled("LATEXPREP_RUN_SANDBOX_TESTS"),
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


class ToolIsolationTests(unittest.IsolatedAsyncioTestCase):
    """A universal Biber image is split in-process; the lipo stub is never granted."""

    @staticmethod
    def universal(entries: tuple[tuple[int, bytes], ...], *, wide: bool = False) -> bytes:
        width = 32 if wide else 20
        records = payload = b""
        start = 8 + len(entries) * width
        for cpu_type, image in entries:
            offset = start + len(payload)
            records += (
                struct.pack(">IIQQII", cpu_type, 0, offset, len(image), 14, 0)
                if wide
                else struct.pack(">IIIII", cpu_type, 0, offset, len(image), 14)
            )
            payload += image
        magic = 0xCAFEBABF if wide else 0xCAFEBABE
        return struct.pack(">II", magic, len(entries)) + records + payload

    def test_host_slice_is_extracted_from_both_container_widths(self) -> None:
        arm, intel = b"arm64 slice image", b"x86_64 slice image"
        for wide in (False, True):
            image = self.universal(((0x01000007, intel), (0x0100000C, arm)), wide=wide)
            for machine, expected in (("arm64", arm), ("x86_64", intel)):
                with (
                    self.subTest(wide=wide, machine=machine),
                    patch("latexprep.runtime.platform.machine", return_value=machine),
                ):
                    self.assertEqual(_host_macho_slice(image), expected)

    def test_unusable_containers_are_refused_and_thin_images_pass_through(self) -> None:
        thin = b"\xcf\xfa\xed\xfe single architecture executable"
        self.assertEqual(_host_macho_slice(thin), thin)
        with patch("latexprep.runtime.platform.machine", return_value="arm64"):
            for description, image in (
                ("absent host slice", self.universal(((0x01000007, b"x86_64 only"),))),
                ("truncated records", struct.pack(">II", 0xCAFEBABE, 4) + b"\x01\x00\x00\x0c"),
                ("empty container", struct.pack(">II", 0xCAFEBABE, 0)),
                ("short image", b"\xca\xfe\xba\xbe"),
            ):
                with self.subTest(description=description), self.assertRaises(PreparationError):
                    _host_macho_slice(image)
            escaping = bytearray(self.universal(((0x0100000C, b"arm64 slice image"),)))
            escaping[16:20] = (len(escaping) - 4).to_bytes(4, "big")
            with self.assertRaisesRegex(PreparationError, "outside its container"):
                _host_macho_slice(bytes(escaping))
        with (
            patch("latexprep.runtime.platform.machine", return_value="riscv64"),
            self.assertRaisesRegex(PreparationError, "Unsupported host architecture"),
        ):
            _host_macho_slice(self.universal(((0x0100000C, b"arm64 slice image"),)))

    def test_sealing_leaves_no_writable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve() / "biber-0"
            (base / "payload/inner").mkdir(parents=True)
            (base / "biber").write_bytes(b"thin image")
            (base / "biber").chmod(0o700)
            (base / "payload/inner/module.pm").write_bytes(b"payload module")
            _seal_toolchain(base)
            for path in (base, *base.rglob("*")):
                with self.subTest(path=path.name):
                    self.assertFalse(path.stat().st_mode & 0o222, path)
            self.assertTrue((base / "biber").stat().st_mode & 0o100)
            (base / "payload").chmod(0o700)
            (base / "payload/link").symlink_to(base / "biber")
            with self.assertRaisesRegex(PreparationError, "unsupported entry"):
                _seal_toolchain(base)

    @unittest.skipUnless(sys.platform == "darwin", "Seatbelt profiles are macOS-only")
    async def test_only_commands_that_may_run_biber_receive_payload_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            base, workspace = root / ".toolchain/biber-0", root / "work"
            base.mkdir(parents=True)
            workspace.mkdir()
            executable, payload = base / "biber", base / "payload"
            executable.write_bytes(b"thin biber image")
            executable.chmod(0o500)
            payload.mkdir()
            (payload / "libperl.dylib").write_bytes(b"extracted payload")
            runner = ToolRunner()
            info = payload.stat()
            runner._biber_toolchain = _BiberToolchain(
                executable,
                payload,
                ToolRunner._file_identity(executable),
                (info.st_dev, info.st_ino),
            )
            profile = runner._sandbox_command(
                ["/bin/sh", "-e", f"$biber='{executable} --noconf %O %B';"], workspace
            )[2]
            for rule in ("process-exec", "file-read*", "file-map-executable"):
                self.assertIn(
                    f"(allow {rule} (literal {json.dumps(str(executable))}) "
                    f"(subpath {json.dumps(str(payload))}))",
                    profile,
                )
            blocked = runner._sandbox_command(
                ["/bin/sh", "-e", "$biber='internal latexprep_backend_blocked';"], workspace
            )[2]
            self.assertNotIn(str(payload), blocked)
            self.assertNotIn(str(executable), blocked)
            self.assertNotIn("(allow process-exec (literal", blocked.rsplit("\n", 1)[-1])
            shutil.rmtree(payload)
            payload.mkdir()
            with self.assertRaisesRegex(PreparationError, "payload was replaced"):
                runner._sandbox_command(["/bin/sh", "-e", str(executable)], workspace)


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

    async def test_bibtex_strategy_never_prepares_or_names_the_biber_payload(self) -> None:
        for backend, preparations in (("bibtex", 0), ("auto", 1), ("biber", 1)):
            with self.subTest(backend=backend):
                _, runner = await self.build(backend, {"main.aux": "\\relax\n"})
                self.assertEqual(runner.biber_preparations, preparations)
                build = next(command for command in runner.commands if command[-1].endswith(".tex"))
                rules = build[build.index("-e") + 1]
                self.assertEqual("$biber='biber --noconf %O %B'" in rules, backend != "bibtex")
                if backend == "bibtex":
                    self.assertIn("$biber='internal latexprep_backend_blocked'", rules)
                    self.assertNotIn("--noconf", rules)

    async def test_unpreparable_biber_is_reported_instead_of_raising(self) -> None:
        self.build_count += 1
        runner = ControlBuildRunner({"main.bcf": _BCF})
        with patch.object(
            runner,
            "prepare_biber",
            AsyncMock(side_effect=PreparationError("no arm64 slice")),
        ):
            result = await build_project(
                self.source,
                "main.tex",
                self.root / f"build-{self.build_count}",
                "pdflatex",
                runner,
                bibliography_backend="biber",
            )
        tool = next(item for item in result.findings if item.rule == "build.bibliography_tool")
        self.assertEqual((tool.code, tool.status), ("BLD202", "inconclusive"))
        self.assertIn("no arm64 slice", tool.message)
        self.assertFalse(result.success)

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
