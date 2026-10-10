# Offline example

Use this small fixture to try the CLI without your own manuscript, including
from a wheel installation. After [installing](installation.md), run these commands
in one POSIX shell. They create a fresh temporary directory; no TeX or PDF tools
are needed.

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

Both commands should exit with code 0 and outcome `passed`. `inspect` also
shows a `TEX009` information note because the demo has no generated `.bbl`;
it does not change the outcome. Add `--show-passed` to list the individual
results. They inspect sources and bibliography only; no
build or bundle is verified. `--isolated` ignores any configuration file, and
`--offline` prevents network requests.

## Save a report

```sh
fledge inspect "$demo_dir/paper" --main main.tex --isolated --offline \
  --quiet --output-format json --report "$demo_dir/inspection.json"
fledge rule BIB001
```

JSON also appears on stdout. The [report guide](reports.md) explains it.

The source distribution includes a nested fixture too. From its root directory:

```sh
fledge inspect examples/nested-paper --main main.tex --isolated --offline
```

For your own manuscript, follow the [quick start](quickstart.md).
