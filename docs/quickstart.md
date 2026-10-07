# Quick start

After [installing the CLI](installation.md), start with an offline source check.
The first example requires neither TeX nor PDF tools. It works from a source
checkout or a wheel installation.

## Create a small paper

These POSIX-shell commands create a new temporary directory. Keep using the same
shell so that `demo_dir` stays available. The fixture contains only local files.

```sh
demo_dir="$(mktemp -d)"
mkdir "$demo_dir/paper"
cat > "$demo_dir/paper/main.tex" <<'TEX'
\documentclass{article}
\title{A small paper}
\author{Example Author}
\date{}
\begin{document}
\maketitle
\begin{abstract}
A minimal project for checking a local source bundle.
\end{abstract}
\section{Introduction}
The source cites a book about TeX~\cite{knuth1984}.
\bibliographystyle{plain}
\bibliography{references}
\end{document}
TEX
cat > "$demo_dir/paper/references.bib" <<'BIB'
@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The {TeXbook}},
  year = {1984},
  publisher = {Addison-Wesley}
}
BIB
```

For your own work, keep private manuscripts outside the tool's repository or
under its ignored `.local/` directory. Review paths before staging: PDFs and
other source assets are intentionally trackable.

## Inspect without compiling

```sh
latex-prep inspect "$demo_dir/paper" --main main.tex --isolated --offline
latex-prep bib check "$demo_dir/paper" --main main.tex --isolated --offline
```

`--isolated` disables configuration discovery; it does **not** disable the
runtime isolation used for compilation. `--offline` denies reference-network
requests. The source fixture should pass these checks with exit code 0.
The report states that compilation and PDF or bundle verification were not run.
A passing source check does not certify a submission ZIP.

To save the same inspection as JSON, use a new file outside the paper directory:

```sh
latex-prep inspect "$demo_dir/paper" --main main.tex --isolated --offline \
  --quiet --output-format json --report "$demo_dir/inspection.json"
latex-prep rule BIB001
```

`--report` writes an additional JSON file; JSON still appears on stdout in this
example. Use a fresh filename if you repeat the command. See
[reports and exit codes](reports.md) for how to interpret findings.

With a source checkout or extracted source distribution, you can also inspect
the included nested project from its root directory:

```sh
latex-prep inspect examples/nested-paper --main main.tex --isolated --offline
```

## Check the build, then prepare a copy

This step needs the [build and PDF tools](installation.md#add-tools-for-the-operations-you-need)
and a working macOS or Linux isolation backend. It is optional for the offline
inspection exercise above. Using the same shell and fixture:

```sh
latex-prep check "$demo_dir/paper" --main main.tex --isolated --offline
latex-prep prepare "$demo_dir/paper" --main main.tex --isolated --offline \
  --layout flat --output "$demo_dir/prepared-paper"
```

Preparation builds a baseline, changes a staging copy, checks preservation, then
rebuilds a fresh extraction of the exact ZIP. When all required gates succeed,
the new output directory contains:

```text
prepared-paper/
  sources/
  submission.zip
  manuscript.pdf
  report.json
```

The input remains unchanged. The ZIP contains the source files without a wrapper
directory; the delivered PDF and report sit beside it. The output path must not
already exist and must be outside the input. A blocked run does not release a
verified bundle. Review advisories and any accepted exceptions in `report.json`;
they differ from a clean pass.

Use `prepare --dry-run` to preview transformations after the baseline build.
It still needs compilation tools and isolation, and its `planned` result is not
a verified package. Use [configuration](configuration.md) to set actual page
limits and manuscript policies, and [preparation options](preparation-options.md)
to request further transformations.
