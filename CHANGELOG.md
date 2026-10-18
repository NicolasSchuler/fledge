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
- **Check catalogue JSON.** `fledge rules --json` now prints an object with
  `schema_version` and a `rules` list instead of a bare list; `fledge rule CODE
  --json` and `fledge submission-text --json` gain `schema_version` too.
- **Removed `--non-interactive`.** The hidden flag did nothing, because Fledge never
  prompts; remove it from scripts.

### Changed

- Option help names the defaults it quotes from the settings, and avoids internal terms
  such as "CPU slots", "page-pair renderers" and "isolated build". The command list
  is no longer cut off, and `fledge rule CODE` no longer prints test names (they stay
  in the JSON form). `--main` has one help text everywhere.
- The "Not checked" hint names the example presets in alphabetical order, taken from
  the packaged presets.
- The distribution is published as **`fledge-latex`**, because the PyPI project
  named `fledge` is unrelated. The `fledge` command, the `latex-prep` alias and the
  `latexprep` module are unchanged. Remove an earlier source-checkout installation
  named `fledge` or `latex-preparation` before installing `fledge-latex` into the
  same environment (see the installation guide).
- Fledge is released under the **MIT License**.
- The missing-`.bbl` note (`TEX009`) is a scope note instead of a failed result,
  shown only with `--show-passed` unless `submission_checks.require_bbl = true`
  turns it into a warning.
- The `backend` detail of the missing-`.bbl` finding (`TEX009`) is now `bibtex`,
  spelled like the `bbl_format` detail of the coverage finding; it was `BibTeX`.
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
- Lists of places in messages end with `(+N more)` throughout. Manuscript-constraint
  messages (for example the floats without a caption) used `, +N more` before.
- In JSON reports, `execution.observed.memory_accounting` is now one of
  `sampled parent and registered process groups`, `not sampled` or `unavailable`; the
  reason accounting was unavailable is in the new `memory_accounting_problem`. The
  build entry `tools.execution` records the build command's `duration_seconds`.
- Rendered-page measurements are much faster: ink coverage (`PDF205`), figure
  whitespace and edge ink, contrast samples and the changed-pixel count of a rendered
  comparison now use byte operations instead of per-pixel Python loops. Results are
  unchanged.
- Messages about the output, preview output, reference PDF and job timeout no longer
  name command-line flags where the job library reports them; they name the output
  or the setting instead.
- Source-preparation messages name the files and causes behind them: comment
  removal lists the source files that could not be parsed, an unsafe document input
  and a missing, linked or non-plain required input are told apart, a failed second
  `tex-fmt` pass reports its exit status, and inlining a bibliography into a project
  with missing files is reported as failed rather than inconclusive.
- Comparison findings record the pixel-share tolerance as `max_changed_pixel_ratio`
  (formerly `pixel_tolerance`), next to `channel_tolerance`.
- Image-metadata matches (`PRV103`) name well-known fields: `EXIF Artist (0x013b)`
  instead of `EXIF tag 0x013b`, and `JPEG IPTC 2:116 (CopyrightNotice)` instead of
  `JPEG IPTC 2:116`.
- Artwork findings (`PDF301` to `PDF313`) lead with the outcome and the numbers that
  matter, such as the lowest image resolution, the thinnest stroke or the pages with
  printer marks, instead of describing the method.
- The configured PDF check groups that run together on one PDF share a single frozen
  copy and each read-only tool measurement: with every group configured, `qpdf`,
  `mutool`, `pdftotext` and `pdfinfo` run once per PDF instead of once per group.
  Findings are unchanged. The measurement output is held in memory until the checks of
  that PDF finish.
- `max_bytes` served three purposes under one name. It now stays the default of two
  limits that can be set apart: the total size of an import, of each build's copy of
  the sources and of the prepared archive that is extracted again to verify it
  (`max_import_bytes`), and the size of any one file a tool may write
  (`max_generated_file_bytes`). With `max_bytes` alone, every limit is as before. The
  `package.archive_limit` and resource-limit messages name the setting to raise.
- A tool command that a job runs without explicit limits now uses the same defaults as
  the settings: a memory limit of 1024 MiB (`build_memory_mb`) instead of 2048 MiB.
  A job always passes its own limits, so real runs are unchanged.

- The checks of one document now share one analysis of its sources instead of each
  reading and lexing the whole project again: `fledge inspect` of a project with 200
  input files (3 MB) takes about 1.6 s instead of 5.0 s, and a full run builds the
  source graph once for each tree it checks. Findings are unchanged.
- A `tex-fmt` run that a time or output limit stopped now says which limit and its
  value, as a stopped build does, instead of "did not complete".

- A defect inside one scheduled check now ends the run with the error outcome and exit
  code 4, not the blocked outcome and 3 that a problem with your files gets. The other
  checks' results stay in the report, and the failed check still has its `execution.task`
  finding with the failing frames. An expected failure such as an unreadable file keeps
  blocking with 3.
- `max_import_bytes` may not exceed four times `max_generated_file_bytes` (each is
  `max_bytes` unless set). A tool workspace holds at most that much, copy of the sources
  included, so a larger import total could never be built; the settings error now names
  both settings. Lowering `max_generated_file_bytes` alone therefore needs a lower
  `max_import_bytes` or `max_bytes` too.
- The settings that reports record show `"<set>"` for `online_contact_email` instead of
  the address, in the JSON report, the HTML report and the diagnostics bundle. The
  address itself is still sent to `api.crossref.org`.
- `fledge pdf check` no longer offers `--engine` and `--bibliography-backend`, which it
  never used because it builds nothing, and its `--reference-pdf` help describes the
  comparison of the checked PDF.
- `--select` and `--ignore`, `checks.select` and `checks.ignore` accept the name of a
  check, `source.edit_marker` or the earlier `source-edit-marker`, as the code of that
  check, and `fledge rule` explains a check by any of its names.
- A failure that is not the request's fault (for example a full disk while publishing)
  after the job started is labelled "run did not finish" instead of "request validation",
  and the terminal summary no longer calls it a request error.

### Added

- Findings that had no check code now have one, so `fledge rule`, `--select` and
  `--ignore` work for them and they carry fix guidance: the source-graph findings
  `TEX301` to `TEX327` (unreadable or over-limit sources, constructs the scan cannot
  follow, traversal limits and main-file selection), `PKG005` (nonportable file
  names), `PKG006` to `PKG008` (flat-layout errors), `PKG302` and `PKG303` (package
  inputs and archive size), `BIB008` and `BIB009`, `BLD010`, `FMT003`, `FMT004` and
  `NET008` (what online checks share). Gaps and errors are always enforced, so
  ignoring their code does not hide them; only the advisories `TEX315` to `TEX319`
  and `PKG005` can be ignored. Results that describe the run or the tool
  environment, such as `execution.*`, `build.failed` and `doctor.*`, stay uncoded.
  `fledge rules --json` also reports whether a check is `mandatory` or `reviewable`.
- `--json-report` names the option that saves the JSON report; `--report` remains an
  alias. The HTML report has a `<main>` landmark, a title that names the command and
  document, captions on its tables, and dark-mode and print styles.
- JSON reports carry `schema_version`, and the schema is published with the
  documentation.
- Opt-in check `BIB110` verifies the biblatex `.bbl` format version a receiving
  service expects (`submission_checks.biblatex_bbl_format_version`).
- `max_import_bytes` and `max_generated_file_bytes` settings, documented with the other
  size, time and memory limits in the configuration guide. The guide also explains the
  two `min_stroke_width_pt` settings (`PDF213` under `[pdf_checks]` and `PDF309` under
  the artwork tables) and the two embedded-font settings.
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
- Diagnostic logging. The hidden `--debug` option and `FLEDGE_DEBUG=1`, which only
  printed tracebacks, now also print log lines to stderr: each tool command's start
  and end (tool name, plain option names, exit status and duration, never a path),
  a limit that stopped a tool, online requests (host, status and duration, never a
  URL or the contact address), scheduler tasks and their block reasons, PDF
  measurement reuse, which settings file was found and whether it uses a legacy
  name, and source-analysis cache builds. Standard output stays the report, and
  the lines show the home and temporary directories generically. Without the
  option the library prints nothing.

### Fixed

- Copy-ready submission text could read the wrong raw source for a span between two
  included files when that text also occurred, blanked, earlier on the same line (for
  example after `\verb|...|`), and then showed a stray backslash or letter. Every
  copied span now records its exact offset in its file.
- Redirected terminal reports no longer carry trailing spaces on wrapped lines.
- Configuration discovery no longer reads a settings file from the system temporary
  directory, where any local user could have placed one.
- A crash exited with 1, the code for "passed with advisories", so CI read it as
  success; it now exits 4 with a plain message.
- `bib check`, `fmt` and `pdf check` failed when the settings defined
  `workflow.documents`; they now ignore it.
- A stray socket timeout was reported as the job deadline being exceeded.
- When the combined report of a multi-document `prepare` could not be represented
  within the redaction limits, the command failed with an exception; it now reports a
  `report.redaction_limit` error and publishes nothing, as it does for one document.
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
- The temporary state directory of every tool command stayed in the job workspace
  until the job ended, so long jobs counted thousands of leftover directories against
  the workspace and storage limits; each is removed when its command ends.
- Removing discovered secrets from submission findings recursed without a bound, so
  an unusually deep or self-referencing value crashed it; such a finding is now
  withheld whole and says so.
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

- `--debug` and `FLEDGE_DEBUG=1` printed a traceback with this machine's paths, credentials
  and terminal escape sequences unchanged. The traceback, and the exception text that the
  debug log now includes for a defect in a check, pass through credential redaction,
  generic home and temporary directories and terminal sanitizing. The log's path scrubbing
  also no longer rewrites the log record that other handlers receive.
- `fledge inspect INPUT | head` ended with exit status 1 when the reader closed the pipe
  early, hiding the verdict of the run; the exit code is now the outcome's.
- `--json-report X --html-report X` (or any two report destinations that name one path) was
  rejected with exit 4 but still wrote the file; no report file is written then.
- When publication failed and its partial output could not be removed, only a note on the
  hidden exception said so. The progress output and the report now name the leftover
  directory and say to remove it manually.
- A version check stopped by a resource limit was reported as "it exceeded a resource limit"
  without the limit; it now gives the limit and the setting to change. The documented
  `max_import_bytes` example (1 GiB with a 64 MiB file limit) could never build and now
  uses values that do. The `PKG303` fix text said `max_bytes` where the check reports
  `max_import_bytes`.
- Damaged-PDF detection missed Poppler's `Config Error`, `Permission Error` and
  similar categories, and `error:` from MuPDF in the middle of a line; every
  `<category> Error:` and `error:` now makes the PDF result incomplete, while
  warnings and `Unimplemented Feature` do not.
- A build dependency spelled with `..` (for example `root/../outside/file`) was accepted as
  a file inside the project because the containment test was only lexical.
- Messages read "Choose a different output path" instead of "Choose a output path",
  "spans 1 page (page 1)" instead of "spans 1 pages (pages 1-1)", and a missing
  dependency lists "a, b, c, d, e (+5 more)" like every other list.
- A comment-only line between `\input` (or `\includegraphics`) and its argument no longer
  turns the dependency into an unresolved dynamic path.
- Pressing Ctrl-C while the final report was being written, after the output was published,
  claimed that no verified output was published; the message now says it stays in place.
- `fledge submission-text --json` printed nothing to standard output when it failed; it now
  prints an object with `schema_version` and `error`, with the same exit code.
- A settings error for a file that still uses a former table name such as `[pdf_artwork]`
  named `rendered_pdf_artwork`, a key the file never contained; it now names the key as
  written.

### Documentation

- The configuration guide states exactly when discovery reads the system temporary
  directory (the input is that directory, or a ZIP or PDF lies directly in it) and that a
  `pyproject.toml` that is not valid TOML stops discovery with "Cannot read settings"
  instead of being skipped. The reports and troubleshooting guides describe the exit code
  and the traceback of a defect inside a check.
- The troubleshooting guide now says that online checks ignore proxy settings, and
  that `comment_policy = "private"` keeps notes such as "Reviewer 2" (use
  `"all"` to drop every comment). The installation guide states the order in which
  legacy `latex-prep` configuration names are read.
- `requirements.md` and `docs/parallelization-plan.md` are marked as historical
  design records. Hard-coded check counts were removed from the backlog, and the
  documentation tests check that every test name the published pages quote exists.
- A module map (`docs/modules.md`) assigns every module of the flat `latexprep`
  package to one of five layers with a one-line responsibility, and
  `architecture.md` relates the layers to its components. A test fails when a module
  is missing from the map, imports from a layer above its own, or when `click` or
  `rich` is imported outside the presentation layer.

[0.3.0]: https://github.com/NicolasSchuler/fledge/compare/v0.2.0...HEAD
