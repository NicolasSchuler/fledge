# Changelog

All notable changes to Fledge are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as far as a 0.x release allows:
breaking changes are listed under their own heading.

The version is defined once, as `__version__` in `src/latexprep/__init__.py`.

## [0.3.0] - unreleased

Compared with the `v0.2.0` tag. That tag predates the distribution rename, the
license and the release workflow, and it still names the distribution `fledge`, so
the first release of `fledge-latex` is 0.3.0.

### Breaking changes

- **Report format version 2.** JSON reports now start with `"schema_version": 2`
  and are described by `docs/_static/report.schema.json`. Finding fields were
  renamed: `evidence` is now `evidence_kind`, and `suggestion` is now `next_step`.
  Each finding also has an `incomplete` flag for results whose evidence was
  partial, unavailable or bounded; such a failure can be neither accepted nor
  suppressed. Update scripts that read these fields.
- **Rule names are dotted.** Hyphenated rule names such as `source-edit-marker` are
  now `source.edit_marker`. The old names remain aliases for `fledge rule`, for
  check selectors, but reports show only the dotted names.
- **Doctor request errors.** In `fledge doctor` reports, a rejected request is now
  `execution.request` and an internal error `execution.internal` (formerly
  `configuration.invalid` and `execution.error`).
- **Three settings were renamed.** `format` is now `apply_formatting`,
  `pdf_artwork` is now `rendered_pdf_artwork`, and `figure_artwork` is now
  `included_figure_artwork`. The former names are still accepted in settings files
  and presets, but a file or preset layer must not use both spellings of one
  setting. Reports record the current names. The `--format` option is unchanged.

### Changed

- The distribution is published as **`fledge-latex`**, because the PyPI project
  named `fledge` is unrelated. The `fledge` command, the `latex-prep` alias and the
  `latexprep` module are unchanged. Remove an earlier source-checkout installation
  named `fledge` or `latex-preparation` before installing `fledge-latex` into the
  same environment (see the installation guide).
- Fledge is released under the **MIT License**.
- The missing-`.bbl` note (`TEX009`) is a scope note instead of a failed result,
  shown only with `--show-passed` unless `submission_checks.require_bbl = true`
  turns it into a warning.
- On macOS, the Biber toolchain is prepared only when a source mentions biblatex.
  A build that still writes a `.bcf` is discarded and rebuilt once with Biber.
- Stage findings stay separate when their evidence differs, and unavailable PDF
  measurements are marked incomplete instead of being reported as passes.
- The opt-in private-comment scan (`PRV005`) now shares the phrases of the default
  `PRV007` advisory for private notes and distribution restrictions (for example
  "note to self", "not for distribution" and "confidential"), besides `TODO`,
  `FIXME`, `XXX` and `??`. Reviewer correspondence stays with `PRV007`.
- The version is read from one place, `latexprep.__version__`; `pyproject.toml`
  declares it as dynamic.
- The source archive now carries every script that the documentation tests load
  (`scripts/*.py`) and this changelog.
- The live-test environment variable is `FLEDGE_RUN_INTEGRATION=1`. The former
  `LATEX_PREP_RUN_INTEGRATION` and `LATEXPREP_RUN_SANDBOX_TESTS` still enable the
  same tests.
- Local absolute paths in reports, the HTML report and the diagnostics bundle are
  now shown generically (`~` for your home directory, `<tmp>` for temporary
  directories), so a shared report does not reveal a user name or directory
  layout. `artifacts` in the report still lists the real paths.
- `online_contact_email` is now sent only to `api.crossref.org`, not to every
  host an online check contacts.
- An online link check that gets 403, 404, 405 or 501 for a HEAD request now asks
  again with GET before reporting the link.
- A host name that does not resolve is now reported as unresolved, and a host with
  several addresses is tried address by address.
- A missing external tool now names the package that provides it (Poppler, qpdf,
  MuPDF, `tex-fmt` or TeX Live) with the commands that install it. A
  `build.execution_unavailable` finding records the missing tool as `details.tool`.
- Settings errors name the key as you write it in a settings file and state what it
  accepts, for example `jobs must be an integer from 1 to 64` or
  `pdf_checks.raster_dpi must be an integer from 18 to 144`, and a value of the wrong
  type gets the same message. Messages call a TOML array a list instead of an
  immutable tuple. The `manuscript` and `pdf` constraints that are top-level keys
  (`abstract_min_words`, `min_image_dpi`, `expected_page_width_pt` and the rest) now
  name their key instead of "PDF dimensions and image resolution must be positive
  finite numbers". `build_jobs`, `render_jobs`, `wrap_length` and `tab_size` each
  name only the setting that is out of range.
- An unknown key inside a settings table, such as `[pdf_checks] raster_dp`, suggests
  the closest valid key and lists the valid ones, as an unknown top-level key already
  did.
- `online_replication_urls` now rejects a URL listed twice, which only spent the
  request budget twice, and a `filename_allowed_characters` of spaces alone is
  rejected as empty.
- Rendered-page measurements are much faster: ink coverage (`PDF205`), figure
  whitespace and edge ink, contrast samples and the changed-pixel count of a rendered
  comparison now use byte operations instead of per-pixel Python loops. Results are
  unchanged.

### Added

- JSON reports carry `schema_version`, and the schema is published with the
  documentation.
- Opt-in check `BIB110` verifies the biblatex `.bbl` format version a receiving
  service expects (`submission_checks.biblatex_bbl_format_version`).
- `fledge doctor` also exercises installed BibTeX and Biber as advisory stages.
  A missing tool or a failing fixture is an advisory, not a failure.
- Reports record elapsed seconds for each stage, including the transformed and
  archive builds. Sandboxed tools run with a neutral `USER` so `latexmk` no longer
  warns.
- Installation by `pipx install fledge-latex` or `uv tool install fledge-latex`,
  with guidance for Linux, Windows (WSL 2) and Overleaf, and a
  continuous-integration recipe for paper repositories.
- The macOS installer writes a marker file into its private directory first, so
  `install.sh --uninstall` can also remove an installation that failed before the
  launcher was created, and a failed install prints the exact cleanup command.
- Continuous integration builds the documentation on pull requests, lints
  `install.sh` with ShellCheck, builds and installs the wheel in a fresh
  environment, runs on Python 3.12 as well as 3.11 and 3.13, and runs the live
  loaded-options, PDF-tool and Linux isolation tests that earlier runs skipped.
  Dependabot keeps the pinned actions current.
- Package metadata now has authors, keywords, classifiers, an issue tracker and a
  changelog link.
- `--debug`, or `FLEDGE_DEBUG=1`, prints the traceback of an internal error to
  standard error and keeps standard output a clean report.
- A report or diagnostics file that cannot be saved is recorded in the report as a
  `report.write` finding that names the option and a next step.
- The terminal and CI excerpt of an unresolved finding now also lists its
  `issues`, shows the reason beside a page number, and names the DOI of each
  candidate (`NET003`, `NET004`).

### Fixed

- A crash exited with 1, the code for "passed with advisories", so CI read it as
  success; it now exits 4 with a plain message.
- `bib check`, `fmt` and `pdf check` failed when the settings defined
  `workflow.documents`; they now ignore it.
- A stray socket timeout was reported as the job deadline being exceeded.
- Copy-ready submission text silently dropped a `[...]` that followed a control
  space, `\,` or `\;`.
- A stray `$` could make the source scan hang through exponential regex
  backtracking.
- A TikZ `\path` was taken for the `url` package's verbatim `\path` and blocked
  flattening.
- TeX's 79-column log wrapping hid undefined citation keys entirely.
- A killed build was reported twice and gave no remedy.
- Poppler errors of the form `Syntax Error (N):` from a damaged PDF were accepted
  as a complete read; they now make the PDF result incomplete.
- A `pdfimages` image of type `stencil` broke the image inventory, so image
  resolution could not be checked.
- Posters of about A1 size or larger failed the page-geometry checks because the
  limit for rendered comparison was applied to every PDF.
- `BIB004` reported an undefined macro for the journal macros of a standard style
  (for example `\bibliographystyle{plain}` with `journal = jacm`) and for macros
  a project-shipped `.bst` file defines.
- Online findings lost the reason a lookup failed or was skipped; they now state
  it.
- `MAN201` counted one author twice when the name was empty and a field was also
  missing, and did not name the missing fields.
- `PRV007` flagged comments that start with an acronym such as `MLP:` as private
  author notes; only the initials of a literal `\author` name count now.
- A missing figure or bibliography file made every manuscript-constraint check
  inconclusive; only a missing `\input`, `\include` or local package does now.
- Strict decoding of EXIF text made images with non-ASCII or malformed metadata
  inconclusive in the image metadata scan; such text is now decoded leniently.
- The Linux sandbox (Bubblewrap) refusing to start was blamed on `latexmk`; the
  report now says the sandbox refused.
- A corrupt Homebrew install receipt crashed tool discovery on macOS.
- Terminal output and one-line messages now show private-use, unassigned and lone
  surrogate characters (for example the remains of an undecodable file name) as
  `\uXXXX` escapes instead of passing them on, and copy-ready submission text asks
  for manual review of them as it already did for control characters. Escapes
  beyond the basic plane in HTML reports and CI annotations are now `\UXXXXXXXX`;
  the earlier `\uXXXXX` form could not be told from a shorter escape followed by a
  digit.
- Manuscript checks (the abstract citation policy, word counts and the detection of
  unexpanded constructs) treated most natbib and biblatex citation commands, for
  example `\citealp`, `\fullcite` and `\Textcite`, as unknown macros. They now
  recognize every citation command that the bibliography checks do, including the
  capitalized forms; the abstract citation policy also counts multi-citation
  commands such as `\parencites`.
- Manuscript constraint checks did not become inconclusive when a source file was
  a symbolic link or one reference selected different files in different path
  contexts, although the citation checks already did.
- `PRV005` did not read comments in `.tikz` and `.pgf` files.
- The author-record check treated a `\NewDocumentCommand` or
  `\DeclareDocumentCommand` declaration of the author command as an author record
  and reported the result as inconclusive.
- An abstract made only of an `aligned`, `alignat` or similar math environment is
  now reported as having no literal text outside mathematics.
- Formatting skipped `.tex`, `.ltx` and `.latex` files whose extension was not
  lower case, such as `MAIN.TEX`.
- The build copy could follow a link that appeared while the project was being
  copied; it now refuses it.
- If publishing the output failed, the rollback stopped at the first directory it
  could not remove and left the others; it now removes every one it can.
- A tool version failure printed the tool's absolute path and up to 500
  characters of its error output; it now names the tool and what went wrong.
- The JSON report schema listed a `report` command that Fledge does not have.
- Reads of sources, bibliographies, build logs and build evidence checked a file
  and then opened it again, so a file replaced in between by a link or a named pipe,
  or grown past its size limit, could still be read. Each read now opens the file
  once, refuses links and special files at that point, and stops at its limit. A
  build log that is a link is therefore reported as unavailable instead of followed.
- A path in the settings (`workflow.documents` `main`, `source_directory` and
  `include`, `package.include`, formatting exclusions, source-transform paths) or
  given as `--main` was not checked for a Windows drive prefix such as `C:`
  everywhere; every one of these places now refuses it, as project import already
  did.
- The PDF inspection accepted an ambiguous `pdfinfo` report (a repeated property
  such as `Pages` or `Title`) that the other PDF checks refused, and the rendered
  comparison and the inspection accepted extracted text without its final page
  boundary. Every PDF check now applies the same preflight and page-boundary rules.

### Documentation

- The troubleshooting guide now says that online checks ignore proxy settings, and
  that `comment_policy = "private"` keeps notes such as "Reviewer 2" (use
  `"all"` to drop every comment). The installation guide states the order in which
  legacy `latex-prep` configuration names are read.
- `requirements.md` and `docs/parallelization-plan.md` are marked as historical
  design records. Hard-coded check counts were removed from the backlog, and the
  documentation tests check that every test name the published pages quote exists.

[0.3.0]: https://github.com/NicolasSchuler/fledge/compare/v0.2.0...HEAD
