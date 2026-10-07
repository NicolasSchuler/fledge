"""Sphinx configuration for the local and source-distribution documentation."""

project = "Fledge"
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]
myst_heading_anchors = 4
nitpicky = True

html_theme = "alabaster"
html_title = "Fledge"
html_show_copyright = False
html_logo = "_static/logo.png"
html_static_path = ["_static"]
html_css_files = ["logo.css"]
html_context = {"logo_alt": "Fledge logo: an ink-blue origami swallow with an orange fold"}
