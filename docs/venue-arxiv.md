# Prepare an arXiv upload

arXiv compiles your LaTeX source itself and publishes that source next to the
paper, so two things matter: the upload must build on arXiv's side with the
tools arXiv runs, and it must not reveal anything you would not publish. The
`arxiv` preset switches on the checks for both. It is a starting point, not an
arXiv policy: Fledge maintains no venue rules, and passing the preset does not
mean that arXiv will accept the upload. Read arXiv's current submission
instructions alongside this page.

## Run it

```sh
fledge init paper --preset arxiv          # writes paper/fledge.toml to edit
fledge check paper --main main.tex        # build and check with those settings
fledge prepare paper --main main.tex -o arxiv-upload
```

Upload `arxiv-upload/submission.zip`. To try the preset without writing a
settings file, add `--preset arxiv` to `check` or `prepare` instead of running
`fledge init`. `arxiv-upload/manuscript.pdf` is the PDF Fledge rebuilt from that
ZIP with your TeX installation; arXiv builds its own.

## What the preset checks

| Concern | Checks | What to do when one reports |
| --- | --- | --- |
| Engine | `TEX013` warns when the document builds with an engine arXiv does not offer; the preset lists pdfLaTeX and XeLaTeX, not LuaLaTeX | Build with `--engine pdflatex` or `xelatex`, or a `% !TEX program` comment, if the document allows it |
| Figures | `TEX011` blocks EPS and PS figures without a converted PDF, because arXiv converts no figures | Convert each figure to PDF (`epstopdf figure.eps`) and include the PDF |
| Output drivers | `MAN018` reports driver options such as `[pdftex]` or `[dvips]` and the `psfig` package, which break when arXiv picks the TeX processor | Remove the option and let the package detect the driver; use `graphicx` instead of `psfig` |
| Generated lists | arXiv runs neither makeindex nor makeglossaries, so `prepare` ships the index, glossary and nomenclature files its build generated; `TEX012` warns when one is missing | Run `prepare`, or keep the generated file next to the main file |
| Bibliography | arXiv runs BibTeX or Biber when no `.bbl` is uploaded, so `prepare` ships your `.bib` files and no `.bbl` | Nothing, unless you want to upload a `.bbl`; see below |
| Published source | Private notes and TODO markers in comments (`PRV005`), credential-shaped text (`PRV004`), shell-escape constructs (`PRV006`) and files the paper does not use (`PKG106`) | Remove the text or file from your project; `prepare` also removes comments with private markers from the copy (`comment_policy = "private"`) |
| Fonts | `PDF104` requires embedded fonts in your local build | Re-export the figure or load a font that embeds |

`comment_policy = "private"` removes only comments that contain `TODO`, `FIXME`,
`XXX`, `private`, `internal`, `confidential` or `note to self`. Check the
prepared copy, or set `comment_policy = "all"` in `[source_transforms]` to remove
every removable comment.

Comments cannot be removed from a source file that is not UTF-8, such as Latin-1
(`TEX328`), because Fledge never rewrites such a file; `prepare` then stops with
`TEX201`. [Convert the file](troubleshooting.md#tex328-a-source-file-that-is-not-utf-8),
or set `comment_policy = "retain"` and remove the comments yourself.

## Fill in the placeholders

The generated `fledge.toml` leaves values that change over time as commented
placeholders. Copy them from arXiv's current instructions, not from memory:

- `max_archive_bytes` (`PKG105`): the upload size limit;
- `filename_allowed_characters` (`PKG102`): the characters arXiv accepts in file
  names;
- `expected_texlive_year` under `[build_checks]` (`BLD105`): the TeX Live release
  arXiv compiles with, so a different local release is reported.

## Upload a `.bbl` on purpose

An uploaded `.bbl` takes precedence over arXiv's own BibTeX or Biber run, so a
stale one replaces your bibliography. If you want to upload one anyway, set this
in `fledge.toml`:

```toml
[submission_checks]
venue_runs_bibliography_tools = false
check_bbl_coverage = true                # BIB109: the .bbl contains every cited key
# biblatex_bbl_format_version = "3.3"    # BIB110, biblatex only: the format of arXiv's TeX Live
```

`prepare` then ships the `.bbl` its verified build generated, and a missing one is
a warning (`TEX009`). A project that already holds only the `.bbl` named after the
main file, without the `.bib` files, is fine as it is: the build reads that `.bbl`, so
the missing database is not a missing file (`TEX005`), and the bibliography checks
that read entries say that they could not. A biblatex `.bbl` must have the format version that the
biblatex of arXiv's TeX Live release reads; the second line of a `.bbl` built
with that release shows it.

## Before you upload

- Fix every error and decide on every warning; `fledge rule CODE` explains any
  code.
- Look at the "Not checked" line: checks that need values you have not set did
  not run.
- arXiv's processing is the final word: check the PDF arXiv shows you before you
  announce the paper.

See [configuration](configuration.md) for the settings and
[generated files](preparation-options.md#generated-files) for what `prepare`
ships.
