# Preparation operations and review controls

Every transformation writes a separate staging copy. `prepare --dry-run` previews
source changes after building the baseline; final input selection, flattening and
the bundle are not verified. `prepare --output NEW_DIRECTORY`
also verifies the prepared and archive-rebuilt PDFs before release. Explicit edit
values, key mappings and exception reasons in configuration constitute the user's
reviewed choices. No remote metadata response is applied automatically.

The runnable {download}`preparation settings <../examples/preparation.toml>` select conservative
bibliography formatting and private-comment removal. Other operations below are
independent opt-ins. CLI options override TOML; unknown keys and invalid types fail.

## Bibliography and source transformations

```toml
layout = "flat"
bibliography_backend = "bibtex" # auto, bibtex, or biber
format = true
normalize_doi = true

[bibliography_transform]
format_entries = true
remove_fields = ["file", "annotation"]
cited_only = true
order_entries = false
key_renames = { oldKey = "newKey" }
merge_keys = { duplicateKey = "retainedKey" }

[[bibliography_transform.field_edits]]
key = "retainedKey"
field = "title"
expected = "A GPU study"
replacement = "A {GPU} study"

[source_transforms]
comment_policy = "private" # retain, private, or all
merge_inputs = false
# inline_bibliography = "main.bbl"
filename_overrides = { "figures/chart.pdf" = "result-chart.pdf" }

[formatting_options]
exclude = ["generated/*.tex"]
blank_lines = "preserve" # formatter, preserve, or collapse
```

Bibliography edits preserve values, macros, brace protection, comments and unknown
fields unless the operation explicitly selects them. Renames update supported
literal citations and bibliography relationships together. A merge keeps the
chosen target's metadata; it does not reconcile conflicting records. Cited-only
output retains supported relationship closure and `nocite` usage. Ambiguous
scope, multiple roots in the selected package, executable bibliography preambles
and unsupported key uses block destructive proposals. Entry ordering is refused
when its effect cannot be safely isolated.

Field-edit tables require exactly one before-state choice (`expected` or
`expected_absent = true`) and one after-state choice (`replacement` or
`remove = true`). This supports adding and deleting fields despite TOML having
no null value. Array rows `[key, field, expected, replacement]` remain supported
for literal replacements. Guarded field edits precede DOI-prefix normalization.

Source merging supports standalone literal `input` lines; it preserves their
tokens and removes only fully inlined files in staging. `include`, imports and
subfiles have different semantics and are not silently merged. Bibliography
inlining accepts an explicitly supplied literal BibTeX `thebibliography` file;
it does not inline arbitrary biblatex output. Comments retain newline-suppression
markers, literal environments, license blocks and formatter/template directives.
Comment removal edits only `.tex`, `.ltx` and `.latex` files; local class and
style files are copied unchanged. Formatter output must preserve protected spans
and pass a second idempotence run. A formatter-off region starts at a comment
line `% tex-fmt: off` and ends at `% tex-fmt: on`; the prefixes `fmt`,
`fledge` and the earlier `latex-prep` are accepted as well. Only `tex-fmt` itself
honors `% tex-fmt: off`; the other prefixes protect regions in Fledge's checks. An
unmatched marker makes the result inconclusive.
Filename overrides require `layout = "flat"` and retain flattening safety checks.

## Independent manuscript and supplement packages

```toml
[workflow]
baseline_runs = 2

[[workflow.documents]]
name = "paper"
source_directory = "paper"
main = "main.tex"
include = ["*.tex", "sections/*.tex", "*.bib", "figures/*.pdf"]

[[workflow.documents]]
name = "supplement"
source_directory = "supplement"
main = "supplement.tex"
engine = "lualatex"
# reference_pdf = "reviewed-supplement.pdf"
```

Each document gets its own source copy, baseline, transformed build, ZIP and
fresh archive rebuild. Selection patterns are case-sensitive project-relative
globs; each pattern must match a file and the selected root must be included.
These patterns restrict candidate inputs; they do not force every match into the
final package. Omitting `include` makes every imported file in that document's
source directory a candidate. Dependency selection retains the needed inputs;
list additional files in `[package] include` using
paths relative to that document's source directory. Those paths follow any flat
filename mapping. See [package contents](configuration.md#package-contents).
Reference PDF paths resolve relative to the settings file. Use either `main` or
`workflow.documents`, never both. All selected packages must verify before any
are released:

```text
output/
  paper/{sources/,submission.zip,manuscript.pdf,report.json}
  supplement/{sources/,submission.zip,manuscript.pdf,report.json}
  report.json
```

Independent document work uses the same CPU, memory, build and render budgets;
there is no additional unbounded worker pool. Default build concurrency remains
one. Every report identifies the document and stage for each finding. A failed
supplement blocks the entire release while retaining findings for all documents.

## Preservation and explicit exceptions

```toml
[workflow]
baseline_runs = 2 # 1–5 fresh builds, compared exactly

[workflow.comparison]
channel_tolerance = 1
max_changed_pixel_ratio = 0.0001

[[reporting.accepted_exceptions]]
code = "CMP003"
stage = "baseline versus prepared"
reason = "Reviewed the intended visual change on page 3."
page_start = 3
page_end = 3

[[reporting.suppressions]]
code = "TEX001"
path = "appendix.tex"
line_start = 20
reason = "The marker is an intentional example discussed in the manuscript."
```

Pixel tolerances affect only baseline-versus-prepared rendering, and are recorded
with the measured differences. Text, page counts and page dimensions remain
separate checks. Repeated baselines and the final archive rebuild use exact
comparison. All comparisons retain bounded 144-DPI evidence, not a semantic
equivalence guarantee.

Accepted exceptions keep the original failed result and add its explicit reason;
a successful release with such decisions reports `accepted_exceptions` (exit 2).
Suppressions annotate advisories and do not authorize errors. Incomplete evidence,
build failures, unsafe transformations and archive mismatches cannot be waived.
Unmatched or ineligible decisions are reported. A `document` field additionally
scopes a decision in multi-document jobs. New check codes are not automatically
eligible for exceptions.

## Metadata, artwork and structural evidence

```toml
[metadata_privacy]
image_identity_terms = ["Example Author"]

[[metadata_privacy.edits]]
path = "main.tex"
field = "hypersetup.pdfauthor"
expected_before = "Example Author"
replacement = ""

[structure_checks]
author_command = "author"
required_author_fields = ["affiliation", "email"]
check_table_structure = true
check_link_structure = true
require_figure_alt = true
check_structure_references = true

[[structure_checks.heading_expectations]]
page = 8
title = "Supplementary results"
number = "A"

[pdf_artwork]
grayscale_preview = true

[figure_artwork]
classify_artwork = true
classify_type3_glyphs = true
```

Metadata changes require an exact before-value in one supported literal source
declaration. The final rebuilt PDF is checked again for the selected replacements
and removed values. Authorship or rights are never erased implicitly. Image
identity checks inspect supported PNG/JPEG metadata; they do not decode raster
text or establish anonymity.

Per-author checks use explicit sequential preamble records and required command
names, rather than maintaining publisher templates. Heading checks match declared
page/title/number lines in extracted text. Tagged PDF checks inspect supported
table, link, alternate-text and parent-tree/MCID structures; they do not establish
natural reading order, description quality or PDF/UA conformance.

`pdf_artwork` applies to the final document. `figure_artwork` independently applies
to each literal input PDF figure, with its own coordinate system; document page
regions are never reused implicitly for figure files. Crop/registration marks,
whitespace, edge contact and raster-region classifications are review candidates.
Type 3 analysis examines supported glyph programs and separates image operations
from vector operations. Missing tools, unsupported transforms or partial objects
remain incomplete observations.

`pdf_artwork.contrast_samples` accepts named foreground/background `PdfRegion`
tables, a `minimum_ratio` and an optional `max_channel_spread`. The regions identify
actual rendered color patches. Nonuniform or ambiguous patches are inconclusive;
the checker does not infer what represents text or whether color carries meaning.
Coordinates use top-left PDF points in the supported uncropped/unrotated frame.

## Reports and standalone PDF inspection

To inventory actual loaded class/package options, add
`inventory_loaded_options = true` under `[build_checks]`. Check `BLD301` reads
kernel registers in a disposable instrumented build and verifies the file set
against the recorder. It includes supported forwarded options, while semantic
defaults and later package setup commands remain outside its scope. This
instrumentation is absent from original and submitted sources.

```sh
fledge pdf check paper.pdf --config settings.toml --output-format compact
fledge pdf check new.pdf --reference-pdf old.pdf --json
fledge check ./paper --config settings.toml --preview-output ./gray-pages
fledge prepare ./paper --config settings.toml --output ./prepared \
  --html-report ./report.html --diagnostics ./unverified-diagnostics.zip
fledge inspect ./paper --output-format ci
```

Grayscale previews are PNG pages retained outside the source ZIP. Preparation
stores them under `previews/` in the output; standalone checks require an explicit
new preview directory. Preview export and bundle publication share a rollback
barrier. HTML is offline and escaped, with source diffs, recorded region diagrams
and local PDF page links. It does not embed annotated PDF page crops. CI output
uses escaped GitHub Actions annotations. Diagnostics contain sanitized reports,
recorded diffs and explicit text evidence, prominently marked unverified; they
are not a submission bundle.

macOS execution uses `sandbox-exec`; Linux uses a required Bubblewrap backend
with private namespaces and read-only toolchain inputs. Backend unavailability
blocks builds. Linux runtime behavior and optional qpdf/MuPDF adapters need their
own live validation on hosts where they are available. No tool is installed
automatically and no unsandboxed fallback is provided. On macOS, Biber runs from a
sealed copy that Fledge prepares for the job; see
[Biber on macOS](workflow.md#biber-on-macos).
