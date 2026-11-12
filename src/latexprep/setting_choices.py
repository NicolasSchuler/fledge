"""Closed sets of values that settings accept.

Command-line choices and settings validation derive from these, so a new engine,
backend or layout is added in one place. This module imports nothing else from the package.
"""

from __future__ import annotations

from typing import Literal, get_args

Engine = Literal["pdflatex", "xelatex", "lualatex"]
# The engine setting: an engine, or "auto" to follow the main file's % !TEX program comment
# and otherwise build with pdfLaTeX.
EngineSetting = Literal["auto", "pdflatex", "xelatex", "lualatex"]
BibliographyBackend = Literal["auto", "bibtex", "biber"]
Layout = Literal["preserve", "flat"]
ENGINES: tuple[Engine, ...] = get_args(Engine)
ENGINE_SETTINGS: tuple[EngineSetting, ...] = get_args(EngineSetting)
BIBLIOGRAPHY_BACKENDS: tuple[BibliographyBackend, ...] = get_args(BibliographyBackend)
LAYOUTS: tuple[Layout, ...] = get_args(Layout)
