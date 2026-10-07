# Offline example

Use this small fixture to try the CLI without your own manuscript, including
from a wheel installation. After [installing](installation.md), run these commands
in one POSIX shell. They create a fresh temporary directory; no TeX or PDF tools
are needed. With the macOS installer, use `~/.local/bin/fledge` in place of
`fledge` below unless its directory is on your `PATH`.

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

## Inspect it

```sh
fledge inspect "$demo_dir/paper" --main main.tex --isolated --offline
fledge bib check "$demo_dir/paper" --main main.tex --isolated --offline
```

Both commands should return exit code 0 with `outcome: passed` and four passing
findings. Their scope is source/bibliography inspection; no build or bundle is
verified. `--isolated` disables configuration discovery, while `--offline`
disables reference-network requests.

## Save a report

```sh
fledge inspect "$demo_dir/paper" --main main.tex --isolated --offline \
  --quiet --output-format json --report "$demo_dir/inspection.json"
fledge rule BIB001
```

The report file must be new and outside the input. JSON also appears on stdout.
Use the [report guide](reports.md) to interpret it.

The source distribution includes a nested fixture too. From its root directory:

```sh
fledge inspect examples/nested-paper --main main.tex --isolated --offline
```

For your own manuscript, follow the [quick start](quickstart.md).
