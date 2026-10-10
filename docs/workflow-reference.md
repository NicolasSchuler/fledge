# Workflow reference

This page holds the detail behind [what prepare does](workflow.md): how package
contents are chosen, the settings tables, report formats, redaction, resource
limits and the exact source syntax the scanner supports.

## Implemented areas

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

## How package contents are selected

Preparation combines complete, bounded build traces with the selected literal
source-dependency graph. It first selects inputs needed for the requested
transformations, retaining an explicitly selected bibliography-inlining `.bbl`
when applicable. After transformations it recomputes the final input set before
archiving; flattening applies to the reduced project. A missing or incomplete
build trace blocks preparation.

When the trace is complete, an incomplete or ambiguous literal graph is not
blocking: it becomes a `package.dependencies` warning, because the recorded inputs
are direct evidence of what the build read. Flattening and source transformations
still require a complete literal graph. If input selection fails in `prepare`, the
source, bibliography and manuscript checks still run on the full snapshot, and a
task skipped only because a prerequisite failed appears as an info/skipped
`execution.task` note instead of an error.

This is dependency-based packaging for the selected build, not a proof of the
smallest possible bundle. The exact archive must still rebuild and pass the
required comparisons before release. With `workflow.documents`, each named
package receives its own output subdirectory plus an aggregate report; other
document roots are verified only when selected. During `prepare`, filename,
type and deliverable policies are checked after dependency selection and
flattening, then again on the exact archive extraction. Supply source-relative
paths for required deliverables; flattening remaps them.

Preliminary cleanup removes OS and version-control metadata, editor backups and
rebuildable auxiliary files (`.aux`, `.log`, `.out`, `.nav`, `.snm`, `.vrb`,
`.xdv`, index and glossary files, and `_minted*` directories), never a `.bbl`.
When a `<main>.bbl`, or for biblatex `<main>.run.xml`, sits beside the main
file and the build read a regenerated file of that name, `prepare` ships the
existing file; it survives cleanup and `--layout flat`. The package inventory lists it under
`details.generated_bibliography`.

## Settings tables

CLI options override the configuration file, which overrides a selected preset
and the built-in defaults. See [configuration](configuration.md) for discovery.

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

`fledge pdf check FILE` reports settings that need a source project (source/PDF
metadata matching, input-figure font and artwork policies) as info "not run"
instead of blocking.

## Report formats

The default terminal report puts blocking failures first, then incomplete checks
and warnings. Each finding shows its code, name, location, observed issue and
next step. Passed results and scope notes are counted but listed only with
`--show-passed` in terminal, compact and CI output; JSON and HTML list
everything. Terminal and compact output add an `Evidence:` excerpt: the first
three recorded items and a count of the rest. A terminal report with more than
20 changes summarizes them by kind unless `--diff` is given; compact output
always summarizes, and JSON lists every change. Colors are decoration only.

Compact output writes one plain line per finding with no table, ANSI styling or
hard wrapping; embedded newlines are escaped. Progress goes to stderr, and
`--quiet` suppresses it. `--json` is an alias for `--output-format json` and
writes only JSON to stdout.

```text
TEX001 warning/failed main.tex:3 | Unfinished text markers: Possible unfinished editing or placeholder text: 'TODO'. | next: Finish or remove the marked draft text after reviewing its context.
```

`--html-report FILE` writes an escaped offline report with recorded-region
diagrams and source before/after diffs; it does not embed PDF pages.
`--output-format html` writes the same document to stdout. `--output-format ci`
emits escaped GitHub Actions annotations. `--diagnostics ZIP` exports sanitized
reports and diffs as **unverified diagnostic material**, never a submission
package. Grayscale previews go to `--preview-output DIR`, or under `previews/`
in a successful preparation output. `--report FILE` saves JSON even when a run
is blocked; a successful `prepare` always saves `report.json` with its output.

## Redaction

Known credential patterns and credential-bearing URLs are redacted in every
report format, including settings, tool inventories and diffs. A plain URL
username (`ssh://git@host`) or a `mailto:` address is not a secret, whereas
`user:password@` and token-like usernames are redacted; short secrets are
replaced only as whole words. Redaction protects report handoff. It does not
remove credentials from the submitted sources; the opt-in `PRV004` secret scan
finds candidates there.

## Execution and resource limits

`--jobs` sets shared CPU slots; the default is 2, with one build and one
page-pair renderer at a time. `--jobs 1` runs everything serially.

```sh
fledge prepare ./paper --output /tmp/prepared-paper \
  --jobs 4 --render-jobs 2 --job-timeout-seconds 600
```

For `check`, baseline PDF analysis can begin while source checks continue. For
`prepare`, source checks and PDF analysis wait for baseline dependency
selection. PDF inventories, comparison sides and final verification branches
overlap; page pairs and formatting files use bounded workers. The baseline,
transformation, prepared build and archive rebuild gates stay ordered, and
publication waits for every required result. `--build-jobs` bounds overlapping
builds of independent document packages.

Every expensive step shares one CPU and memory budget. The coordinator reserves
128 MiB; render reservations account for both RGB buffers. Renderer and formatter
subprocesses each have a 128 MiB cap. One monitor samples aggregate memory and
temporary disk use; these are sampled limits that can overshoot briefly, not
operating-system guarantees. JSON `execution` records estimates, observed peaks,
timings and whether accounting was available.

The default whole-job deadline is 600 seconds, the per-tool timeout is 120
seconds and the temporary-data ceiling is 2 GiB. Configure `memory_mb`,
`build_memory_mb`, `max_temporary_bytes` and an optional fixed
`source_date_epoch` in TOML. Cancellation stops new work and drains running
tools before the workspace is removed. Each input PDF is frozen once per job, and
workers get separate writable directories with read-only access to the frozen
inputs. The [execution design](parallelization-plan.md) and
[benchmark](parallelization-benchmark.md) give the background.

## Biber on macOS

TeX Live ships Biber on macOS as a self-extracting universal binary that cannot
unpack and run itself inside the sandbox. When `bibliography_backend` is `auto`
or `biber`, Fledge extracts the host-architecture slice into a folder under the
job directory, runs a sandboxed warm-up that unpacks the payload there, makes the
copy read-only and grants execution only to that sealed copy.
`bibliography_backend = "bibtex"` skips this, and Linux needs no preparation.

The copy costs about 175 MB of temporary space and about 10 seconds once per job.
Both count against `max_temporary_bytes`; keep that above about 250 MB for Biber
jobs. If preparation fails and the build needs Biber, Fledge reports a `BLD202`
finding instead of running Biber unconfined.

## Supported source syntax

Flattening uses a conservative scanner for literal TeX commands, not a TeX
interpreter. It supports literal `input`, `include`, `includegraphics`,
`graphicspath`, `bibliography`, `addbibresource` and `bibliographystyle`, plus
literal `import`/`subimport`, constrained `subfile` bodies, selected data readers
and explicit local font filenames. Dynamic or unsupported path handling blocks
flattening rather than guessing. Filename maps and diffs are visible; local
class/style and main job-name contracts constrain renaming. Source merging
handles standalone literal `input` lines only. Bibliography inlining requires an
explicitly selected BibTeX `thebibliography` file.

The scanner tolerates ordinary TeX that is not a dependency. An unmatched `[`
(such as the interval `[0,1)`) does not stop parsing. `\newif\ifX` toggles and
template classes using `\if@…` do not make the dependency graph uncertain; only a
file reference inside an executed `\if…\fi` block does. An extensionless graphics
reference that matches several files is resolved in TeX's lookup order, and
`TEX008` names the selected file as an advisory. `\color` and `\textcolor` are
not `TEX002` editing commands. `TODO` and label scans skip local `.sty` and
`.cls` files.

Manuscript checks report a confirmed violation as failed even when unrelated
constructs are uncertain; uncertainty is scoped to the checked region and to the
document body. An abstract word limit with unresolved macros uses a word-count
range and is decisive only when the range lies wholly inside or outside the
limit. `required_sections` distinguishes a missing heading from one that could
not be confirmed. The image metadata scan (`PRV103`) reads JPEG IPTC and extended
XMP; an EXIF MakerNote or unsupported IPTC layout is recorded as a limitation.

`bib check`, `inspect` and online checks read only the `.bib` resources the
selected document declares, so an unused backup `.bib` neither blocks nor spends
requests; without a resolvable declaration every `.bib` file is read. Citation
coverage stays conclusive with ordinary `\newcommand` and `\if…` constructs; only
cite-related definitions or citations inside conditionals add uncertainty.

PDF comparison uses page count, extracted text and exact RGB page renders at 144
DPI. Explicit tolerances and accepted exceptions apply only to baseline versus
prepared; repeated baselines, supplied reference PDFs and archive rebuilds are
compared exactly. PDF inspection supports up to 5000 pages; rendering is capped
at 300 pages and 16 million pixels per page, with an inconclusive result beyond
that. A figure PDF without font resources passes the figure-font policy.
