# Module map

Fledge is one flat package, `latexprep`, of about two hundred modules. This page assigns every
module to exactly one layer and states what it is responsible for. Look here before adding a
module, to decide where it belongs and what it may import.

```text
presentation     the command line and every output format
     |
orchestration    the job workflow: tasks, builds, staging, verification, publication
     |
checks           checks, transformations and policies
     |
adapters         tool execution, PDF tools, builds, HTTP, project import and archives
     |
domain           findings, report, rules, options, readers and helpers
```

A module imports from its own layer and from the layers below it, never from a layer above.
Only the presentation layer imports `click` or `rich`. The import graph has no cycles. These
rules are checked by `tests/test_module_boundaries.py`, which reads the assignment from
`tests/module_layers.py` and fails when a module is missing from it, listed twice, imports
upward, or disagrees with the tables below. Edit the mapping and this page together; a new
module needs one line in each.

Module docstrings state each responsibility in more detail. The [development notes](development.md)
describe how the modules work together.

## Domain and support

The vocabulary and the pure logic everything else shares: findings, the report, errors, the
catalogue of check codes and its data-only `*_rules.py` modules, settings choices and option
schemas, the bounded TeX and BibTeX readers, the literal source graph, text, path and file
helpers, and the cancellation and resource-budget primitives. None starts a process or opens
a network connection, and each imports only other modules of this layer. (79 modules)

| Module | Responsibility |
| --- | --- |
| `__init__` | Package marker: the version number and the silent library logger |
| `analysed_source` | One project's literal source analysis, built once and shared by the checks that read it |
| `bibliography_check_rules` | Data-only catalogue entries for configurable offline bibliography checks |
| `bibliography_options` | Settings of the offline bibliography checks, validated when they are constructed |
| `bibliography_transform_rules` | Data-only catalogue entries for explicit, atomic bibliography transformations |
| `bibtex_layout` | Where the pieces of a parsed .bib entry sit in its text, and edits that cut them out |
| `bibtex_names` | Family names in a BibTeX `author` field, read with BibTeX's own name syntax |
| `bibtex_syntax` | Bounded BibTeX syntax reader shared by bibliography checks, transformations and lookups |
| `build_check_rules` | Data-only catalogue for checks over build and recorder evidence |
| `cancellation` | Cooperative cancellation helpers: owned workers that finish before a cancellation propagates |
| `check_selection` | Validated public check selectors, independent of checker configuration |
| `citation_graph` | Literal citations, bibliography declarations and styles of one selected manuscript |
| `comment_markers` | Words that mark unfinished work or a private note in manuscript text and comments |
| `core_rules` | Data-only catalogue for the original source, packaging, build, PDF and comparison checks |
| `defects` | Telling expected failures from defects, and describing a defect without any paths |
| `documentation_links` | Where the hosted documentation lives, for messages that send a researcher to it |
| `errors` | Expected failures that carry an actionable message, and the kinds callers tell apart |
| `execution_records` | Typed records of the `Report.execution` entries that jobs write and renderers show |
| `expanded_document` | The selected literal source graph read as one expanded document |
| `file_safety` | Bounded, link-safe file helpers shared by tool execution, builds and packaging |
| `file_scan` | Bounded scans of supplied project files: an inventory and a byte budget for reading them |
| `finding_text` | Wording and bookkeeping shared by the checks that build findings |
| `finding_weight` | How hard a check hits when it does not pass: one mapping from outcome to severity |
| `findings` | The `Finding`: one check's result, and the constructors for findings that no check produces |
| `limits` | Unit constants and the limits that more than one module shares |
| `literal_text` | Literal TeX text without expansion: shared command tables and plain-text reading |
| `loaded_options_rules` | Data-only catalogue for actual post-document kernel option evidence |
| `manuscript_check_options` | The `[manuscript_checks]` settings: which opt-in manuscript policies run |
| `manuscript_check_rules` | Data-only catalogue for explicitly selected manuscript structure checks |
| `manuscript_vocabulary` | The command and environment vocabularies the opt-in manuscript policies share |
| `measurement_findings` | Findings for configured PDF measurements: three-state outcomes that never pass by omission |
| `metadata_privacy` | Options of the metadata privacy checks: selected source edits and identity terms |
| `metadata_privacy_rules` | Data-only privacy entries for selected metadata edits and image metadata scans |
| `online_check_rules` | Data-only catalogue entries for explicitly selected remote reference checks |
| `online_options` | Settings of the online checks, and the vocabulary that every online module shares |
| `option_config` | Strict TOML conversion for grouped, immutable checker options, and the checks they share |
| `package_headers` | Literal release metadata of the class and package files a build loaded |
| `path_safety` | Relative-path validation and containment checks for paths from settings, archives and tools |
| `pdf_artwork_options` | Artwork policy options and the rules they switch on (no measurement code) |
| `pdf_artwork_rules` | Data-only catalogue for explicit artwork measurements and review candidates |
| `pdf_check_options` | The option schema of the configured PDF measurements, and which checks it enables |
| `pdf_check_rules` | Data-only catalogue for optional PDF measurements and object policies |
| `pdf_geometry` | Page geometry, colour and render-size arithmetic shared by the PDF measurements |
| `pdf_previews` | Optional, bounded PNG previews kept outside serializable job reports |
| `pdf_rule_names` | The names of the `pdf.*` rules reported for the manuscript PDF and its input figures |
| `pdf_tool_output` | Parsers for the text and XML that Poppler and MuPDF print about a PDF |
| `png_support` | PNG framing shared by the encoder, the validator and the two readers |
| `project_files` | The files of a project as the source graph inventories them, and how their names compare |
| `project_names` | Names inside a project: normalization, collisions, portability and excluded metadata |
| `raster_pixels` | Bounded PNM raster reading and pixel analysis in C-speed standard-library byte operations |
| `redaction` | Bounded credential redaction for complete, JSON/asdict-shaped report trees |
| `report` | The `Report` a job produces, with its outcome, changes and sanitized serialization |
| `reporting_rules` | Data-only entries for explicit review and diagnostic reporting safeguards |
| `resource_budget` | Atomic CPU, memory and tool-class reservations shared by every expensive job operation |
| `rule_definition` | The shape of one catalogued check; data-only `*_rules.py` modules build these |
| `rules` | Stable public check codes, descriptions and the behavioural tests associated with them |
| `runtime_rules` | Data-only catalogue for explicit build backend verification |
| `setting_choices` | Closed sets of values that settings accept |
| `source_command_checks` | Judging the commands of one parsed source: which ones the literal subset cannot follow |
| `source_file_commands` | Which LaTeX commands read a project file, and how each names its file |
| `source_findings` | Where the source graph records what it could not establish, and the wording it shares |
| `source_graph` | Literal dependency graph of a LaTeX project, built on the bounded lexical subset |
| `source_graph_rules` | Data-only catalogue for the literal source graph |
| `source_loading` | Reading one project source and lexing it, within the byte and command budgets |
| `source_resolution` | Turning the file name in a command into the project file TeX would read |
| `source_transform_rules` | Data-only diagnostic catalogue for explicitly selected source preparation |
| `structure_rules` | Data-only catalogue for explicit author and PDF structure checks |
| `submission_check_rules` | Data-only catalogue entries for configurable local submission checks |
| `submission_options` | The `[submission_checks]` settings: what to scan a submission bundle for, and how |
| `submission_readiness_rules` | Data-only catalogue for cheap submission-readiness advisories and their opt-in policies |
| `tex_commands` | TeX source vocabularies: file suffixes, math markers and command families |
| `tex_comments` | Comments of a TeX source with their line numbers, and the private notes among them |
| `tex_lexer` | Bounded lexical subset of LaTeX shared by source inspection, checks and transformations |
| `tex_protected_regions` | Regions of TeX source that a tool must leave exactly as written |
| `text_edits` | Offset-based text edits, byte-order-mark restoration and unified diffs of proposals |
| `text_safety` | Making untrusted text harmless for one output channel |
| `uncertainty_reasons` | Why a literal declaration may not take effect where it is written |
| `workflow_options` | Options for packaging several documents and for comparing PDFs, independent of any publisher |
| `workflow_rules` | Data-only catalogue for document packages and repeat builds |

## Infrastructure adapters

The boundary to the outside world: restricted tool execution and its sandboxes, the PDF tool
sessions, the build with `latexmk` and its evidence, the HTTP client for online lookups, project
import and the submission archive, and settings discovery. They import the domain layer and each
other, never a check, the job workflow or an output format. (34 modules)

| Module | Responsibility |
| --- | --- |
| `biber_toolchain` | A sealed single-architecture Biber that macOS Seatbelt may execute |
| `bibliography_controls` | Which bibliography backend a build's fresh control files require |
| `build_bibliography` | Bibliography backends in one build: when to prepare Biber, and what the controls require |
| `build_dependencies` | Bounded fresh build evidence: the recorder trace and latexmk's rule database |
| `build_log` | Findings from the bounded final-pass log of a LaTeX build |
| `build_recorder` | What the TeX recorder shows one build read, judged for build checks |
| `build_result` | What one isolated build returns: its findings, evidence and the report entries |
| `build_workspace` | The disposable workspace of one build: a bounded copy of the source and an output folder |
| `command_runner` | The port that checks use to run external tools, and the values it exchanges |
| `config_discovery` | Finding and reading the settings file of a job |
| `crossref` | The Crossref REST API as the online checks use it: request URLs and typed replies |
| `formatting` | Explicit tex-fmt adapter; computes patches without modifying the input |
| `import_budget` | The limits that bound a project import, and the running count of what an import has used |
| `latexmk_build` | Isolated latexmk builds of one main file, with fresh and bounded build evidence |
| `latexmk_command` | The latexmk command line of one build and the fixed Perl configuration it carries |
| `loaded_options` | Bounded post-document LaTeX kernel option evidence from disposable builds |
| `online_http_session` | Bounded requests to public hosts: connection, pacing, budget, redirects and caching |
| `online_http_wire` | The HTTP/1.1 bytes of one request and its bounded response |
| `pdf_qpdf_graph` | Read access to a PDF's objects through qpdf's JSON version 2 output |
| `pdf_tools` | Running PDF tools: argument builders, frozen inputs, sessions and measurements |
| `pinned_dns` | Resolving a host name to public addresses without letting a slow resolver stall a job |
| `presets` | Opt-in example settings files shipped as package data |
| `process_control` | Starting and stopping child processes so that none outlives a cancelled launcher |
| `process_table` | How the platform lists processes, and which registered command each one belongs to |
| `project` | Copy a project folder or ZIP archive into a new, bounded, link-free snapshot |
| `project_archive` | Write a staged project folder as a reproducible submission ZIP archive |
| `public_url` | Which URLs and network addresses an online check may request, and how they are shown |
| `resource_monitor` | Process, memory and storage accounting for restricted tool commands |
| `runtime` | Fail-closed local tool execution: the sandboxed `ToolRunner` |
| `sandbox` | Restricted execution backends: macOS Seatbelt and Linux Bubblewrap |
| `tool_launch` | What every tool command starts with: resource-limit wrapper, environment and refusal check |
| `toolchain` | Locate installed tools and the system and distribution resources they may read |
| `toolchain_help` | Installation guidance when the TeX toolchain a build needs is missing |
| `zip_validation` | Checks that make a ZIP archive safe to extract, before and while its entries are read |

## Checks, transforms and policies

The checks and the transformations: bibliography, manuscript, build, PDF, online, submission and
source checks, the planners that edit a staged copy (source and bibliography transformations, flat
layout, cleanup, metadata edits), and the two policies that decide which checks run and which
findings a review accepts. They import the domain and adapter layers, never the job workflow or
an output format. (61 modules)

| Module | Responsibility |
| --- | --- |
| `author_records` | Explicit, user-configured author-record contracts in the literal preamble |
| `bbl_checks` | Shipped generated bibliography (.bbl): presence, citation coverage and biblatex format |
| `bibliography_checks` | Offline citation coverage and field checks over a manuscript's declared bibliography |
| `bibliography_duplicates` | Search for entries that probably describe the same work |
| `bibliography_edits` | Operations of a bibliography proposal that need only the .bib files |
| `bibliography_field_checks` | Checks of the fields inside each bibliography entry |
| `bibliography_files` | Finding and reading the .bib files of a project, within byte and count budgets |
| `bibliography_index` | The entries of a set of .bib documents: their keys, aliases, relationships and reach |
| `bibliography_integrity` | Offline integrity checks of the .bib files themselves |
| `bibliography_keys` | Operations of a bibliography proposal that depend on the manuscript's citations |
| `bibliography_proposal` | What a bibliography proposal is, and the helpers that its operations share |
| `bibliography_results` | How the offline bibliography checks word and order their findings |
| `bibliography_styles` | The string macros that BibTeX styles define, so that they are not reported as undefined |
| `bibliography_transform` | Explicit, atomic bibliography proposals for a supported literal source graph |
| `build_checks` | Explicit checks over complete build evidence, including transitive packages |
| `check_policy` | Maps the selected checks onto the options that activate them before dispatch |
| `figure_color_space` | The colour space of raster figures against the family a venue requires (`PDF313`) |
| `flat_layout` | Planning a flat layout: every shipped file at the top level, references rewritten to match |
| `image_metadata` | Bounded scan of PNG and JPEG metadata for configured identity terms |
| `manuscript` | Configurable constraints on the literal declarations of one selected source graph |
| `manuscript_abstract_checks` | Citations and uppercase abbreviations in the abstract (`MAN003`, `MAN004`) |
| `manuscript_checks` | Opt-in, publisher-neutral checks of literal manuscript structures |
| `manuscript_float_checks` | Captions, labels, references and descriptions of figures and tables (`MAN011` to `MAN016`) |
| `manuscript_heading_checks` | Heading depth, empty sections and required declarations (`MAN005`, `MAN006`, `MAN010`) |
| `manuscript_layout_checks` | Layout override candidates (`MAN002`) and required or forbidden line numbers (`MAN017`) |
| `manuscript_metadata_checks` | Required literal metadata and ORCID identifiers (`MAN008`, `MAN009`), and the reader |
| `manuscript_package_checks` | The direct package inventory and the allowlist policy (`MAN001`) |
| `manuscript_policy_kernel` | What every opt-in manuscript policy shares: the reading, the outcome and the finding |
| `manuscript_reference_checks` | Hard-coded figure, table, section and equation numbers in prose (`MAN007`) |
| `manuscript_scan` | Literal declarations collected from the selected source graph, and the counts made of them |
| `metadata_source_edits` | Explicit literal metadata edits in copied TeX sources, and the final PDF recheck |
| `online_checks` | Explicit, bounded online reference checks; no rewriting or implicit networking |
| `online_comparison` | Comparing a bibliography entry with the Crossref record of its DOI |
| `online_inputs` | What an online check may send: bibliography entries and source links, bounded |
| `package_inputs` | Select project inputs for one observed build, without modifying the source tree |
| `package_inventory` | Reconcile selection evidence with observed package files and existing ZIP sizes |
| `pdf_artwork` | Opt-in advisory artwork measurements of one PDF, composed from the artwork modules |
| `pdf_artwork_drawings` | Artwork findings measured from the MuPDF trace of painted drawings |
| `pdf_artwork_rendered` | Rendered-pixel artwork measurements: figure whitespace, edge ink, previews and contrast |
| `pdf_artwork_trace` | MuPDF trace geometry: painted drawings with their placement, extent and active clip |
| `pdf_check_pipeline` | Run the configured PDF check groups together, for a source project or a lone PDF |
| `pdf_checks` | Optional, bounded PDF measurements with explicit evidence boundaries |
| `pdf_comparison` | Bounded rendered and extracted-text comparison of two PDFs, with optional previews |
| `pdf_inspection` | Bounded single-PDF inspection and explicitly configured acceptance policies |
| `pdf_object_checks` | Catalog-language, annotation and action policies read from qpdf's object data |
| `pdf_paint_checks` | Stroke-width, color-space, text-contrast and grayscale-separation measurements |
| `pdf_printer_marks` | Printer-mark candidates: short crop and registration strokes outside a trim region |
| `pdf_sparse_page_checks` | Sparse-page measurement: how much of each rendered page carries ink |
| `pdf_structure_checks` | Bounded tagged-PDF structure observations and the structure option schema |
| `pdf_structure_tree` | The tagged structure tree of a PDF, read from qpdf's object data |
| `pdf_text_geometry_checks` | Text-size, printable-bounds and overlap measurements from Poppler's text boxes |
| `pdf_type3_glyphs` | Type 3 glyph programs: do declared glyphs paint images, vector shapes, or both? |
| `project_cleanup` | Propose the debris of a staged project that a submission should not contain |
| `review_policy` | Explicit scoped review decisions: accepted exceptions and suppressed advisories |
| `source_analysis` | Inspecting a project's sources without editing them: roots, dependencies and lint |
| `source_transform` | Opt-in, copy-only source transformations for a bounded literal TeX subset |
| `submission_checks` | Bounded, local submission-policy checks; never edit sources or execute TeX |
| `submission_content_scan` | The single pass over every supplied file that finds identity terms, secrets and notes |
| `submission_readiness_checks` | Cheap, deterministic submission-readiness advisories over the literal source graph |
| `submission_scan_findings` | Findings for the matches of a submission scan, and the wording they share |
| `submission_text` | Bounded, source-only submission fields; unresolved content remains visible |

## Orchestration

The job workflow: validating a request and settings, scheduling the tasks of one document or of
several, importing, building, staging, verifying and publishing, and turning the results into a
report. It uses everything below it and knows nothing of terminals, HTML or the command line. (19 modules)

| Module | Responsibility |
| --- | --- |
| `config` | Validated settings: the `Settings` schema, layered from preset, file and command line |
| `core` | Application workflow entry point: validates a job request, dispatches its command and publishes the result |
| `coverage_hints` | Opt-in checks that a run did not perform because its settings left them off |
| `doctor` | Bounded setup diagnostics using application-owned sources and normal isolation |
| `document_builds` | The builds of one document beyond the scheduled baseline: staged rebuilds and repeats |
| `document_run` | One project's run: import, analysis, baseline, staged transformations and a verified package |
| `document_tasks` | Scheduled tasks of one document and the build and comparison adapters they share |
| `documents` | Several explicitly selected documents of one project, each checked and packaged independently |
| `job_context` | The request, settings and resources every stage of one job shares |
| `job_execution` | The resource budget, restricted runner, private workspace and deadline of one job |
| `job_report` | How a job's findings become its outcome: stage tags, blocking, check selection and review |
| `main_selection` | Choosing the main file of a project: the configured one, else the only candidate root |
| `package_verification` | The last steps of preparation: archive the sources, rebuild from the ZIP, run final checks |
| `publication` | Publish verified packages, previews and reports into new directories outside the input |
| `scheduler` | Dependency scheduling of job tasks under the shared resource budget |
| `staging` | Staging copies of an imported project: document selection, transformations and flattening |
| `standalone_checks` | Checks of one aspect without the build workflow: a PDF, the bibliography or formatting |
| `task_findings` | The findings that say how a scheduled task ended, and the contract readers rely on |
| `task_reporting` | How the tasks, builds and input selections of one document enter its report |

## Presentation

Everything a person or a script sees: the `fledge` command line and its option helpers, the
renderers for the terminal, compact, JSON, HTML and CI output, the progress display and the
diagnostic log. These are the only modules that import `click` or `rich`. (21 modules)

| Module | Responsibility |
| --- | --- |
| `__main__` | Entry point for `python -m latexprep`; runs the command line |
| `ci_annotations` | GitHub Actions workflow annotations for a report, one escaped command per line |
| `cli` | The `fledge` command line: commands, options, settings layering and exit codes |
| `command_options` | Click option helpers and failure handling shared by the command adapters |
| `compact_report` | The compact report: short plain records for scripts and coding agents |
| `diagnostic_bundle` | Explicitly unverified diagnostic ZIP exports of sanitized reports and diffs |
| `diagnostic_logging` | Opt-in diagnostic log lines on stderr (`--debug`); the library itself emits nothing |
| `doctor_command` | CLI adapter for bounded local setup diagnostics |
| `finding_view` | One display view per finding, shared by every report renderer |
| `html_report` | The offline HTML report: escaped evidence and no executable or remote resources |
| `html_report_previews` | The PDF-differences section of the HTML report: before and after page images |
| `html_report_regions` | SVG diagrams of the page regions a PDF finding reports, for the HTML report |
| `html_text` | Escaping of report values for the HTML renderers |
| `init_command` | `fledge init`: write a starter or preset fledge.toml without prompting |
| `report_files` | Saving the report files a command was asked for |
| `report_formats` | Report output formats and the one dispatch commands use to write a final report |
| `rule_catalogue_view` | Terminal views of the check catalogue: the list of checks and the explanation of one |
| `submission_text_command` | CLI adapter for source-only submission text export |
| `terminal_console` | The Rich consoles every command prints through |
| `terminal_progress` | Transient progress on a terminal, plain stages when stderr is redirected |
| `terminal_report` | The Rich terminal report, and plain rendering of it for tests and embedding |
