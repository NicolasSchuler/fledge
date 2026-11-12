"""Installation guidance when the TeX toolchain a build needs is missing."""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace

from .documentation_links import INSTALLATION_GUIDE
from .findings import Finding
from .setting_choices import ENGINES, Engine, EngineSetting

_TEX_TOOLS = frozenset({"latexmk", *ENGINES})


def _build_engine(finding: Finding, setting: EngineSetting) -> Engine:
    """Return the engine the build of ``finding`` needed: the one it recorded, else the setting.

    The build records the engine it chose; a finding without one falls back to the setting,
    and ``auto`` to pdfLaTeX, as for a main file without a ``% !TEX program`` comment.
    """
    recorded = finding.details.get("engine")
    for engine in ENGINES:
        if recorded == engine or (recorded is None and setting == engine):
            return engine
    return "pdflatex"


def explain_missing_toolchain(findings: list[Finding], engine: EngineSetting) -> list[Finding]:
    """Explain each finding of a missing TeX toolchain, with installation steps for this system.

    A finding counts when its rule is ``build.execution_unavailable`` and the tool named in
    its details is ``latexmk`` or a TeX engine. Its message names the tools that are not on
    ``PATH`` (latexmk and the engine of the build, or else the recorded tool), and its
    suggestion gives installation steps for macOS or for other systems.

    Args:
        findings: The findings of a run.
        engine: The engine setting, used when a finding does not record the engine its
            build chose.

    Returns:
        ``findings`` in their order, each finding of an unavailable TeX tool replaced by an
        explained copy that records the missing tools in its details.
    """
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
        needed = _build_engine(finding, engine)
        missing = [name for name in ("latexmk", needed) if shutil.which(name) is None]
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
                    f"Building the PDF for check and prepare needs latexmk and {needed}."
                ),
                suggestion=(
                    f"To install TeX, {install}. Installation guide: {INSTALLATION_GUIDE}. "
                    "To check sources and bibliography without TeX, run fledge inspect."
                ),
                details={**finding.details, "missing_tools": missing},
            )
        )
    return explained
