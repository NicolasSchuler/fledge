"""Sphinx configuration for the local and source-distribution documentation."""

import re
from pathlib import Path

# The version is defined once, as ``__version__`` in ``src/latexprep/__init__.py``; read it
# without importing the package, so the build works from a checkout that is not installed.
_PACKAGE_INIT = Path(__file__).resolve().parents[1] / "src" / "latexprep" / "__init__.py"
_VERSION = re.search(
    r'^__version__ = "([^"]+)"$', _PACKAGE_INIT.read_text(encoding="utf-8"), re.MULTILINE
)
if _VERSION is None:
    raise RuntimeError(f"No __version__ in {_PACKAGE_INIT}")

project = "Fledge"
release = version = _VERSION.group(1)
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]
myst_heading_anchors = 4
nitpicky = True

html_theme = "alabaster"
html_theme_options = {"description": f"Version {release}"}
html_title = "Fledge"
html_show_copyright = False
html_logo = "_static/logo.png"
html_static_path = ["_static"]
html_css_files = ["logo.css", "layout.css"]
html_context = {"logo_alt": "Fledge logo: an ink-blue origami swallow with an orange fold"}
