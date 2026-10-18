"""Installation guidance when the TeX toolchain a build needs is missing."""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace

from .findings import Finding
from .setting_choices import ENGINES, Engine

INSTALLATION_GUIDE = "https://nicolasschuler.github.io/fledge/installation.html"
_TEX_TOOLS = frozenset({"latexmk", *ENGINES})


def explain_missing_toolchain(findings: list[Finding], engine: Engine) -> list[Finding]:
    """Explain a missing TeX toolchain once, with installation steps for this system."""
    explained = []
    for finding in findings:
        # The build records which tool a ToolUnavailableError named in the finding's details.
        tool = finding.details.get("tool")
        if (
            finding.rule != "build.execution_unavailable"
            or not isinstance(tool, str)
            or tool not in _TEX_TOOLS
        ):
            explained.append(finding)
            continue
        missing = [name for name in ("latexmk", engine) if shutil.which(name) is None]
        missing = missing or [tool]
        found = " and ".join(missing)
        install = (
            "brew install --cask basictex (a small TeX distribution) or install MacTeX, then "
            "open a new terminal so /Library/TeX/texbin is on PATH"
            if sys.platform == "darwin"
            else "install TeX Live and latexmk with the system package manager (for example "
            "sudo apt install texlive latexmk on Debian or Ubuntu)"
        )
        explained.append(
            replace(
                finding,
                message=(
                    f"The TeX toolchain is unavailable: {found} "
                    f"{'was' if len(missing) == 1 else 'were'} not found on PATH. "
                    f"Building the PDF for check and prepare needs latexmk and {engine}."
                ),
                suggestion=(
                    f"To install TeX, {install}. Installation guide: {INSTALLATION_GUIDE}. "
                    "To check sources and bibliography without TeX, run fledge inspect."
                ),
                details={**finding.details, "missing_tools": missing},
            )
        )
    return explained
