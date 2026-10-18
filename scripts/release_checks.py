#!/usr/bin/env python3
"""Checks that run before a release is published and on every pull request.

``version [--tag TAG]``
    The distribution name, the single version definition (``latexprep.__version__``)
    and an optional release tag must agree. Reads files only; nothing is imported.
``wheel DIST_DIRECTORY``
    Install the one built wheel into a fresh virtual environment and run it from
    another directory, so a file missing from the wheel cannot be hidden by the
    source tree.
``installed VERSION``
    Used by ``wheel`` inside the fresh environment: the installed distribution, its
    commands and its shipped presets match the expected version and the source tree.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
import tomllib
from importlib import metadata, resources
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = "fledge-latex"
COMMANDS = ("fledge", "latex-prep")
VERSION_ATTRIBUTE = "latexprep.__version__"
# Package data besides the presets, which are compared with the source directory.
PACKAGED_RESOURCES = ("loaded_options_hook.tex", "report.css")


def declared_version(root: Path = ROOT) -> str:
    """Return the string assigned to ``__version__`` in ``latexprep/__init__.py``."""
    init = root / "src" / "latexprep" / "__init__.py"
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit(f"{init} does not assign a string literal to __version__")


def version_problems(root: Path = ROOT, tag: str = "") -> list[str]:
    """Describe every way the metadata, the module version and the tag disagree."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    problems = []
    if project["name"] != DISTRIBUTION:
        problems.append(
            f"pyproject.toml names the distribution {project['name']!r}, not {DISTRIBUTION!r}"
        )
    dynamic = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    if "version" in project:
        problems.append(
            "pyproject.toml sets a literal version; the only definition is "
            f"{VERSION_ATTRIBUTE}, read through [tool.setuptools.dynamic]"
        )
    elif "version" not in project.get("dynamic", ()) or dynamic.get("version") != {
        "attr": VERSION_ATTRIBUTE
    }:
        problems.append(
            f"pyproject.toml must declare dynamic = ['version'] with attr = {VERSION_ATTRIBUTE!r}"
        )
    version = declared_version(root)
    if tag and tag != f"v{version}":
        problems.append(
            f"release tag {tag!r} does not match {VERSION_ATTRIBUTE} {version} "
            f"(expected 'v{version}')"
        )
    return problems


def check_version(tag: str) -> int:
    problems = version_problems(tag=tag)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    version = declared_version()
    if not tag:
        print("No release tag (manual run): checked the package metadata only.")
    print(f"Version {version} is consistent.")
    return 0


def check_wheel(dist: Path) -> int:
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        print(f"::error::Expected exactly one wheel in {dist}, found {len(wheels)}")
        return 1
    version = declared_version()
    with tempfile.TemporaryDirectory(prefix="wheel-smoke-") as temporary:
        environment = Path(temporary) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = environment / "bin" / "python"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheels[0])],
            check=True,
        )
        # Run from another directory so the checkout cannot shadow the installed package.
        for command in COMMANDS:
            printed = subprocess.run(
                [str(environment / "bin" / command), "--version"],
                cwd=temporary,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if printed != f"fledge, version {version}":
                print(
                    f"::error::{command} --version printed {printed!r}, expected version {version}"
                )
                return 1
        subprocess.run(
            [str(environment / "bin" / "fledge"), "--help"],
            cwd=temporary,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        script = Path(__file__).resolve()
        installed = [str(python), "-I", str(script), "installed", version]
        return subprocess.run(installed, cwd=temporary, check=False).returncode


def check_installed(expected_version: str) -> int:
    import latexprep

    distribution = metadata.distribution(DISTRIBUTION)
    if distribution.version != expected_version:
        print(f"Installed version {distribution.version} is not {expected_version}")
        return 1
    if "site-packages" not in Path(latexprep.__file__).parts:
        print(f"latexprep was imported from {latexprep.__file__}, not the installed wheel")
        return 1
    source = sorted(path.name for path in (ROOT / "src/latexprep/presets").glob("*.toml"))
    shipped = sorted(
        entry.name
        for entry in (resources.files("latexprep") / "presets").iterdir()
        if entry.name.endswith(".toml")
    )
    if not source or shipped != source:
        print(f"latexprep/presets/*.toml in the wheel {shipped} differ from the source {source}")
        return 1
    missing = [
        name for name in PACKAGED_RESOURCES if not (resources.files("latexprep") / name).is_file()
    ]
    if missing:
        print(f"The wheel does not ship {', '.join(missing)}")
        return 1
    print(f"{DISTRIBUTION} {distribution.version} ships presets: {', '.join(shipped)}")
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="check the name, version and release tag")
    version.add_argument("--tag", default="", help="release tag such as v0.3.0; empty to skip")
    wheel = commands.add_parser("wheel", help="install and run the built wheel")
    wheel.add_argument("dist", type=Path, help="directory holding exactly one built wheel")
    installed = commands.add_parser("installed", help="check the installed distribution")
    installed.add_argument("version")
    parsed = parser.parse_args(arguments)
    if parsed.command == "version":
        return check_version(parsed.tag)
    if parsed.command == "wheel":
        return check_wheel(parsed.dist)
    return check_installed(parsed.version)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
