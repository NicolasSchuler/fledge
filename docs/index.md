# LaTeX preparation

Check a LaTeX project, prepare a separate source bundle, and verify that the exact
ZIP rebuilds while preserving the document's text and rendered pages. The input
project is never edited. Constraints come from your settings; there are no
maintained publisher profiles.

Start with [installation](installation.md) and the [offline quick start](quickstart.md).
Source and bibliography inspection work without TeX. Compilation and verified
preparation also require external tools and a working isolation backend.

```{toctree}
:maxdepth: 2
:caption: Using the CLI

installation
quickstart
configuration
preparation-options
reports
troubleshooting
```

```{toctree}
:maxdepth: 1
:caption: Reference

workflow
checks
```

The documents below distinguish current behavior, measured local evidence, and
remaining design work. Requirements and plans do not establish implementation or
live-platform support.

```{toctree}
:maxdepth: 1
:caption: Development and planning

development
check-backlog
parallelization-plan
parallelization-benchmark
```
