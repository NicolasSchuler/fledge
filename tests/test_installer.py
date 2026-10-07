"""Exercise the installer with fake external tools; never install system packages."""

from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
TEX_TOOLS = ("latexmk", "pdflatex", "bibtex")
PDF_TOOLS = (
    "pdfinfo",
    "pdftotext",
    "pdftoppm",
    "pdffonts",
    "pdfdetach",
    "pdfimages",
    "pdftohtml",
)

MOCK_PROGRAM = r"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["MOCK_LOG"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
if name == os.environ.get("MOCK_FAIL"):
    sys.exit(17)
if name.startswith("python"):
    if args[:1] == ["-c"]:
        sys.exit(1 if os.environ.get("MOCK_OLD_PYTHON") and name == "python3" else 0)
    if args[:2] == ["-m", "venv"]:
        target = Path(args[2]) / "bin"
        target.mkdir(parents=True)
        for tool in ("python", "latex-prep"):
            shutil.copy2(os.environ["MOCK_PROGRAM"], target / tool)
    elif args[:2] == ["-m", "pip"] and os.environ.get("MOCK_PIP_FAIL"):
        sys.exit(19)
elif name == "brew":
    if os.environ.get("MOCK_BREW_NO_TOOLS"):
        sys.exit(0)
    binaries = {
        "mactex-no-gui": ("latexmk", "pdflatex", "bibtex"),
        "python@3.13": ("python3.13",),
        "poppler": (
            "pdfinfo", "pdftotext", "pdftoppm", "pdffonts",
            "pdfdetach", "pdfimages", "pdftohtml",
        ),
        "tex-fmt": ("tex-fmt",),
        "qpdf": ("qpdf",),
        "mupdf-tools": ("mutool",),
    }
    for package in args:
        for tool in binaries.get(package, ()):
            shutil.copy2(os.environ["MOCK_PROGRAM"], Path(os.environ["MOCK_BIN"]) / tool)
elif name == "latex-prep":
    if args == ["--tool-locations"]:
        for tool in ("latexmk", "pdflatex", "pdfinfo"):
            subprocess.run([tool, "--selected-tool"], check=True)
    else:
        print("Usage: latex-prep [OPTIONS] COMMAND [ARGS]...")
elif args == ["--selected-tool"]:
    print(str(Path(sys.argv[0]).absolute()))
"""

# Use the actual shell logic; replace just host/tool discovery so missing-tool
# cases are deterministic even on machines with a complete TeX installation.
HARNESS = r"""
source "$1"
shift
uname() { printf '%s\n' "$MOCK_PLATFORM"; }
id() { printf '%s\n' "$MOCK_UID"; }
find_tool() {
    if [[ -x "$MOCK_BIN/$1" ]]; then
        printf '%s\n' "$MOCK_BIN/$1"
    else
        return 1
    fi
}
main "$@"
"""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="latex installer tests ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source with spaces"
        (self.source / "src/latexprep").mkdir(parents=True)
        (self.source / "src/latexprep/cli.py").touch()
        (self.source / "pyproject.toml").touch()
        self.script = self.source / "install.sh"
        shutil.copy2(INSTALLER, self.script)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.tools = self.root / "fake tools"
        self.tools.mkdir()
        self.log = self.root / "commands.jsonl"
        self.program = self.root / "mock-program"
        self.program.write_text(f"#!{sys.executable}\n" + MOCK_PROGRAM)
        self.program.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "MOCK_PROGRAM": str(self.program),
            "MOCK_LOG": str(self.log),
            "MOCK_BIN": str(self.tools),
            "MOCK_PLATFORM": "Darwin",
            "MOCK_UID": "501",
        }
        self.add_tools("sandbox-exec")

    @property
    def prefix(self):
        return self.home / ".local/share/latex-preparation"

    @property
    def launcher(self):
        return self.home / ".local/bin/latex-prep"

    def add_tools(self, *names):
        for name in names:
            shutil.copy2(self.program, self.tools / name)

    def complete_tools(self):
        self.add_tools("python3", *TEX_TOOLS, *PDF_TOOLS)

    def run_installer(self, *args):
        return subprocess.run(
            ["/bin/bash", "-c", HARNESS, "installer-test", str(self.script), *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_no_install(self):
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())
        self.assertTrue(all(command[1:2] == ["-c"] for command in self.commands()))

    def test_help_and_invalid_options(self):
        for args in (("--help",), ("--unknown",), ("--prefix",), ("--bin-dir", "--yes")):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 0 if args == ("--help",) else 1)
                self.assert_no_install()

    def test_unsupported_platforms_and_root_are_rejected(self):
        for platform in ("Linux", "MINGW64_NT-10.0"):
            self.env["MOCK_PLATFORM"] = platform
            result = self.run_installer("--yes")
            self.assertEqual(result.returncode, 1)
            self.assertIn("macOS only", result.stderr)
            self.assert_no_install()
        self.env.update(MOCK_PLATFORM="Darwin", MOCK_UID="0")
        result = self.run_installer("--yes")
        self.assertIn("without sudo", result.stderr)
        self.assert_no_install()

    def test_missing_sandbox_fails_before_installation(self):
        (self.tools / "sandbox-exec").unlink()
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sandbox-exec is missing", result.stderr)
        self.assert_no_install()

    def test_complete_toolchain_dry_run_needs_no_homebrew(self):
        self.complete_tools()
        result = self.run_installer("--dry-run", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reuse Python", result.stdout)
        self.assertIn("Dry run complete", result.stdout)
        self.assertNotIn("install --cask", result.stdout)
        self.assert_no_install()

    def test_missing_dependencies_show_large_download_and_optional_plan(self):
        self.add_tools("brew")
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("multi-GB", result.stdout)
        self.assertIn("administrator password", result.stdout)
        self.assertIn("install --cask mactex-no-gui", result.stdout)
        self.assertIn("install python@3.13 poppler", result.stdout)
        self.assertNotIn("install python@3.13 poppler tex-fmt", result.stdout)
        optional = self.run_installer("--dry-run", "--with-optional")
        self.assertIn("install python@3.13 poppler tex-fmt qpdf mupdf-tools", optional.stdout)
        self.assert_no_install()

    def test_missing_homebrew_is_a_prerequisite_not_a_bootstrap(self):
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Installation plan", result.stdout)
        self.assertIn("https://brew.sh", result.stderr)
        self.assert_no_install()

    def test_partial_tex_is_never_replaced(self):
        for tool in ("pdflatex", "tlmgr"):
            with self.subTest(tool=tool):
                self.add_tools(tool)
                result = self.run_installer("--yes")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Existing TeX installation is incomplete", result.stderr)
                self.assertIn("latexmk", result.stderr)
                self.assert_no_install()
                (self.tools / tool).unlink()

    def test_existing_targets_including_broken_symlinks_are_preserved(self):
        for path in (self.prefix, self.launcher):
            path.parent.mkdir(parents=True, exist_ok=True)
            for symlink in (False, True):
                with self.subTest(path=path, symlink=symlink):
                    if symlink:
                        path.symlink_to(self.root / "unrelated absent target")
                    else:
                        path.write_text("unrelated content")
                    result = self.run_installer("--yes")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("already exists", result.stderr)
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_text(), "unrelated content")
                    self.assertEqual(self.commands(), [])
                    path.unlink()

    def test_noninteractive_install_requires_explicit_yes(self):
        self.complete_tools()
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("use --yes", result.stderr)
        self.assert_no_install()

    def test_interactive_decline_makes_no_changes(self):
        self.complete_tools()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen(
            ["/bin/bash", "-c", HARNESS, "installer-test", str(self.script)],
            cwd=self.root,
            env=self.env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.write(master, b"n\n")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("Cancelled. No changes made.", stdout)
        self.assert_no_install()

    def test_existing_python_is_validated_and_newer_python_reused(self):
        self.complete_tools()
        self.env["MOCK_OLD_PYTHON"] = "1"
        self.add_tools("python3.12")
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Reuse Python: {self.tools}/python3.12", result.stdout)
        self.assert_no_install()

    def test_mock_install_handles_spaces_and_relative_destinations(self):
        self.complete_tools()
        result = self.run_installer("--yes", "--prefix", "private dir", "--bin-dir", "bin dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = self.root / "bin dir/latex-prep"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("Installed. Run from any directory", result.stdout)
        pip = next(command for command in self.commands() if command[1:3] == ["-m", "pip"])
        self.assertEqual(Path(pip[-1]), self.source.resolve())
        run = subprocess.run(
            [str(launcher), "--help"],
            cwd=self.home,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usage: latex-prep", run.stdout)
        self.assertFalse(any(self.home.glob(".*rc")))

    def test_mock_homebrew_install_and_post_install_verification(self):
        self.add_tools("brew")
        result = self.run_installer("--yes", "--with-optional")
        self.assertEqual(result.returncode, 0, result.stderr)
        brew = [command for command in self.commands() if command[0] == "brew"]
        self.assertEqual(
            brew,
            [
                ["brew", "install", "--cask", "mactex-no-gui"],
                ["brew", "install", "python@3.13", "poppler", "tex-fmt", "qpdf", "mupdf-tools"],
            ],
        )
        self.assertTrue(self.launcher.exists())
        again = self.run_installer("--yes")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)

    def test_launcher_preserves_discovery_order_across_overlapping_tool_directories(self):
        # PATH chooses one engine, while ordered fallback directories choose
        # latexmk and Poppler. Those fallback directories contain other versions
        # of the same tools: sorting by the first required tool would change them.
        self.complete_tools()
        (self.tools / "latexmk").unlink()
        preferred = self.root / "preferred tools"
        preferred.mkdir()
        fallback = self.root / "second fallback"
        fallback.mkdir()
        for name in ("python3", "pdflatex"):
            shutil.copy2(self.program, preferred / name)
        for name in ("latexmk", "pdflatex", "pdfinfo"):
            shutil.copy2(self.program, fallback / name)
        # Keep the real discovery function. Replace only its host-specific paths
        # in this isolated script copy so no machine's installed TeX is involved.
        script = self.script.read_text().replace(
            "local tool_dirs=(/Library/TeX/texbin /opt/homebrew/bin /usr/local/bin)",
            f"local tool_dirs=({shlex.quote(str(self.tools))} {shlex.quote(str(fallback))})",
        )
        self.script.write_text(script)
        harness = HARNESS[: HARNESS.index("find_tool() {")] + 'main "$@"\n'
        environment = {**self.env, "PATH": "preferred tools:/usr/bin:/bin"}
        result = subprocess.run(
            ["/bin/bash", "-c", harness, "installer-test", str(self.script), "--yes"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Reuse pdflatex: {preferred.resolve()}/pdflatex", result.stdout)
        self.assertIn(f"Reuse latexmk: {fallback}/latexmk", result.stdout)
        self.assertIn(f"Reuse pdfinfo: {self.tools}/pdfinfo", result.stdout)
        # Invoke the installed launcher from a different directory and a PATH
        # that otherwise selects the unwanted copies. Execute tools, not merely
        # parse the generated launcher text.
        run = subprocess.run(
            [str(self.launcher), "--tool-locations"],
            cwd=self.home,
            env={**self.env, "PATH": f"{fallback}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            [Path(line).resolve() for line in run.stdout.splitlines()],
            [
                (fallback / "latexmk").resolve(),
                (preferred / "pdflatex").resolve(),
                (self.tools / "pdfinfo").resolve(),
            ],
        )

    def test_failed_dependency_command_stops_before_environment_creation(self):
        self.add_tools("brew")
        self.env["MOCK_FAIL"] = "brew"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 17)
        self.assertIn("installing external dependencies", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())

    def test_successful_brew_without_required_tools_is_not_claimed_as_complete(self):
        self.add_tools("brew", "python3")
        self.env["MOCK_BREW_NO_TOOLS"] = "1"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("latexmk is still unavailable", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())

    def test_pip_failure_retains_partial_environment_without_publishing_launcher(self):
        self.complete_tools()
        self.env["MOCK_PIP_FAIL"] = "1"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 19)
        self.assertIn("installing the local Python package", result.stderr)
        self.assertTrue(self.prefix.exists())
        self.assertFalse(self.launcher.exists())


if __name__ == "__main__":
    unittest.main()
