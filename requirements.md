# LaTeX Submission Preparation Requirements

Status: Draft for product and implementation planning  
Date: 6 October 2026

Implementation coverage and remaining checks are tracked in the
[check backlog](docs/check-backlog.md); the [README](README.md) describes shipped behavior.

## Purpose and scope

The software prepares a LaTeX project supplied as a folder or ZIP archive for submission. It checks the source, builds and inspects the PDF, automatically performs enabled preparation operations on a separate copy, and produces a submission bundle that has been tested after packaging. Operations include flattening the folder hierarchy, rewriting file references, cleaning bibliography data, formatting source, and excluding unnecessary files. Authors receive an actionable report explaining remaining problems and exactly what changed.

The central requirement is preservation: preparation must retain the author's scientific content, citations, and intended appearance unless the author explicitly selects a change. A successful compilation alone is insufficient to establish that cleanup preserved the document or that a publisher will accept it.

The primary users are researchers preparing anonymous submissions, final manuscripts, preprints, and associated supplementary documents. Automated use in continuous integration should use the same checks and configuration as local use.

In this document, **must** denotes an acceptance requirement for the delivery stage assigned below. CLI execution, automatic folder flattening, and the exclusion of maintained publisher-specific rules are product requirements. Choices explicitly marked **proposed** remain open to revision before implementation.

## Product decisions

- Provide a command-line interface first and an interface-independent core suitable for a later web interface. Initial operating-system targets are proposed as macOS and Linux; Windows support requires separate validation.
- Keep the original folder or ZIP unchanged. Write prepared sources, reports, and archives to a separate destination; do not offer in-place editing in the first release.
- Make compilation, formatting, local bibliography checks, PDF checks, and packaging work offline. Enable external metadata lookup separately.
- Treat `tex-fmt` as the intended formatter mentioned as “text-fmt” in the request. Use a distinct bibliography cleanup component.
- Ship generic operations and configurable checks. Do not maintain publisher, journal, conference, year, or arXiv compliance profiles, package allowlists, template registries, or automatically scraped guideline rules. Users supply the constraints relevant to their current submission in CLI options or a reusable project settings file.
- Make a flat submission directory a first-release output option. Selecting it authorizes supported moves, deterministic collision renaming, and reference updates in the copy without per-file confirmation; unsupported transformations produce actionable blockers.
- Deliver a complete preparation workflow in the first release. Advanced PDF interpretation must not delay basic cleanup and a tested submission ZIP.

## Feature functionality

**First release** is the minimum useful product. **Extended checks** add deeper structural and visual analysis. **Optional** features are outside the initial implementation commitment. Rows spanning stages define the initial boundary explicitly.

| Feature | Supported functionality | Delivery |
| --- | --- | --- |
| Project import | Folder and ZIP input, safe extraction, root selection, main and supplementary targets | First release |
| Dependency discovery | Resolve sources, figures, bibliography, local styles, fonts, and data; retain uncertain dependencies | First release |
| Folder flattening | Move submission files to one level, resolve name collisions, rewrite references, rebuild automatically | First release |
| Compilation | Configurable engine and bibliography backend, isolated builds, bounded execution, structured logs | First release |
| Source checks | Missing citations and references, duplicate labels, TODOs, missing assets, draft settings | First release; deeper structural checks later |
| Manuscript completeness | Abstract and keyword counts, required statements, author fields, identifiers, supporting files | First release basics; semantic interpretation optional |
| Template inspection | Loaded class/package inventory, local shadow copies, user-supplied constraints and reference files | First release inventory; comparisons later |
| Bibliography cleanup | Syntax and field checks, duplicate detection, DOI normalization, reviewed merges, conservative pruning | First release |
| Bibliography enrichment | DOI lookup, metadata comparison, proposed corrections, published-version candidates | Optional online module |
| Source formatting | `tex-fmt` integration, check and diff modes, configurable indentation and wrapping, protected regions | First release |
| Submission cleanup | Exclude build debris and private files; controlled comment removal; explain every removal | First release |
| PDF validation | Page size and count, font embedding, encryption, annotations, attachments, metadata | First release |
| PDF geometry | Margin and column overflow, small text, overlap and clipping candidates with marked locations | Extended checks |
| Figure quality | Effective raster resolution and embedded fonts; deeper vector, line-width, and figure-text analysis | First release basics; extended checks later |
| Accessibility | Configured figure descriptions and placeholders; grayscale previews and deeper PDF checks | Extended checks |
| Project settings | User-supplied limits, expected structure, template constraints, bibliography strategy, archive layout | First release; no publisher catalog |
| Anonymity and metadata | Inspect author fields, acknowledgments, comments, filenames, and PDF metadata for identity leaks | First release basics; cross-document checks later |
| Change verification | Compare baseline, prepared and archive-rebuilt PDFs; optionally compare a supplied submission PDF | First release |
| Submission packaging | Configured folder/ZIP output, supplementary files, independent rebuild from final archive | First release |
| Reporting and automation | Terminal and JSON reports, meaningful exit codes, scoped suppressions; annotated HTML and CI integration later | First release basics; extended checks later |
| Web interface | Upload a project, select operations, inspect findings/diffs, download prepared files using the same core | Optional later interface |
| Advanced transformations | Single-file source merging, bibliography inlining, image optimization, externalized figures, language advice | Optional |

## End to end workflow

1. **Inspect the input.** Import an immutable working snapshot, select the document roots, and show detected engines, dependencies, and ambiguous choices.
2. **Select the operations.** Resolve project settings, layout, output location, and requested transformations. Display the effective settings. Do not infer acceptance rules from a publisher name, document class, or submission stage.
3. **Build a baseline.** Compile the unmodified snapshot in isolation and collect source, bibliography, log, and PDF findings. If a submission PDF is supplied, compare it with the build; its presence alone is not evidence that the source builds.
4. **Prepare a change plan.** Record file moves/exclusions, source and bibliography diffs, reasons, and changes requiring review. Provide a dry-run preview. Enabled deterministic operations proceed automatically; in noninteractive use unresolved review decisions stop the dependent transformation with an actionable result.
5. **Apply selected changes.** Transform only the staging copy. Rebuild and compare it with the baseline. Unexpected output changes prevent validation until resolved or explicitly accepted.
6. **Package and verify.** Create the configured archive and any separately selected source packages. Extract each exact package into a fresh directory and rebuild its assigned documents using only its contents and the declared toolchain. Inspect the resulting PDFs again.
7. **Deliver the result.** Provide the prepared source folder, submission ZIP, final PDF, change summary, and report. Keep diagnostic reports, internal paths, and review material outside the submission ZIP unless explicitly included.

The user must also be able to run inspection, bibliography checks, formatting checks, and PDF inspection independently. If the baseline cannot build, source diagnostics and proposed patches remain available, but preservation and submission readiness remain unverified.

## Project import and dependencies

- Accept nested projects and archives with a single wrapping directory. Detect candidate entry points and permit explicit selection. Multiple plausible roots must not be resolved by silently choosing the first filename.
- Support one primary document and explicitly declared supplementary roots. Assign each build root to a submission package, build each separately, and apply that package's configured layout and inclusion rules. A separately delivered source package must include its own dependencies; noncompiled supporting deliverables require file checks rather than a TeX build.
- Reject archive entries that escape the destination, absolute paths, links escaping the project, duplicate normalized names, and platform filename collisions. Bound extracted size, file count, nesting, and processing time; reject encrypted or unsupported archives with a clear explanation.
- Combine source inspection with files observed during successful builds, such as recorder output. Track `\input`, `\include`, `\includegraphics`, bibliography resources, local classes and styles, font files, and data consumed by figures.
- Distinguish project files from dependencies supplied by the declared TeX distribution. Detect absolute paths, case mismatches, user-specific TeX trees, and dependencies outside the project. Do not copy external files silently.
- Treat dynamic filenames, custom macros, conditional inputs, and incomplete build traces as uncertainty. Preserve potentially needed files unless the user explicitly excludes them. Observe all declared targets; one successful build does not prove a file is globally unused.
- Support explicit include and exclude rules for supplemental data and other noncompiled deliverables. Report conflicts when an exclusion removes an observed dependency.

## Automatic folder flattening

Folder flattening relocates the submission files into one directory while retaining separate source files. Merging all LaTeX content into a single `.tex` file is a separate optional operation. The first release must support `preserve` and `flat` layouts; the proposed default is `preserve`, with `--layout flat` enabling automatic flattening for a submission that needs it.

- Build an explicit mapping from original relative paths to output filenames before changing any files. Keep existing basenames when unambiguous. Resolve collisions deterministically with readable path-derived names, accounting for case folding, Unicode normalization, reserved names, filename limits, and already generated names. Never overwrite or silently merge distinct files.
- For each package configured as flat, move every selected source, image, bibliography, local style, and supporting data file into one submission directory. Its ZIP must contain those files at its root, without a wrapping directory or retained project subdirectories. If a supplementary deliverable needs its own internal structure, the user must explicitly select separate delivery or a separate archive; do not silently restructure its internals.
- Rewrite supported file references according to their original resolution context, including `\input`, `\include`, `\includegraphics`, `\graphicspath`, `\bibliography`, `\addbibresource`, and `\bibliographystyle`. Extensionless references must resolve to the same file as before; use explicit extensions where appropriate. Do not replace path-like prose, URLs, citation keys, or literal code examples by global text substitution.
- For `\graphicspath`, `\import`, `\subimport`, `subfiles`, data/table readers, external-document references, and custom path commands, publish an explicit support boundary. Handle supported forms with command-aware rewriting; identify unsupported constructs and offer exact file/command mappings. Do not claim successful flattening while unresolved references still depend on removed directories.
- Preserve engine and bibliography naming contracts, root/job names, multi-file image companion relationships, and package/class identity. Renaming a `.sty`, `.cls`, or generated bibliography file is not always equivalent to renaming a figure. If the mapping cannot preserve these relationships, stop that transformation with a concrete explanation.
- Detect dependencies that would become ambiguous after relocation, including collisions with installed classes/styles and files selected through search paths. Permit explicit filename overrides. An optional unique-stem constraint may apply across file extensions; it must not break required `.tex`/`.bbl` relationships. Resolve incompatible naming requirements explicitly.
- Record the old/new path mapping and modified references in the external change report. When the supported transformation is unambiguous, apply it without individual rename approvals. Preserve the original tree and make repeated preparation deterministic and idempotent.
- Rebuild all roots assigned to a flat package from its prepared copy and again from that exact final ZIP, with no access to the original paths or other output packages. Verify separately delivered source packages independently. Compare their PDFs with the respective baselines. A successful build alone cannot detect accidentally switching two different images with the same basename.

For example, a collision can be resolved without losing either figure:

| Original file | Flat output | Rewritten reference |
| --- | --- | --- |
| `figures/results.pdf` | `figures-results.pdf` | `\includegraphics{figures-results.pdf}` |
| `appendix/results.pdf` | `appendix-results.pdf` | `\includegraphics{appendix-results.pdf}` |
| `sections/method.tex` | `method.tex` | `\input{method.tex}` |

These names illustrate the operation; the naming algorithm must also resolve collisions introduced by path sanitization.

## Compilation and log analysis

- Support pdfLaTeX, XeLaTeX, and LuaLaTeX with BibTeX or Biber where available and permitted by user configuration. Allow explicit selection and show detected settings. Unsupported legacy or custom pipelines must be identified as such.
- Run the required bibliography and reference passes to convergence within configured limits. Report unresolved rerun requests, tool failures, missing dependencies, and timeouts separately.
- Extract errors, undefined citations and references, multiply defined labels, missing characters, font substitutions, and overfull or underfull boxes. Include source locations and numerical overflow where available, and deduplicate repeated-pass messages.
- Never treat an old PDF, a partial PDF produced after errors, or the mere existence of an output file as build success.
- Compile without network access or shell escape by default. Enforce a process boundary limiting reads to the staged project and declared toolchain resources, and writes to job directories. A temporary directory and `-no-shell-escape` alone are not a sufficient sandbox.
- Do not execute project-supplied scripts or executable configuration, including `.latexmkrc`, merely because it was imported. Explicitly trusted custom execution must remain isolated, bounded, and visible in the report. If required isolation is unavailable, retain inspection functionality and report compilation as blocked.
- Detect missing tools without installing packages or changing the user's environment automatically. Report tool versions and the actual build command without exposing credentials.

## Source and structural checks

The first release must detect missing citation keys, unresolved references, duplicate labels, missing assets, TODO markers, placeholder text, visible editing commands, and user-disallowed draft settings. Include common leftovers such as `FIXME`, `XXX`, `\todo`, highlighting, editing colors, draft watermarks, line numbers, `\showframe`, and dummy author/DOI fields. Checks must distinguish executable content from comments and literal examples, and use compilation evidence when available.

Extended checks must add figure and table labels/captions, label placement, unreferenced floats, configurable numbering and first-reference order, apparently empty sections, hard-coded reference numbers, and suspicious layout overrides. Examples include negative spacing, manual page breaks, excessive font shrinking, forced float placement, and changes to margins, text width, or line spacing. Rendered order and appendix numbering need context; source order alone is insufficient.

These patterns are diagnostic evidence, not proof of an error or intent. Legitimate full-width floats, intentional blank pages, package-specific label conventions, and custom reference macros must be configurable. A literal `??` or a negative `\vspace` must not automatically become a blocking violation.

## Manuscript completeness and template inspection

- Extract supported abstract, keyword/index-term, heading, author, and affiliation structures. Report word/keyword counts and heading depth; apply minimums, maximums, and required fields only when the user supplies them. State the counting method, treatment of mathematics, and uncertainty from unresolved macros.
- Check user-requested statement presence, including data availability, funding, competing interests, author contributions, ethics, or AI-use declarations. A heading or paragraph can establish presence, not factual adequacy. Do not infer that a study involved human participants or AI use, or generate declarations without author input.
- Inspect supplied author metadata and corresponding-author fields, and validate ORCID syntax/checksum when present. Format validity does not verify identity or ownership. Compare supported source and PDF metadata without guessing hidden submission-system records.
- Check the presence of explicitly requested deliverables such as a title page, highlights, graphical abstract, cover letter, replication-package link, response letter, and supplementary files. Keep files meant for separate upload distinct from files required to compile the manuscript. Do not invent their content.
- Inventory the actual loaded document class, options, packages, and local copies that shadow installed files. Accept user-supplied expected values, allow/deny lists, and a reference template for comparison. A class name or a different checksum alone does not prove the file is official, outdated, or improperly modified; compare like-for-like versions and report the evidence.
- Keep template updates, declaration writing, and layout changes outside automatic cleanup. Do not maintain an official-template download registry or offer an unsupported claim of publisher compliance.

## Bibliography checks and cleanup

### Local validation

- Parse BibTeX and biblatex data without discarding unknown fields. Diagnose syntax errors, repeated fields, duplicate keys, undefined string macros, and broken entry relationships.
- Check required fields according to entry type, bibliography system, and user settings. Handle articles, proceedings, books, preprints, datasets, software, and web resources without imposing article-only fields on all entries.
- Inspect malformed DOI values, suspicious URLs, missing year or venue information, inconsistent page ranges, and capitalization that may need protection. Missing DOI is an advisory finding unless explicitly required by configuration; many legitimate references have no DOI.
- Detect exact and likely duplicate works separately. Present the matching evidence, field conflicts, and affected citation keys. A preprint and a published paper must not be merged solely because their titles resemble each other.
- Check references used by every declared document, including `\nocite`, and retain transitive dependencies such as `crossref`, `xdata`, sets, related entries, `@string`, and `@preamble`. Account for supported bibliography filters and mappings; if citation coverage is uncertain, do not automatically prune entries.

### Cleanup operations

- Offer consistent indentation, field layout, and optional ordering while preserving parsed values, brace protection, author names, diacritics, macros, and citation keys. Entry sorting needs review where input order can affect output.
- Normalize supported DOI representations according to the selected field convention, such as storing a bare identifier in a `doi` field, as a visible patch. Source-field normalization must not silently remove a full DOI link required in the rendered references; bibliography styles govern presentation. Do not infer validity from normalization or rewrite an unrecognized DOI string.
- Offer removal of unused entries and selected private fields such as local file paths or personal notes from the submission copy. Show exclusions and their reasons; custom fields must not be discarded merely because they are unfamiliar.
- Require review for merges, citation-key renaming, metadata replacement, field deletion, and capitalization changes. Key changes must update every supported citation and bibliography relationship atomically; unresolved references prevent the operation.
- Treat a URL as redundant only under an explicit policy. A dataset, software release, supplementary resource, and DOI landing page may serve different purposes.
- Produce a bibliography diff, rerun the appropriate backend, and verify citations and rendered references. Keep the full original bibliography intact outside the prepared copy.

### Optional metadata enrichment

Query external metadata only when enabled. Send only the reference fields needed for lookup, show the provider and returned candidate, and distinguish DOI syntax, resolution, and agreement with the cited work. Matching must consider authors, title, year, venue, and publication version. A successful DOI resolution alone does not establish a correct citation.

Offer missing DOI candidates, conflicting metadata, and possible published versions for review. Never invent metadata or replace a preprint automatically. Use bounded requests, caching, and rate-limit handling; provider failure, no match, and ambiguous results must remain distinct. Crossref is an initial provider candidate because its API exposes DOI records and metadata queries. [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)

Later provider adapters may cover OpenAlex or DBLP, plus correction/retraction notices where an authoritative record exists. Preserve disagreements and show the source; no provider is assumed to supply an infallible canonical record. Optional DOI, reference-URL, and replication-link checks must distinguish a confirmed missing resource from redirects, authentication requirements, rate limits, and temporary failures. Link resolution does not verify artifact completeness or reproducibility. Requests must avoid private-network destinations, credentials, and unrelated tracking URLs.

## Source formatting

Integrate `tex-fmt` through a replaceable adapter. It provides check and print modes, configuration, and formatting exclusions, but does not perform semantic parsing or syntax correction. The preparation tool must therefore verify its output independently. [tex-fmt documentation](https://github.com/WGUNDERWOOD/tex-fmt)

- Provide formatting checks, previewable diffs, and application to the staging copy. Expose indentation, wrapping, and blank-line policies; an optional one-sentence-per-line mode can follow later.
- Honor protected regions and exclude verbatim, listings, generated files, and third-party class/style files by default. Permit additional project-specific exclusions.
- Preserve significant comments, escaped percent signs, macro token boundaries, and whitespace-sensitive constructs. Do not apply a blanket trailing-whitespace or comment-removal rule across arbitrary TeX.
- Keep prose wording, equations, citations, and macro definitions unchanged in meaning. Reordering packages or changing punctuation is outside formatting.
- Use one configured owner for `.bib` formatting so LaTeX and bibliography formatters do not repeatedly undo each other's output.
- Resolve formatter settings explicitly, without inheriting undeclared machine-specific defaults. Formatting must be idempotent for the supported input: a second run produces no additional diff.

## Cleanup and submission privacy

- Exclude rebuildable auxiliary files, editor backups, operating-system metadata, caches, and version-control directories from the bundle, unless the selected build/bibliography strategy requires a particular generated file.
- Offer comment and inactive-draft removal only for supported constructs. Preserve token separation and newline suppression, licensing notices that must be retained, formatter directives until formatting is complete, and required template directives. Uncertain conditional code must remain unchanged and be reported.
- Detect accidental private material in source comments, bibliography notes and paths, unused drafts, logs, and PDF metadata. Findings must identify the location without unnecessarily repeating sensitive content in reports.
- Offer a local scan for likely credentials or private tokens with redacted findings and explicit false-positive handling. Selected metadata sanitization may run automatically in the prepared copy and rebuilt PDF; do not erase authorship or rights information without the corresponding user-selected operation.
- For anonymous review, inspect configured author names and affiliations, acknowledgments, identifying links, filenames, source comments, and PDF metadata. Report limitations for images, indirect clues, and unsupported formats. Do not claim guaranteed anonymity or rewrite self-citations automatically.
- For final submission, check required author and publication metadata and lingering anonymous/review options. Rights statements, affiliations, and publication identifiers must come from the author or supplied venue information.
- List each excluded file and transformation. Preserve originals, required source dependencies, and explicitly included supplementary material.

## PDF and figure checks

### First release checks

Inspect the final PDF for readability and extractable text, page count and page boxes, expected page dimensions, font embedding and font types, encryption, annotations, attachments, forms, active content/media, and configured metadata requirements. Whether a font type or PDF feature is forbidden comes from user settings. Crop/registration marks and scan-only pages can be flagged where detectable; do not classify a legitimate image-only page as a scanned manuscript. Font checks must also inspect included PDF figures when possible and distinguish confirmed attribution from a suspected source.

For raster images, calculate effective resolution at each rendered placement, including scale and rotation; report both axes when scaling is unequal. An image used at different sizes can have different findings. If dimensions cannot be established, report the measurement as unavailable. Do not mistake vector artwork for low-resolution raster content or treat every PDF figure as purely vector.

### Extended geometry checks

Compare detected content against user-defined page, margin, and column regions, accounting for headers, footers, page rotation, crop boxes, and legitimate spans across columns. Flag possible overflowing equations, tables, figures, or captions; unusually small text; near-empty pages; and overlap or clipping candidates. Additional advisory checks may identify rasterized tables, excessive figure borders, and thin lines at the rendered scale. Artwork categories such as line art or photographs may use different user-supplied thresholds; inferred categories must remain visible and overridable.

Each finding must state the page, region, measured value and unit, applicable threshold, and evidence quality. PDF points and TeX points must be distinguished or converted explicitly. Provide a highlighted page crop and a source link when mapping is supported; do not fabricate a figure number or source line.

Text size inside vector figures may be measurable after placement transforms. Text converted to outlines or embedded in raster images requires estimation and must not be reported as an exact measurement. Unknown figure semantics, decorative overlap, and inferred column boundaries remain advisory unless validated evidence supports a blocking rule.

Image compression, downsampling, cropping, conversion, and figure externalization are optional reviewed transformations. They must preserve required resolution, fonts, color, transparency, and appearance according to the selected policy. Never enable lossy compression implicitly to satisfy a size limit.

## Accessibility

Check the presence and nonempty content of user-required figure descriptions, including `\Description` where the template uses it. Flag obvious placeholders separately from qualitative judgments about description quality. Later checks may inspect document language, tagging, reading order, links, table structure, and distinctions conveyed only by color. Offer grayscale figure previews and inspect contrast only where foreground/background colors can be measured reliably; neither operation proves perceptual separability or complete accessibility.

Source description presence does not establish PDF accessibility. Checks must state whether they inspect source markup, PDF structure, or visual evidence. Full accessibility conformance and the adequacy of descriptions require human review; optional language-model advice must be clearly labeled and require explicit data-sharing consent.

## User supplied submission settings

Keep the maintenance surface to a documented configuration schema and generic check implementations. The application must ship no publisher/venue rule catalog, inherited compliance profiles, annual page-limit tables, remote policy synchronization, or automatic interpretation of a Guide for Authors. A reusable project settings file may contain constraints supplied by its author; naming that file after a journal does not make it a maintained product profile.

The configuration must support:

- Selecting and ignoring checks by stable code or family in `[checks]`, with unknown selectors rejected. Required input-safety, transformation and bundle-verification guards remain enforced regardless of lint selection.
- Document roots, source/PDF comparison inputs, engine and toolchain selection, bibliography strategy, and explicitly included generated dependencies.
- Archive layout (`flat` or `preserve`), optional wrappers for preserved layouts, filenames and length/character limits, allowed file types, size limits, separate supporting deliverables, and automatic operations.
- Expected class/options and user-supplied package allow/deny lists or reference templates, without bundled publication-policy data.
- Page dimensions, total or section-specific budgets, margins, columns, font size, image resolution by artwork category, line width, and other supported numeric thresholds.
- Abstract/keyword limits, required fields or sections, description presence, identity terms to scan, selected metadata removal, and required supporting files.
- Execution budgets for CPU workers, memory, builds, PDF rendering and network requests, including a serial mode and provider-specific request limits when online lookup is enabled.

Configuration precedence must be explicit: command-line options override the selected project settings file, which overrides documented generic defaults. Discover the nearest `.latex-prep.toml`, `latex-prep.toml` or `[tool.latex-prep]` table in `pyproject.toml`; `--config` overrides discovery and `--isolated` disables it. Do not load configuration from inside an input ZIP or inherit an undeclared global configuration. Unknown keys, conflicting constraints, and unsupported required operations must produce errors instead of silently falling back. Reports must show the configuration source, settings and check selection.

When no acceptance threshold is supplied, report measurements and generic diagnostics. Do not invent a page budget, image-resolution minimum, required declaration, or submission-stage policy. A field omitted from user settings is not evidence that the corresponding publisher condition passed. Stage labels may annotate a run, but must not enable hidden rules.

Page-count settings must say whether references, appendices, and supplements count. If a boundary cannot be identified reliably, accept an explicit boundary or report the check as inconclusive. Package-specific syntax support, such as extracting `\Description`, is technical interoperability rather than a claim about when a publisher requires it.

## Common needs found in publishing guidance

The following official guidance, checked on 6 October 2026, motivates reusable capabilities. It is design evidence rather than a policy dataset that the application must keep current. Exact requirements remain the author's responsibility for the selected journal, track, and submission stage.

| Guidance and scope | Relevant observation | Generic capability |
| --- | --- | --- |
| [Elsevier LaTeX instructions](https://www.elsevier.com/researcher/author/policies-and-guidelines/latex-instructions), Editorial Manager workflow | Source, figures, and bibliography must share one folder level | Automatic flat layout and path rewriting |
| [Elsevier Editorial Manager support](https://www.elsevier.support/publishing/answer/how-to-submit-a-latex-file-in-editorial-manager) | File naming and missing style/source dependencies can prevent compilation | Configurable names, collision detection, dependency packaging, final rebuild |
| [Empirical Software Engineering guidelines](https://link.springer.com/journal/10664/submission-guidelines) | Discourages subfolders and specifies abstract/keyword requirements, declarations, bibliography, and artwork criteria | Flat layout; configurable structure/counts; statement presence; bibliography and figure checks |
| [ACM CHI 2026 production instructions](https://chi2026.acm.org/publication-ready-author-instructions/), a specific ACM conference | Specifies source organization, class settings, package constraints, and figure descriptions | Root/template inspection, supplied package lists, description checks |
| [IEEE conference PDF requirements](https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/meet-ieee-xplore-requirements/) | Checks embedded fonts and PDF structure/security | Font, PDF-object, attachment, and encryption inspection |
| [IEEE journal graphics guidance](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/create-graphics-for-your-article/resolution-and-size/) and [supplement guidance](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/prepare-supplementary-materials/) | Artwork requirements depend on image type; supplements have separate roles | Effective resolution by placement/category and separate deliverable outputs |

The current [JSS](https://www.sciencedirect.com/journal/journal-of-systems-and-software/publish/guide-for-authors), [IST](https://www.sciencedirect.com/journal/information-and-software-technology/publish/guide-for-authors), [TOSEM](https://dl.acm.org/journal/tosem/author-guidelines), and [TSE](https://www.computer.org/csdl/journal/ts) author guidance could not be retrieved sufficiently to verify their journal-specific details. No numeric limits or mandatory journal items are inferred from the supplied examples or historical mirrors.

## Change policy and verification

| Change category | Examples | Required behavior |
| --- | --- | --- |
| Automatic within an enabled policy | Folder flattening and supported path/collision renaming; disposable file exclusion; validated formatting | Operate on the copy without per-file prompts, record changes, and verify output |
| Review required | DOI additions, bibliography merges, uncertain comment removal, ambiguous path mappings, image compression | Present the exact patch and consequences; apply only selected changes |
| Diagnostic only | Small figure text, suspected overlap, uncertain reference identity, inadequate descriptions | Explain the evidence and a possible remedy; do not change content |

“Automatic” is conditional on supported syntax and verified preconditions. Users select supported operations through CLI options or settings for unattended runs. Routine moves and reference rewrites within an enabled flat-layout operation must not require individual approval. Changes to inputs, configuration, or proposed patches invalidate the affected review decisions and verification results.

For enabled flattening, cleanup, or formatting, compare baseline and prepared PDFs using page count, normalized extracted text, and rendered-page differences. Text extraction alone can miss equations or figures; rendering alone can miss metadata and structure. Define comparison tolerances and excluded nondeterministic metadata, and report which comparisons completed.

Unexpected text, equation, citation, or layout changes block a validated result. Expected changes, such as an approved bibliography correction, require explicit review of the affected output; patch approval must not silently accept unrelated rendering changes. If comparison is unavailable or the baseline is unstable across builds, preservation remains inconclusive. These checks provide evidence of preservation, not a proof of semantic equivalence for arbitrary TeX.

Support an optional supplied submission PDF as a distinct comparison input in the first release. Compare it with the unmodified source build to detect pre-existing source/PDF mismatches, and with the final archive build to show the actual deliverable. Report these comparisons separately from transformation preservation. Identify changed pages and measured/textual differences; never equate matching extracted text with identical figures. If the user selects source/submission-PDF consistency as required, a mismatch or unavailable comparison blocks that requirement even when cleanup itself preserved the baseline.

## Packaging and delivery

- Produce the prepared source folder and a ZIP satisfying the selected operations and settings. Include or omit the PDF, `.bib`, `.bbl`, local styles, and generated dependencies according to the chosen bibliography/build strategy and dependency evidence. Preserve a supplied `.bbl` when required; do not inline or regenerate it under an incompatible backend. Report unverified toolchain compatibility.
- Verify flat layout at the ZIP-entry level, including the absence of wrapper directories. For preserved layouts or separately declared deliverables, use the explicit requested folder structure. Return title pages, highlights, response letters, or supplements separately when requested; check presence without manufacturing content or uploading it.
- Never overwrite the input or an existing output silently. Write incomplete work to a separate job location and publish the final local output only after the relevant stages finish.
- Verify each delivered source package by extracting it afresh and rebuilding its assigned documents without access to the original tree, other output packages, user TeX additions, or undeclared caches. Permit only the declared system toolchain and explicitly packaged generated dependencies. A source document packaged separately must not be required to rebuild from the main manuscript ZIP.
- Compare the rebuilt archive output with the approved staged output. Recheck final PDF properties and archive limits; a PDF that passed before the final transformation is insufficient.
- Use stable ordering, normalized archive timestamps, and documented compression settings. Identical prepared file contents and packaging settings must produce the same ZIP bytes. Bit-identical PDFs across different TeX installations are not promised.
- Allow an explicitly requested diagnostic export when preparation is blocked. Such an export must be visibly marked unverified and must not be reported as submission-ready.

## Reports and user interface

Every check must expose a stable descriptive rule name, execution status, severity, explanation, supporting evidence, location when available, and a suggested action. Separate **passed**, **failed**, **inconclusive**, **skipped**, and **not applicable**; missing tools, unsupported syntax, and lookup failures must not become passes.

Keep evidence type independent of severity: direct observations (such as a nonembedded font), derived measurements (such as effective resolution), heuristic findings (such as possible clipping), and semantic advice (such as declaration adequacy) require distinct labels. Do not assign unsupported star ratings or numeric confidence scores. A reliable measurement becomes a violation only when an applicable supplied constraint is exceeded. Semantic advice cannot become a deterministic publication violation.

The first release must provide a concise terminal summary and structured JSON with detailed findings, changes, exclusions, tool versions, effective settings, and build results. Later HTML reports must include marked PDF regions and before/after views. Escape project-controlled content in reports and keep them usable offline. Machine-readable output must not be mixed with progress messages.

Use Click for the Python CLI and Rich for terminal rendering. Show blocking issues first with their code, descriptive name, file/line or PDF page where known, observed problem, and suggested next step. Provide a compact plain-text format suitable for passing to an LLM agent, with one finding per line and no terminal styling, alongside full JSON evidence. Preserve incomplete-check states and avoid implying that a suggested edit has been applied.

Assign explicit, stable codes to checks, paired with descriptive names. Publish a discoverable catalogue through `rules` and `rule CODE`. Every registered code must identify at least one unit test that exercises the real checker on a meaningful fixture and asserts the intended behavior and code. Include valid, violating, and uncertain cases where applicable; a registry-membership assertion alone does not satisfy this requirement.

Support scoped suppressions with a reason. Show suppressed or waived findings and any departure from the selected constraints; never rewrite them as successful checks. Nonwaivable failures include invalid archives, failed required builds, and unverified final-bundle reconstruction.

For the complete preparation workflow, outcomes must distinguish:

| Outcome | Meaning |
| --- | --- |
| Passed selected checks | Every required check completed successfully, all required source packages rebuilt, and no blocking finding or unapproved output change remains |
| Passed with advisories | The same required conditions hold, with nonblocking findings shown explicitly |
| Completed with accepted exceptions | The user accepted permitted deviations from the selected constraints; these remain visible |
| Blocked or incomplete | A required condition failed, could not be checked, or still requires a decision |

Standalone check commands must report their narrower scope. A successful bibliography or PDF inspection does not imply that the project or submission bundle has passed preparation.

No outcome guarantees publisher acceptance. Define stable exit codes distinguishing a clean pass, advisories, accepted exceptions, blocking findings, and execution/configuration failure. An explicit CI policy may allow advisories; it must not conceal incomplete required checks.

The proposed command groups are `inspect`, `check`, `prepare`, `bib`, `fmt`, and `report`. A typical interface could be:

```sh
latex-prep check paper.zip --main main.tex
latex-prep prepare paper.zip --main main.tex --layout flat --format --output prepared-paper
latex-prep prepare paper/ --config submission.yaml --output prepared-paper
latex-prep check paper.zip --main main.tex --reference-pdf submitted.pdf
latex-prep bib check paper/
latex-prep fmt paper/ --check
```

`latex-prep` is a placeholder executable name. `check` leaves the input unchanged and may build a temporary copy; `prepare` runs the complete workflow. The command syntax is illustrative, not a finalized API.

Provide `--dry-run` to show the plan without producing a submission bundle and `--non-interactive` to guarantee that a run cannot wait for terminal input. Neither option bypasses ambiguous mappings or mandatory verification. Report whether compilation was needed during planning.

## Later web interface

A future web interface must reuse the same configuration, planning, transformation, checking, and reporting components as the CLI. The interface should let a user upload a ZIP, select operations or a settings file, view progress, review source/PDF differences, and download the prepared archive and reports. Browser folder upload may be added where supported; ZIP upload is the baseline. Do not duplicate validation logic or construct shell command strings from form fields.

Expose structured job inputs, progress events, cancellation, results, and artifacts independently of the CLI renderer. Do not implement a web server or hosted service in the first release. Local serving versus remote hosting remains a later deployment choice. Before accepting remote uploads, require per-job isolation, upload/resource quotas, access control, and explicit file retention and deletion behavior; use the same restricted build execution model.

## Implementation boundaries and quality requirements

Reuse established tools through adapters with explicit options and supported versions. `latexmk` is a candidate for build orchestration, `tex-fmt` for source formatting, and `bibtex-tidy` for bibliography formatting and duplicate suggestions. `arxiv-latex-cleaner` is a candidate for selected cleanup operations, subject to the same preservation checks; its broad deletion and compression options must not bypass the tool's change policy. [latexmk](https://ctan.org/pkg/latexmk), [bibtex-tidy](https://github.com/FlamingTempura/bibtex-tidy), [arxiv-latex-cleaner](https://github.com/google-research/arxiv-latex-cleaner)

Python is the selected implementation language. The initial implementation uses external Poppler tools for PDF inspection and comparison; deeper PDF analysis remains an adapter choice. The core needs project discovery, checks, transformations, configuration resolution, build control, verification, packaging, and reporting as testable components. A third-party plugin ecosystem and a new general TeX parser are outside the initial scope. Reusing a cleanup tool with a publisher name must not introduce a publisher-policy maintenance obligation. See the [implementation status](README.md) for the supported subset; this requirements document continues to describe the full planned release.

- Local analysis must send no manuscript content off-device. Network operations and external AI services require separate enablement, declared payloads, and bounded execution.
- Failures in one optional checker must not erase completed results. A missing required checker prevents a pass; an optional skipped checker remains visible.
- Set and report resource limits for archives, builds, PDF parsing, rendering, and network calls. Show the active stage, elapsed time, and cancellation support. Stop child processes and preserve available diagnostics when cancelled.
- Cache results only when the relevant input, settings, and tool versions remain compatible. Final archive verification must use a fresh extraction and must not reuse project build outputs from an earlier stage.
- Measure performance on a declared test corpus before setting latency promises. Record stage timings so optimization targets observed bottlenecks.
- Preserve source encodings where supported; report unsupported encodings and make conversion an explicit change. Validate Unicode paths, spaces, and cross-platform filename behavior.

## Parallel execution requirements

The first release must use a bounded dependency scheduler in the Job Runner, following the [parallel execution design](architecture.md#parallel-execution). The CLI and a later web interface must use the same scheduling and result semantics.

- Run independent source and bibliography analysis alongside baseline compilation on immutable inputs. Start PDF-dependent checks only after their required build succeeds. Reconcile compilation-derived dependencies and citation usage before pruning or reference rewrites.
- Permit concurrent independent root builds and final-package verifications only in separate working/output directories. Respect explicit cross-document dependencies and preserve the ordered passes within each build. Every final source package must still rebuild independently from its own contents.
- Keep one coordinator responsible for each staging tree. Parallel workers may compute independent formatting patches or temporary outputs, but cross-file transformations and their application must be ordered. No check, build or packaging operation may consume partially applied changes.
- Enforce shared CPU/memory budgets and bounded tool concurrency, accounting for nested subprocess workers. Optional network lookups must also respect per-provider request limits. A future multi-job service must enforce service-wide limits and fair allocation rather than multiplying the machine budget for every job.
- Reuse immutable parsed inputs where practical. Do not share unsafe mutable library state across workers, and do not reuse results after their relevant source, configuration or toolchain inputs change.
- Collect structured results centrally and produce stable finding order, rename decisions and prepared contents regardless of task completion order. Live progress and elapsed times may vary. For identical prepared file contents and packaging settings, ZIP bytes must remain deterministic.
- Propagate failures to tasks requiring the failed output without discarding independent results or preventing diagnostic collection. Distinguish required failures from optional unavailable checks. Cancellation must stop queued tasks and relevant child processes, release capacity, preserve diagnostics and prevent release of incomplete packages; review waits must not reserve execution slots.
- Offer serial execution and configurable concurrency limits. Measure end-to-end duration, stage timings and peak resource use on representative projects before choosing performance defaults or claiming speedups.

## Acceptance criteria

Each implemented rule or transformation must have valid, violating, and relevant ambiguous fixtures. The following are release gates, with deeper visual cases applying when those features ship.

| Scenario | Required observable result |
| --- | --- |
| Folder and equivalent ZIP | Equivalent prepared contents and findings; original inputs remain unchanged |
| Nested project with literal source, image, and bibliography paths | Flat folder and ZIP root; supported references updated automatically; rebuilt PDFs preserve the baseline |
| Duplicate figure basenames, case/Unicode collisions, or sanitized-name collisions | Distinct deterministic names, no overwrite, and correct image selected at every use |
| Extensionless graphics, search paths, or an unsupported path-generating macro | Preserve original resolution for supported forms; unresolved cases block flat-layout completion with exact locations |
| Class/style identity, generated bibliography naming, or multiple roots | Preserve coupled names and build contracts; incompatibilities stop the transformation |
| Supplementary dataset with an internal directory structure | Separately declared delivery preserves its structure; no silent flattening or omission |
| Main paper and supplementary document delivered in separate source packages | Each rebuilds from its own exact extracted package and included dependencies; neither relies on the other package |
| Flat layout enabled in a noninteractive run | Supported moves and rewrites complete without per-file prompts; ambiguity returns an actionable failure |
| Ambiguous roots or supplementary documents | Explicit target selection; all selected roots participate in dependency and build checks |
| Unsafe archive or untrusted build configuration | No access outside permitted locations and no implicit script execution |
| Missing file, failed engine, stale PDF, or nonconverging references | Clear failure/incomplete state; no validated bundle |
| Dynamic file dependency or citation macro | No speculative pruning; uncertainty is reported |
| Bibliography inheritance, string macros, and `\nocite{*}` | Required entries and fields survive cleanup; rendered citations remain correct |
| Duplicate-key conflict or ambiguous DOI candidate | Reviewable evidence and patch; no silent merge or metadata replacement |
| Formatting and comment cleanup around verbatim, escaped `%`, and meaningful line endings | Supported transformations preserve output; unsupported constructs remain unchanged; second run is idempotent |
| Missing DOI service or optional local tool | Other checks continue; unavailable checks do not become passes |
| Nonembedded font or raster image scaled differently in two places | Correct font finding and distinct effective-resolution results |
| Valid column-spanning float versus real overflow | Legitimate span is allowed; overflow has a correctly located measurement and stated tolerance |
| Configured figure description absent or empty | Finding against the supplied setting without claiming a complete accessibility audit |
| No page/abstract/figure threshold configured | Measurement and applicability status; no invented publisher limit or compliance pass |
| Unknown settings key or contradictory constraints | Configuration error with the affected setting named |
| Required declaration, author field, or supporting file absent | Presence finding without fabricated content or semantic assurance |
| Supplied PDF differs from an otherwise valid source build | Separate source/PDF mismatch finding and changed-page evidence |
| Unexpected PDF change after cleanup | Before/after evidence and blocked validation until resolved |
| Dependency accidentally omitted from the ZIP | Fresh extraction/build fails and prevents a validated result |
| Repeated packaging of identical staged contents | Identical ZIP bytes under the same packaging settings |
| Suppression, accepted exception, or unavailable required check | Report and exit status preserve the actual outcome |
| Serial and concurrent execution of the same controlled fixture | Equivalent findings, decisions and prepared sources, apart from timing/progress order; identical ZIP bytes for fixed prepared inputs |
| Concurrent builds with the same job name in different documents | No shared auxiliary files, overwritten outputs or cross-build contamination |
| Flattening or citation changes overlapping formatting work | Ordered application, correct final references, and no reader sees a partially transformed tree |
| CPU/memory limits and tools with internal parallelism | Admission stays within configured budgets; unsafe resource requests are rejected or blocked explicitly |
| A build fails while independent source checks run | Dependent PDF checks do not run; independent findings remain available; no false successful outcome |
| Cancellation with queued work and active child processes | Children stop, queued work stays unstarted, capacity is released, and incomplete output is not released |
| Slow or rate-limited metadata provider | Provider limits are respected and local checks continue; lookup status remains explicit |
| Several web jobs when that interface ships | Service-wide budgets and fair allocation hold; workspaces and cancellation remain isolated |

Before releasing automatic transformations, run them over representative single- and multi-file projects, supported engines, BibTeX and Biber, custom macros, and main/supplement combinations. Measure false positives for heuristic visual checks on manually labeled fixtures before enabling them as blockers. User acceptance should include preparing an actual submission and manually inspecting its final PDF and ZIP.

## Delivery sequence and remaining decisions

**First release:** CLI-based folder/ZIP preparation; bounded task scheduling; automatic flat hierarchy and path rewriting; controlled builds; basic source, structure, metadata, and PDF checks; local bibliography cleanup; `tex-fmt`; conservative file/comment cleanup; user-supplied settings; baseline and supplied-PDF comparisons; final archive rebuild; and terminal/JSON reports.

**Extended checks:** deeper structural linting, user-supplied template comparisons, margin/column analysis, figure typography and line width, accessibility/grayscale checks, annotated HTML, and CI annotations.

**Optional extensions:** online bibliography enrichment and artifact-link checks, explicitly selected build environments, reviewed single-file source merging or bibliography inlining, image optimization, standalone PDF comparisons, a web interface, and opt-in language or description advice. Maintained publisher rules, manuscript rewriting, scientific fact checking, venue upload, and guaranteed certification are outside scope.

Before implementation, settle the distribution model, supported toolchain range, concrete flattening syntax support, comparison tolerances, configuration format, and initial automatic-cleanup allowlist. A representative nested LaTeX project should serve as the first end-to-end acceptance case. CLI execution, flat-layout support, and the publisher-independent design are settled requirements; local versus hosted web deployment can be decided later.
