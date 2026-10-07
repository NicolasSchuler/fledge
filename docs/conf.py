"""Sphinx configuration for the local and source-distribution documentation."""

project = "LaTeX preparation"
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]
myst_heading_anchors = 4
nitpicky = True

html_theme = "alabaster"
html_title = "LaTeX preparation"
html_show_copyright = False
