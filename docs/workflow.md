# Workflow reference and support boundaries

A Python CLI that prepares a separate LaTeX submission copy and checks that the
packaged sources rebuild. It does not modify the input project or maintain
publisher rules. This reference describes the implemented subset. The broader requirements and
architecture are available in the [development and design references](development.md).

## Run locally

For macOS setup, start with `bash install.sh`; see [installation](installation.md)
for its confirmation flow and tool requirements. The commands below describe the
manual development route. Python 3.11 or later is required; the CLI uses Click
and Rich. Install the local package and its declared Python dependencies into a
virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
fledge inspect examples/nested-paper
fledge bib check examples/nested-paper --json
fledge check examples/nested-paper --main main.tex
fledge prepare examples/nested-paper \
  --main main.tex --layout flat --output /tmp/prepared-paper
```

Alternatively, install the local package with your Python package manager
(`uv tool install .`, for example) to expose the `fledge` executable.
Preparation commands never install tools automatically; the separate installer
handles explicitly confirmed setup.

Builds require `latexmk`, the selected TeX engine and its bibliography tools.
PDF checks use Poppler's `pdfinfo`, `pdftotext`, `pdftoppm`, `pdffonts`,
`pdfdetach`, `pdfimages`, and, for text geometry, `pdftohtml`. Selected PDF object
checks additionally use `qpdf`; stroke and color checks use MuPDF's `mutool`.
Formatting requires `tex-fmt`. These tools are only needed for the checks that
use them. An unavailable tool makes its configured check inconclusive.

Builds require an operational isolation backend: **macOS sandbox-exec** or
**Linux Bubblewrap** with the required user-namespace support. If a surrounding
sandbox or host policy prevents isolation from starting, builds are blocked.
Source and bibliography inspection still work. Windows isolation is not
implemented, and there is no unsandboxed fallback.

Live verification on the macOS development host covers pdfLaTeX/BibTeX package
rebuilds, two independently selected flat packages, repeated baselines, selected
bibliography/source transformations, and simple XeLaTeX/LuaLaTeX builds. Literal
import, subfile and input-merge fixtures also passed page-count, text and exact
144-DPI render comparisons. This is fixture coverage, not validation of every
combination. Biber builds and the realistic-project corpus also pass live on macOS.
Linux isolation has controlled-command tests but no live Linux run, and `qpdf`
and `mutool` adapters have controlled-output tests rather than live-tool
validation.

### Biber on macOS

TeX Live ships Biber on macOS as a self-extracting, universal binary that cannot
unpack and run itself inside the sandbox. When `bibliography_backend` is `auto` or
`biber`, Fledge instead extracts the host-architecture slice of that binary
(without `lipo` or Xcode) into a runtime-owned folder under the job directory,
runs a sandboxed warm-up that unpacks the payload there, makes the copy read-only,
and grants execution only for that sealed copy, not for anything in a writable
workspace. `bibliography_backend = "bibtex"` never prepares it, and Linux
Bubblewrap needs no preparation.

The copy costs about 175 MB of temporary space and about 10 seconds once per job.
Both count against `max_temporary_bytes` (default 2 GiB); keep that limit above
about 250 MB for Biber jobs. If preparation fails and the build needs Biber, it
reports a `BLD202` bibliography-tool finding instead of falling back to an
unconfined Biber.

## Implemented workflow

| Area | Current functionality |
| --- | --- |
| Import | Folder or ZIP; wrapper-directory handling; traversal, link, collision and size checks; OS metadata excluded; nonportable names kept with a warning |
| Sources | Root discovery, supported dependency paths, duplicate labels and unfinished-work diagnostics |
| Manuscript | Configurable class/package policies, abstract/count/heading constraints, literal author metadata, ORCID, declarations, float captions/labels/references and figure-description presence |
| Flat layout | Deterministic mapping and explicit filename overrides; literal import/subfile contexts, selected data readers and local font paths; protected naming contracts |
| Bibliography | Local checks plus opt-in field layout/removal, cited-only proposals, explicit key/merge mappings, guarded metadata edits and preservable key ordering |
| Formatting and source edits | `tex-fmt` diffs/idempotence, project exclusions, protected regions and blank-line policies; opt-in comment cleanup, standalone input merging and selected BibTeX `.bbl` inlining |
| Builds | Fresh isolated copies, controlled `latexmk`, explicit bibliography-backend selection, shell escape disabled and recorder-observed package/date/shadow policies |
| PDF | Page/text/region and artwork measurements, explicit heading expectations, bounded Type 3/structure checks, configured contrast samples, grayscale previews and text/render comparisons |
| Privacy and bundle | Configured identity scans, bounded image-metadata scans, guarded source metadata edits and final-PDF rechecks; filename/type/deliverable/size/template policies |
| Online references | Separately enabled DOI, Crossref metadata/candidate/notice and public-link checks with request limits; no automatic corrections |
| Packaging | Build-traced and literal dependencies plus extra files listed in `[package] include`; independent document selections; deterministic ZIP, fresh extraction and rebuild for each; all selected packages must verify before release |
| Interface | CLI and standalone PDF inspection; terminal/compact/JSON/HTML/CI reports, scoped review decisions, unverified diagnostic exports and a rule catalogue |
| Concurrency | Shared CPU/memory/build/render admission; overlapping PDF branches and inventories; bounded page-pair and formatting workers; serial mode |

`prepare` builds the original snapshot, applies selected changes in staging,
rebuilds and compares the result, writes a candidate archive, and rebuilds a fresh
extraction of that exact archive. Only satisfied required gates release:

```text
prepared-paper/
  sources/         prepared source files
  submission.zip   source files, without an enclosing wrapper directory
  manuscript.pdf   PDF rebuilt from the archive
  report.json      checks, changes, diffs, settings and tool diagnostics
```

With `workflow.documents`, each named package receives that layout in its own
output subdirectory, plus an aggregate report. Other document roots are verified
only when explicitly selected. Noncompiled deliverables receive their configured
file checks; their content adequacy and separate delivery are not inferred.

The output path must be new and outside the input. Reports and the delivered PDF
are outside the source ZIP. Each package contains the selected document's needed
project inputs plus explicitly listed extra files. Unrelated drafts, images, and
data are omitted from staging; originals are untouched.
Source, manuscript and bibliography policies are checked again on the extracted
archive so that transformed filenames or content cannot bypass a configured rule.

### How package contents are selected

Preparation combines complete, bounded build traces with the selected literal
source-dependency graph. It first selects inputs needed for the requested
transformations, retaining an explicitly selected bibliography-inlining `.bbl`
when applicable. After transformations it recomputes the final input set before
archiving; flattening applies to the reduced project. A missing or incomplete
build trace (compiler recorder plus the `latexmk` database) blocks preparation.

When the trace is complete, an incomplete or ambiguous literal graph is not
blocking: it becomes a `package.dependencies` warning, because the recorded inputs
are direct evidence of what the build read. Flattening and source transformations
still require a complete literal graph. If input selection fails in `prepare`, the
source, bibliography and manuscript checks still run on the full snapshot, and a
task skipped only because a prerequisite failed appears as an info/skipped
`execution.task` note instead of an error.

This is dependency-based packaging for the selected build, not proof of a globally
smallest bundle or of every possible TeX execution. The exact archive must still
rebuild and pass the required text/render comparisons before release.

Preparation does not retain unknown source/data files. List extra files that must
ship in `[package] include`; see [package contents](configuration.md#package-contents).
For independent documents, `include` globs restrict candidate inputs but do not
force all matches into the final ZIP.

## Commands and configuration

- `inspect INPUT`: source and bibliography analysis without compiling; offline by default.
- `check INPUT`: source analysis, isolated compilation and configured PDF checks.
- `prepare INPUT --output NEW_DIRECTORY`: complete supported preparation workflow.
- `prepare INPUT --dry-run`: baseline build and transformation preview; no verified bundle.
- `bib check INPUT`: standalone bibliography checks; online checks require explicit selection.
- `fmt INPUT --check`: read-only formatting check; `--diff` also prints patches.
- `pdf check FILE`: independent configured PDF inspection; `--reference-pdf` adds comparison.
  Settings that need a source project (source/PDF metadata matching, input-figure
  font and artwork policies) are reported as info "not run" instead of blocking.

All commands are noninteractive. Ambiguity returns a finding. Use `--main` if
several files look like document roots. Without `workflow.documents`, the workflow
builds **one selected document**. Use explicit package selections to verify a main
paper and supplements independently.

Use `--layout flat` to flatten sources automatically, `--format` to run tex-fmt,
and `--normalize-doi` to normalize recognized literal DOI prefixes. These operations
are opt-in. Conservative preliminary cleanup is enabled by default; it removes
OS and version-control metadata, editor backups and rebuildable auxiliary files
(such as `.aux`, `.log`, `.out`, `.nav`, `.snm`, `.vrb`, `.xdv`, index and glossary
files, and `_minted*` directories), never a `.bbl`. `--no-cleanup` disables only
that preliminary step. Dependency selection remains
mandatory and still omits unrelated files. Further transformations require their own explicit
settings; duplicate hints do not authorize a merge, and online evidence does not
authorize metadata edits. All prepared changes pass through the configured PDF
preservation gate and exact archive-rebuild verification.

`--reference-pdf FILE` makes consistency with the supplied PDF a required check,
separate from preserving the baseline. `--max-pages N` sets a total page limit,
including references and appendices. No page limit is assumed otherwise.

Use `fledge.toml` in your project, or `[tool.fledge]` in `pyproject.toml`; the
legacy `latex-prep` names still work.
The CLI discovers the nearest project configuration; `--config FILE` selects one
explicitly and `--isolated` disables discovery. CLI options override the file,
which overrides generic defaults. Unknown keys, unknown check selectors and
invalid or unsatisfiable budgets are errors. No global configuration is loaded.
See [configuration and check selection](configuration.md) for discovery,
precedence and the required validation checks that remain enforced.

```toml
[checks]
select = ["TEX", "BIB", "PDF"]
ignore = ["TEX001"]
```

Selection filters the configured/default checks; it does not infer limits or
enable transformations or network access. Use `select = ["ALL"]` for the default.
The {download}`original example settings <../examples/settings.toml>` show the flat options;
those remain supported. The {download}`additional-check example <../examples/checks.toml>`
shows typed settings tables for checks. The
[preparation options](preparation-options.md) document transformation,
multiple-document and review settings, with an
{download}`explicit example <../examples/preparation.toml>`.

| Table | Configuration scope |
| --- | --- |
| `checks` | Check-code/family selection and exclusions; required release and transformation guards remain enforced |
| `bibliography_checks` | Citation scope, explicit entry-type fields/DOI expectations and optional bibliography heuristics |
| `build_checks` | Overflow tolerance in TeX points; recorder-observed `.sty`/`.cls` allow/deny/required policies, minimum dates and local-shadow hints |
| `manuscript_checks` | Literal package declarations, abstract/heading/metadata/declaration policies and source float structure |
| `pdf_checks` | Explicit page ranges/regions, text geometry, metadata, object policies and drawing measurements |
| `package` | Extra project files to ship although the build never reads them (`include`); a packaging choice, not a check |
| `submission_checks` | Identity terms (whole-word matching by default; `identity_term_matching = "substring"` matches inside words), optional privacy scans, basename/extension policies, deliverables, archive size and local template references |
| `online_checks` | Network permission, individual remote-check switches, contact address and request/response/concurrency limits; see [online evidence](configuration.md#select-online-evidence-deliberately) |
| `source_transforms`, `formatting_options` | Copy-only source edits, flat filename overrides, formatter exclusions/protected regions and blank-line policy |
| `bibliography_transform` | Explicit formatting, field removal, cited-only output, key/merge mappings, guarded field edits and ordering |
| `metadata_privacy`, `structure_checks` | Selected literal metadata edits/image-metadata terms, per-author grammar, rendered heading expectations and supported tagged-PDF structure |
| `pdf_artwork`, `figure_artwork` | Separate final-document and input-PDF artwork policies; regions refer to each inspected PDF's own coordinates |
| `workflow`, `reporting` | Independent document packages, repeated baselines, comparison tolerances and exact scoped review decisions |

Top-level `match_source_pdf_metadata = ["title", "author"]` compares supported
literal source values with the extracted PDF properties. Generated or ambiguous
source metadata is inconclusive. Choose source matching or an explicit expected
PDF value for each field; configuring both for the same field is an error.
PDF dimensions and regions use PDF points
(1/72 inch), measured from the top-left of an unrotated, uncropped page; build
overflow uses TeX points. Section budgets use explicit inclusive page ranges.
They do not infer where references or appendices start.

Template reference paths in TOML resolve relative to the settings file's
directory. Their project-side paths identify files in the supplied source bundle
and follow those files through flattening. A required PDF/ZIP deliverable is checked by its
signature, and a text deliverable by bounded UTF-8 decoding. Presence does not
verify its content, a separate upload or an independent supplementary build.
To require an independent supplementary build, select it in `workflow.documents`.
During `prepare`, filename/type/deliverable policies are checked after dependency
selection and flattening, then rechecked on the exact archive extraction. Supply
source-relative paths for required deliverables; flattening remaps them for the
prepared and extracted checks.

Online checks need an individual switch and network permission; no switch means
no requests. [Select online evidence deliberately](configuration.md#select-online-evidence-deliberately)
states the full semantics: permission, data sharing, request budget, bibliography
scope and which snapshot is checked.

There are **156 registered check codes**, each associated with behavioral unit
tests. This counts individual diagnostics and constraints, not completed rows of
the original feature wishlist. Policies are generic and come from explicit
settings; no publisher rules are maintained. Ordinary local syntax and citation
diagnostics run by default; additional constraints and remote checks require
selection. Unconfigured measurements are inventories, not compliance
passes. See the [check catalogue and test links](checks.md) and the
[remaining-check backlog](check-backlog.md), which distinguishes missing,
partial, and intentionally excluded functionality.

```sh
fledge rules
fledge rule TEX105
fledge inspect ./paper --config ./settings.toml
```

## Readable output and agent handoff

The default Rich report puts blocking failures first, then incomplete checks and
warnings. Each finding shows its code, descriptive name, location, observed issue
and next step. Passed results and scope notes (`info` findings that are `skipped`
or `not_applicable`) are counted but listed only with `--show-passed`, in terminal,
compact and CI output alike; JSON and HTML list everything. Terminal and compact
output add an `Evidence:` excerpt: the first three recorded items of a finding's
evidence list (locations, keys or pages) and a count of the rest. A terminal report
with more than 20 changes summarizes them by kind unless `--diff` is given;
compact output always summarizes, and JSON lists every change. Colors
are optional decoration: labels and statuses remain readable in plain text.

Use compact output to paste into an LLM agent. It writes one plain line per
finding with no table, ANSI styling or hard wrapping. Embedded newlines are
escaped; progress remains on stderr. Use `--quiet` for a clean combined capture.

```sh
fledge check ./paper --output-format compact --quiet > issues.txt
fledge check ./paper --output-format json --quiet > report.json
fledge pdf check ./paper.pdf --html-report /tmp/paper-report.html
fledge check ./paper --output-format ci --quiet
```

Example compact finding:

```text
TEX001 warning/failed main.tex:3 | Unfinished text markers: Possible unfinished editing or placeholder text: 'TODO'. | next: Finish or remove the marked draft text after reviewing its context.
```

`--json` remains an alias for `--output-format json`. JSON retains full evidence,
settings, changes and stages, plus each finding's `code`, descriptive `rule`,
`status`, location and `suggestion`. An operational diagnostic or inventory has
`code: null`; an unavailable check is never silently converted to a pass. Fix
suggestions are guidance, not automatic changes to scholarly content. Re-run
checks after edits; `prepare` still requires successful preservation and archive
verification before publishing its separate output copy.
Known credential patterns and credential-bearing URLs are redacted across all
report formats, including settings, tool inventories and diffs. A plain URL
username (`ssh://git@host`) or a `mailto:` address is not a secret, whereas
`user:password@` and token-like usernames are redacted; short secrets are replaced
only as whole words. This protects
report handoff; it does not remove credentials from the submitted source files
or establish anonymity. The optional privacy checks identify candidates to review.

`--html-report NEW_FILE` writes an escaped offline report with recorded-region
diagrams and source before/after diffs; it does not annotate or embed PDF pages.
`--output-format html` emits the same document on stdout. `--diagnostics NEW_ZIP`
exports sanitized reports and diffs as explicitly **unverified diagnostic
material**, never a submission package. Selected grayscale previews use
`--preview-output NEW_DIRECTORY`, or the successful preparation output.

Scoped suppressions and accepted exceptions require exact codes, optional
location constraints and reasons. Original findings remain visible. Missing
evidence, isolation/build failures and archive mismatches cannot be waived.
Eligible accepted exceptions produce an `accepted_exceptions` outcome, not an
unqualified pass; comparison exceptions apply only to baseline versus prepared.

## Execution and reports

Use `--jobs 1` for serial expensive operations across all phases. The default is
two CPU slots, with one build and one page-pair renderer at a time. To allow more
PDF and formatting work to overlap, select explicit bounds:

```sh
fledge prepare ./paper --output /tmp/prepared-paper \
  --jobs 4 --render-jobs 2 --job-timeout-seconds 600
```

For `check`, baseline PDF analysis can begin while source checks continue.
For `prepare`, source checks and PDF analysis wait for baseline dependency
selection. PDF inventories, comparison sides and final verification branches
overlap. Page pairs and source-formatting files use bounded workers. Each file's
two formatting passes and each pair's two renders remain ordered. The baseline,
transformation, prepared build and archive rebuild gates remain ordered, and
publication waits for all required results. `--build-jobs` bounds overlapping
builds from explicitly selected independent document packages; each document's
baseline, preparation and archive gates remain ordered.

Every expensive leaf shares one CPU/memory admission budget. The coordinator
reserves 128 MiB; render reservations additionally account for both RGB buffers.
Renderer and formatter subprocesses each have a 128 MiB cap within their leaf's
larger reservation; increasing total memory alone does not raise those caps.
One job monitor samples aggregate RSS including the parent and tool groups, plus
temporary disk usage. These are reactive sampled limits with possible overshoot,
not operating-system hard memory guarantees. JSON `execution` records estimates,
observed peaks, queue/operation timings and accounting availability separately.
Subprocess libraries receive single-thread settings where available.

The default whole-job deadline is 600 seconds, the per-command timeout is 120
seconds and the temporary-data ceiling is 2 GiB (a macOS [Biber](#biber-on-macos)
job needs about 175 MB of it). Configure `memory_mb`,
`build_memory_mb`, `max_temporary_bytes` and an optional fixed `source_date_epoch`
in TOML. Cancellation stops admission and drains tools and parser readers before
workspace removal; blocked, expired or cancelled jobs publish no verified bundle.
Each input PDF is frozen once per job and workers have separate writable tool
directories with exact read-only grants to the frozen inputs.

See the [execution design and implementation status](parallelization-plan.md)
and [bounded benchmark](parallelization-benchmark.md). The defaults remain
conservative; performance depends on the document and local tools.

`--json` writes only JSON to stdout; progress uses stderr. `--quiet` suppresses
progress. `--report NEW_FILE` saves an additional report outside the source tree,
including when verification is blocked. Preparation always saves its successful
report with the output. A dry-run result says `planned`, not verified.

Outcomes and exit codes (0, 1, 2, 3, 4, 64 and 130) are listed in
[reports and exit codes](reports.md).

## Support boundaries

Flattening uses a conservative scanner for supported literal TeX commands. It is
not a general TeX interpreter. It preserves comments and literal examples and
blocks dynamic/unsupported path handling rather than guessing. Standard literal
`input`, `include`, `includegraphics`, `graphicspath`, `bibliography`,
`addbibresource`, and `bibliographystyle` are supported, alongside literal
`import`/`subimport` aliases, constrained `subfile` bodies, selected data readers
and explicit local font filenames. Font-family lookup, inferred companion faces,
dynamic paths and subfiles legacy preamble semantics remain unsupported. Subfile
content-dependent manuscript/citation checks remain inconclusive. Filename maps
and diffs are visible; local class/style and main job-name contracts constrain
renaming. Source merging supports standalone literal `input` lines, not `include`
or import execution semantics. Bibliography inlining requires an explicitly
selected standalone BibTeX `thebibliography` file, not biblatex data.

The scanner tolerates ordinary TeX that is not a dependency. An unmatched `[`
(such as the interval `[0,1)`) does not stop parsing a file. `\newif\ifX` toggles
and template classes using `\if@…` do not make the dependency graph uncertain;
only a file reference inside an executed `\if…\fi` block does. An extensionless
graphics reference matching several files (`fig.pdf` and `fig.png`), or found both
in the working directory and in `\graphicspath`, is resolved in TeX's lookup
order; `TEX008` is an advisory naming the selected file, not a blocker.
`\color` and `\textcolor` are ordinary formatting, not `TEX002` editing commands.
`TODO` and label scans skip local `.sty` and `.cls` files, and comment removal
(`TEX201`) edits only `.tex`, `.ltx` and `.latex` files.

Manuscript checks report a confirmed violation as failed even when unrelated
constructs are uncertain. Uncertainty is scoped to the checked region (a float,
the abstract or a section) and to the document body; preamble definitions do not
count. Failure messages give counts and the first locations. An abstract word
limit with unresolved macros uses a word-count range and is decisive only when the
range lies wholly inside or outside the limit. `required_sections` distinguishes
a missing heading from one that could not be confirmed. The image metadata scan
(`PRV103`) reads JPEG IPTC and extended XMP too; an EXIF MakerNote or an
unsupported IPTC layout is recorded as a limitation rather than making the scan
inconclusive.

`bib check`, `inspect` and online checks inspect only the `.bib` resources the
selected document declares, when they can be resolved, so an unused backup `.bib`
neither blocks nor spends requests; otherwise every `.bib` file in the project is
inspected. Apparently uncited entries (`BIB102`) form one finding that lists the
keys. Citation coverage stays conclusive with ordinary `\newcommand` and `\if…`
constructs; only cite-related definitions or citations inside conditionals add
uncertainty. A check with no items to examine reports `not_applicable`, never a pass.

Build entry paths currently require ASCII letters, digits, underscores, dots or
hyphens and a `.tex` suffix. Analysis/transformation currently require UTF-8 text.
Bibliography proposals preserve unaffected serialization and values. Cited-only
output and key mappings require a complete supported citation/resource graph;
macros, filtering, subfile contexts and unresolved relationships can block them.
Explicit merge mappings retain the selected target's metadata and remove the
selected donor; work identity and conflict resolution remain the user's decisions.
Guarded field edits require exact expected-before values. Ordering refuses moves
across ambiguous comment/macro or cross-reference boundaries.

PDF comparison uses page count, extracted text and exact RGB page renders at
144 DPI by default. Explicit channel/pixel tolerances and eligible reviewed changes
apply only to baseline versus prepared; repeated baselines, supplied PDFs and
archive rebuilds use exact comparisons. Required unavailable comparisons block
preparation. This is preservation evidence, not proof of semantic equivalence;
small details below the rendering resolution and differences in PDF internals can
remain undetected. PDF inspection supports up to 5000 pages, but rendering
(comparisons and rendered measurements) is capped at 300 pages, with an explicit
inconclusive message beyond that, and at 16 million pixels per page. A figure PDF
without font resources passes the figure-font policy.
Text boxes, PDF objects and drawing traces support bounded additional checks,
with their assumptions recorded in findings. Type 3 checks inspect supported
declared glyph programs, heading checks match supplied text/page expectations,
and contrast samples compare explicit rendered pixel patches. None establishes
complete glyph coverage, inferred numbering semantics, or automatic foreground
and background selection. Drawing bounds/clipping handle a limited trace subset;
curves, complex clips, masks, OCR and automatic figure/table segmentation still
need further implementation. Tagged-table/link/Figure and reference checks do
not establish natural reading order, description quality or PDF/UA conformance.
Identity terms match whole words by default (`PRV001` and `PRV002`); failure
messages list counts and locations. Identity-term matches and metadata edits do
not establish anonymity.

Concrete unfinished work includes image optimization and externalized-figure
preparation, annotated PDF pages and rendered before/after views, broader source
and PDF geometry adapters, Windows isolation and a web interface. Linux,
qpdf and MuPDF live validation also remains outstanding on the current host.
Semantic declaration/reference adequacy and general accessibility/anonymity need
human assessment; they are not closed by adding deterministic checks. The
[backlog](check-backlog.md) separates these gaps from implemented subsets and
intentional exclusions. The full requirements are not complete, and no result
guarantees publisher acceptance.

## Development checks

Run the commands in [development](development.md#run-checks).

Tests distinguish component/workflow tests with controlled tool doubles from real
tool execution. Live build checks require the tools and operating sandbox listed
above; unavailable isolation must remain visible as skipped or blocked validation.
Every registered code links to tests that exercise detection or measurement using
source fixtures or controlled tool output. A catalogue integrity test rejects
missing test associations; it supplements the behavior assertions. Click command
tests run serially in one interpreter because their stream capture is process-global.
Online tests use fake transports: they verify request handling and interpretation,
not current provider availability or coverage. Optional `qpdf`/`mutool` adapters
must likewise remain inconclusive when their tools or supported evidence are absent.
