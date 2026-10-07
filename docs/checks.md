# Check codes and behavioral tests

Each code has a descriptive name, documented scope, suggested next step and an
explicit association with a unit test that exercises its behavior. Codes are
assigned explicitly and are not reused or derived from table order. Report JSON
retains the diagnostic's descriptive `rule` as well as its public `code`.

The catalogue contains 156 checks, including subchecks and preparation guards.
This is not a count of completed features from the requirements. Inventories and
operational or unsupported-input diagnostics can have no check code and still
affect the result. The [implementation backlog](check-backlog.md) distinguishes
implemented observations from remaining semantic and workflow gaps.

```sh
latex-prep rules
latex-prep rule MAN011
latex-prep rule PDF202 --json
latex-prep inspect ./paper --output-format compact --quiet
```

## Behaviour and configuration

| Family | Scope |
| --- | --- |
| `TEX` | Basic source diagnostics and the original literal manuscript constraints |
| `MAN` | Additional explicitly selected manuscript, author, declaration and float checks |
| `BIB` | Local bibliography structure, citation coverage and configurable metadata checks |
| `BLD` | Final build logs, configured overflow tolerance and recorder-observed package policies |
| `PRV` | Configured identity matches and optional privacy/shell-execution heuristics |
| `PKG` | Flattening contracts and optional filename, deliverable, archive and template-reference policies |
| `PDF` | Explicit PDF constraints and bounded text, object and drawing measurements |
| `NET` | Separately selected online DOI, metadata, candidate, notice and link checks |
| `CMP`, `FMT` | PDF preservation comparisons and formatting-proposal verification |
| `RPT` | Scoped review decisions and explicitly unverified diagnostic exports |

No publisher profiles or maintained policy lists are loaded. Original flat
settings remain supported in {download}`settings.toml <../examples/settings.toml>`.
Use `[checks] select` and `ignore` to filter by code or prefix. The
[configuration guide](configuration.md) explains project-file discovery,
selector precedence and always-enforced safety and preparation guards.
{download}`checks.toml <../examples/checks.toml>` demonstrates the additional typed tables:
`bibliography_checks`, `build_checks`, `manuscript_checks`, `pdf_checks`,
`submission_checks` and `online_checks`. Unknown fields and invalid types are
errors. Required fields, allowlists, identity terms, thresholds, regions and
reference templates come from the user. Inventories do not establish compliance.
The [preparation options](preparation-options.md) document transformations,
multi-document packaging, review controls and additional artwork/structure checks.

Manuscript checks follow supported literal source structures without arbitrary
macro expansion. Citation-use checks select one root by default; the explicit
bibliography `all_roots` option unions supported root graphs. Neither apparently
uncited entries nor assets outside a selected graph are deletion instructions.
Source float order is not rendered float order. Nonempty declarations and figure
descriptions do not establish adequacy, identity or accessibility.

PDF points are 1/72 inch; build overflow is measured in TeX points. Regional PDF
coordinates start at the top-left of an unrotated, uncropped page, and margins
are ordered left, top, right, bottom. Section budgets require explicit inclusive
page ranges. Text-size rounding, unmeasured text and unsupported transforms stay
visible as uncertainty. Geometric overlap, sparse-page, stroke and nominal color
findings remain advisory where the underlying observation cannot establish
intent or perceptual quality. Type 3 is a font type, not proof of bitmap glyphs.

Top-level `match_source_pdf_metadata = ["title", "author"]` derives expected PDF
properties only from supported unambiguous literal source values. Explicit PDF
expectations use `[pdf_checks.expected_metadata]`; the same field cannot use both
source matching and an explicit expectation. Template reference values in
TOML resolve relative to the configuration directory; project-side keys remain
bundle-relative. Equality with a supplied file does not establish authenticity,
age or semantic compatibility.

Online checks need an individual switch and network permission. `--online` and
`--offline` override permission on `inspect`, `check`, `prepare` and `bib check`; neither
selects a check. No online selection means no requests. Without permission,
selected online checks are skipped. DOI/URL targets and, for Crossref searches,
selected reference titles are shared with the provider. Service failures,
restricted resources, no matches and ambiguous candidates stay distinct; no
remote response rewrites the bibliography.

`failed` reports a detected issue, `inconclusive` reports insufficient evidence,
and `skipped` reports a check that did not run. Severity records whether a result
blocks verification or remains advisory. Required failures and incomplete checks
block verified preparation. Missing optional `qpdf` or `mutool` makes their
configured checks inconclusive. An unconfigured constraint is not a pass.

## Catalogue

The table is derived from the registered `RULES`. Linked files contain behavioral
tests; `latex-prep rule CODE` lists their exact method names. The catalogue
integrity test checks unique codes and resolvable associations, not the behavior
itself. Online tests use fake transports and do not establish current provider
availability or coverage. Tool-output fixtures are distinct from live-tool tests.

| Code | Check and scope | Behavioral test file |
| --- | --- | --- |
| `TEX001` | **Unfinished text markers.** Find TODO, FIXME, XXX and ?? in active source; comments and literal examples are ignored. Advisory heuristic. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX002` | **Editing commands.** Find supported draft/editing commands such as todo in active source. Advisory heuristic. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX003` | **Duplicate literal labels.** Find repeated literal labels in the selected source graph without expanding TeX macros. Advisory heuristic. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX004` | **Unresolved literal references.** Find literal references without a matching scanned label. Generated labels may make this inconclusive; advisory heuristic. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX005` | **Missing source dependencies.** Detect absent files named by supported literal source commands. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX006` | **External source paths.** Reject absolute paths and literal file references escaping the imported project. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX007` | **Dependency filename case.** Detect case mismatches without guessing which file a case-sensitive submission system will load. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX008` | **Ambiguous source dependencies.** Detect multiple candidate files for one supported literal source reference. | {download}`test_source.py <../tests/test_source.py>` |
| `TEX101` | **Expected document class.** Compare the selected document's literal class declaration with expected_document_class. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX102` | **Document class options.** Check required_class_options and forbidden_class_options in literal class declarations. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX103` | **Forbidden package declarations.** Check direct literal package declarations in selected local sources; packages loaded by system classes are outside this scan. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX104` | **Required abstract.** Require one supported abstract declaration when require_abstract is enabled. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX105` | **Abstract word count.** Apply explicit abstract_min_words/abstract_max_words to a lexical source count, excluding math and citation arguments. Unexpanded macros are inconclusive. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX106` | **Keyword count.** Apply keywords_min_count/keywords_max_count to supported keyword declarations with explicit separators. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `TEX107` | **Required section headings.** Check required_sections against literal headings after normalizing whitespace and case; content adequacy is not assessed. | {download}`test_manuscript.py <../tests/test_manuscript.py>` |
| `PKG001` | **Colliding protected filenames.** Block flattening when class, style, bibliography or root filenames cannot retain their required identities. | {download}`test_source.py <../tests/test_source.py>` |
| `PKG002` | **Nonportable protected filenames.** Block flattening when sanitizing a protected filename would change its naming contract. | {download}`test_source.py <../tests/test_source.py>` |
| `PKG003` | **Relocation shadows a system file.** Block flattening when relocating a local class/style would shadow a previously external dependency. | {download}`test_source.py <../tests/test_source.py>` |
| `PKG004` | **Broken companion filename relationships.** Block flattening that would break supported generated/sidecar filename relationships. | {download}`test_source.py <../tests/test_source.py>` |
| `BIB001` | **Bibliography syntax.** Detect incomplete or malformed entries using the bounded local BibTeX scanner; do not partially normalize damaged files. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB002` | **Duplicate citation keys.** Report duplicate bibliography keys and conflicting fields without merging entries. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB003` | **Repeated bibliography fields.** Report repeated fields within an entry instead of selecting or normalizing an arbitrary value. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB004` | **Undefined bibliography string macros.** Check string macros against local definitions and built-in month names; incomplete parse coverage is inconclusive. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB005` | **Bibliography relationship targets.** Resolve supported cross-reference relationships locally; dynamic or incompletely parsed targets are inconclusive. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB006` | **Literal DOI syntax.** Validate supported literal DOI syntax; macros, concatenation and ambiguous markup are inconclusive. No online resolution is performed. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BIB007` | **Exact duplicate DOI values.** Detect exact duplicate normalized literal DOI values without folding case or merging entries. | {download}`test_bibliography.py <../tests/test_bibliography.py>` |
| `BLD001` | **Undefined citations in build log.** Detect TeX's undefined-citation diagnostics; this does not check whether every bibliography entry is cited. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD002` | **Undefined references in build log.** Detect TeX's undefined-reference diagnostics after the build. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD003` | **Duplicate labels in build log.** Detect multiply-defined label diagnostics emitted by TeX. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD004` | **Missing characters in build log.** Detect missing-character diagnostics emitted by TeX; no visual glyph recognition is performed. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD005` | **Font warnings in build log.** Detect font substitution and other TeX font warnings. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD006` | **Overfull boxes in build log.** Detect overfull horizontal/vertical boxes and extract reported point overflow; no threshold is assumed. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD007` | **Underfull boxes in build log.** Detect underfull horizontal/vertical box warnings emitted by TeX. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD008` | **Unresolved rebuild requests.** Block success when the final build log still requests a rerun. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `BLD009` | **TeX errors in build log.** Detect explicit TeX error, fatal-error and emergency-stop diagnostics. | {download}`test_runtime.py <../tests/test_runtime.py>` |
| `PDF001` | **Total PDF page limit.** Compare the full page count, including references and appendices, with explicit max_pages. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF101` | **Expected PDF page dimensions.** Compare each page's reported dimensions in PDF points with expected_page_width_pt/expected_page_height_pt using page_size_tolerance_pt. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF102` | **Consistent PDF page dimensions.** Compare the full range of page dimensions using page_size_tolerance_pt when require_consistent_page_size is enabled. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF103` | **Minimum effective image resolution.** Compare Poppler's reported x/y raster PPI at embedded scale with explicit min_image_dpi; vector art and figure text are outside this check. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF104` | **Embedded PDF fonts.** Require every font reported by pdffonts to be embedded when require_embedded_fonts is enabled. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF105` | **Type 3 PDF fonts.** Reject fonts reported as Type 3 when forbid_type3_fonts is enabled; Type 3 does not necessarily mean bitmap. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF106` | **PDF encryption.** Reject encryption reported by pdfinfo when forbid_encryption is enabled; inaccessible metadata is inconclusive. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF107` | **PDF forms.** Reject forms reported by pdfinfo when forbid_forms is enabled. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF108` | **PDF JavaScript.** Reject JavaScript reported by pdfinfo when forbid_javascript is enabled; this is not exhaustive active-content analysis. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `PDF109` | **Embedded PDF attachments.** Reject embedded files reported by pdfdetach when forbid_attachments is enabled. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `CMP001` | **PDF comparison page count.** Compare the number of pages in the two supplied PDFs. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `CMP002` | **PDF comparison extracted text.** Compare per-page Poppler text extraction; this is not proof of semantic equivalence. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `CMP003` | **PDF comparison rendered pages.** Compare exact RGB renders at 144 DPI within bounded page/pixel limits; smaller details may escape detection. | {download}`test_pdf.py <../tests/test_pdf.py>` |
| `FMT001` | **Source formatting differences.** Report differences from explicitly configured tex-fmt output without modifying the original project. | {download}`test_formatting.py <../tests/test_formatting.py>` |
| `FMT002` | **Formatter idempotence.** Reject a formatting proposal when a second tex-fmt pass changes it again. | {download}`test_formatting.py <../tests/test_formatting.py>` |
| `BIB101` | **Source citation coverage.** Resolve supported literal citation keys against resources declared by the selected root, or each explicitly selected all-root graph. Dynamic source and incomplete bibliographies are inconclusive; this does not replace compilation. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB102` | **Apparently uncited bibliography entries.** Find entries unreachable from literal citations, nocite and supported relationships within selected bibliography resources. Explicit all-root scope unions usage across roots; dynamic/filter/set uncertainty prevents a definitive absence claim. No entry is removed. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB103` | **Configured bibliography fields.** Check user-supplied entry-type field requirements, including explicit alternatives such as author\|editor. No publisher schema is assumed. Missing required fields fail; macros, unresolved inheritance and incomplete coverage are blocking inconclusive results. Include doi here to require DOI presence. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB104` | **Requested DOI presence.** When DOI entry types are explicitly configured, report absent or empty DOI fields as advisory. Macros and inheritance are inconclusive; absence is not proof that a DOI exists, and syntax or identity is not verified by this presence check. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB105` | **Bibliography page-range syntax.** Check supported numeric and matching-prefix page ranges for malformed or descending endpoints. Single pages and comma-separated ranges are supported; Roman numerals, macros and unfamiliar notations are explicitly inconclusive. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB106` | **Bibliography URL syntax.** Validate literal HTTP, HTTPS and FTP URL structure, host, port, whitespace and percent escapes. Other schemes and TeX markup are inconclusive. No request is made and syntax success does not establish reachability. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB107` | **Possible title capitalization loss.** When explicitly enabled, flag unprotected uppercase or mixed-case title tokens as a style-dependent advisory. Existing braces are retained; macros are inconclusive and no capitalization is changed. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BIB108` | **Possible duplicate bibliography works.** When explicitly enabled, compare bounded literal titles at the configured similarity threshold within matching normalized author/year groups. Candidates are heuristic, separate from exact DOI identity, and never merged. Missing metadata, macros and comparison limits are inconclusive. | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| `BLD101` | **Configured overfull-box tolerance.** Compare numerical final-log box overflow with an explicit tolerance in TeX points; incomplete build/log evidence cannot pass. | {download}`test_build_checks.py <../tests/test_build_checks.py>` |
| `BLD102` | **Recorder-observed package policy.** Apply explicit allow/deny/required sets to transitive .sty and .cls inputs observed by the build recorder. | {download}`test_build_checks.py <../tests/test_build_checks.py>` |
| `BLD103` | **Minimum declared package date.** Compare a loaded package's bounded literal ProvidesClass/ProvidesPackage header date to an author-specified minimum; this is not authenticity verification. | {download}`test_build_checks.py <../tests/test_build_checks.py>` |
| `BLD104` | **Existing local package shadow.** Look up a system counterpart from an empty sandboxed workspace for each loaded local class/package; report intentional-override candidates as advisories. | {download}`test_build_checks.py <../tests/test_build_checks.py>` |
| `BLD301` | **Actual loaded class and package option inventory.** Collect bounded post-document normalized/raw kernel option lists and global class options from a line-neutral disposable-build hook. Require fresh complete markers, a successful build, and exact recorder agreement for package origins. Effective defaults and later package setup commands are outside this inventory. | {download}`test_loaded_options.py <../tests/test_loaded_options.py>` |
| `MAN001` | **Allowed direct package declarations.** Compare direct literal declarations in the selected local graph with an explicit allowlist; system transitive packages are outside scope. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN002` | **Layout override candidates.** Advisory scan for literal negative spacing, page breaks, shrinking, forced placement and layout length overrides; intent is not inferred. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN003` | **Abstract citation policy.** Apply an explicitly enabled no-citation policy to supported citation commands in literal abstracts. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN004` | **Abstract abbreviation candidates.** Apply a selected forbid/define policy heuristically to uppercase tokens; definition candidates use expanded words followed by an abbreviation in parentheses. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN005` | **Maximum literal heading depth.** Compare supported literal heading commands with a user-supplied maximum depth; section has depth one. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN006` | **Apparently empty sections.** Advisory lexical scan for headings whose section subtree has no observable content; generated content is inconclusive. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN007` | **Hard-coded reference candidates.** Advisory scan for prose such as Figure 2, Table 3, Section 4 or Equation (5); literal examples and command arguments are excluded. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN008` | **Required literal author metadata.** Require nonempty explicitly selected title, author, affiliation, email and corresponding-author declarations in supported source commands; no identity or per-author completeness claim. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN009` | **Literal ORCID syntax and checksum.** Validate literal identifiers in orcid, ORCID and orcidlink commands with the ISO 7064 MOD 11-2 checksum; validity does not establish ownership. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN010` | **Nonempty required declarations.** Require literal content under user-named headings or declaration{name}{body} commands; statement adequacy and applicability require human review. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN011` | **Required figure and table captions.** Require nonempty literal caption commands in supported figure/table environments, including starred environments and certain literal input boundaries. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN012` | **Required figure and table labels.** Require a nonempty literal label associated with each supported figure/table environment. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN013` | **Figure and table label placement.** Check that literal float labels follow a caption in source order; custom counter changes and subfloat semantics are inconclusive. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN014` | **Figure and table references.** Require an external supported literal reference to each labeled figure/table; generated references and unsupported float structures are inconclusive. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN015` | **Figure and table first-reference source order.** Compare first literal reference order separately for figures and tables with their source appearance; this does not infer rendered float order. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `MAN016` | **Required figure descriptions.** Require nonempty literal Description commands for figures and flag obvious placeholder text; presence does not establish description quality or PDF accessibility. | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| `PRV101` | **Explicit source metadata sanitization.** Plan user-selected literal metadata replacements in copied TeX source only when the unique field matches its exact expected-before value. Generated, conditional, ambiguous and unsupported declarations block the proposal. | {download}`test_metadata_privacy.py <../tests/test_metadata_privacy.py>` |
| `PRV102` | **Rebuilt PDF metadata after sanitization.** Check selected hypersetup replacements against final reported PDF properties and search those properties for removed literal source values. Missing metadata evidence remains inconclusive; binary objects and anonymity are not verified. | {download}`test_metadata_privacy.py <../tests/test_metadata_privacy.py>` |
| `PRV103` | **Configured identity terms in image metadata.** Scan supported PNG text/EXIF and pre-scan JPEG EXIF/XMP/comments for explicit literal identity terms with bounded parsing and decompression. No OCR, pixel scan, image rewriting or general anonymity guarantee. | {download}`test_metadata_privacy.py <../tests/test_metadata_privacy.py>` |
| `NET001` | **DOI resolution.** Resolve explicitly selected literal DOIs through doi.org; distinguish missing, restricted and unavailable responses from successful resolution. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET002` | **Remote bibliography metadata agreement.** Compare supported literal title, author, year and venue fields with Crossref DOI metadata; report differences as reviewable evidence, not automatic corrections. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET003` | **Missing DOI candidates.** Search Crossref using an explicitly shared literal bibliography title and rank reviewable DOI candidates; no match and ambiguity remain distinct. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET004` | **Published versions of preprints.** Surface Crossref published-version relations or title-search candidates for explicitly identified preprints; never infer that similar titles establish the same work. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET005` | **Correction and retraction notices.** Surface Crossref update metadata and related notice records, keeping corrections and retractions separate and provider coverage explicit. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET006` | **Reference URL health.** Check explicitly selected literal bibliography URLs with bounded public-network requests; redirects, authentication, throttling and unavailable resources are distinct outcomes. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `NET007` | **Replication and source-link health.** Check explicitly supplied replication URLs and literal URL commands in the selected source graph; HTTP availability does not establish reproducibility or artifact completeness. | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| `PDF201` | **Section page budgets.** Compare explicitly supplied inclusive page ranges with user-supplied section budgets; ranges outside the actual document are inconclusive. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF202` | **Reported rendered text size.** Compare Poppler text sizes with global or explicitly located regional minima. Integer rounding, absent text, unsupported rotation/crop and partial regions remain inconclusive. Raster/outlined text and semantic categories are not inferred. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF203` | **Printable text bounds.** Compare extractable text boxes with supplied margins or allowed regions. Advisory; image bounds, clipping paths, rotated/cropped pages and semantic object attribution are outside this adapter's measured scope. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF204` | **Text overlap candidates.** Measure pairwise extractable-text rectangle intersections against an explicit area ratio; legitimate mathematical overlays may be reported. Advisory. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF205` | **Sparse rendered page candidates.** Compare bounded grayscale pixel coverage with an explicit minimum and background assumption. Exempt pages are supplied explicitly; intended blank pages and scan-only pages are not inferred. Advisory. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF206` | **Required PDF metadata.** Require nonempty user-selected pdfinfo text properties. Presence does not establish rights, identity or semantic correctness. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF207` | **Expected PDF metadata agreement.** Compare properties with supplied expected values using whitespace-only normalization. Source expansion, identity and author order equivalence are not inferred. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF208` | **PDF tagging presence.** Require pdfinfo to report tagging present; this is not a reading-order, table/link structure, PDF/UA or general accessibility validation. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF209` | **PDF language declaration.** Inspect qpdf's resolved catalog language for required presence or an explicit expected language tag; unsupported encoding or missing qpdf is inconclusive. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF210` | **PDF annotation type policy.** Inspect resolved page annotation objects for explicitly forbidden subtypes, including indirect objects. Missing/incomplete qpdf evidence is inconclusive. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF211` | **PDF action-name candidates.** Scan resolved reachable dictionaries for explicitly forbidden /S names. Advisory action candidates; opaque streams and orphan objects are excluded, and no malware-free guarantee is made. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF212` | **Included PDF figure fonts.** Inspect explicitly supplied input PDFs for configured embedding and Type 3 policies. No reported fonts is inconclusive; final-PDF attribution and bitmap Type 3 glyph classification are not established. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF213` | **Transformed PDF stroke width.** Compare MuPDF trace stroke widths after uniform placement transforms with an explicit minimum. Hairlines/nonuniform transforms, raster lines and filled outlines are unmeasured; candidates remain advisory. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF214` | **Rendering color-space policy.** Compare MuPDF trace rendering color-space names with an explicit allowed list. Source encoding, color-only meaning and perceptual accessibility are not inferred. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF215` | **Text contrast against supplied background.** Compare opaque trace text colors against an explicitly supplied uniform background, treating DeviceRGB/Gray as sRGB. Actual backgrounds, raster text and compositing are not established; the comparison is advisory. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PDF216` | **Grayscale color separation candidates.** Compare nominal sRGB luminance for distinct opaque DeviceRGB/Gray colors on each page against a supplied difference. Advisory; color-only meaning, proximity, raster palettes, compositing and perceptual accessibility are not inferred. | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| `PRV001` | **Configured source identity terms.** Scan configured literal identity terms in bundle text, comments, URLs and filenames. Case-insensitive matches do not establish anonymity. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PRV002` | **Configured PDF identity terms.** Scan extracted PDF text and supplied metadata for configured literal identity terms; unavailable extraction is inconclusive. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PRV003` | **Possible identifying language.** Optionally flag acknowledgement declarations and literal self-identifying phrases in active source as advisory hints. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PRV004` | **Possible credentials.** Optionally detect bounded credential-shaped tokens, private-key headers and secret assignments in local text, filenames and PDF metadata. Candidate values are redacted; absence is not proof of safety. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PRV005` | **Private comments and unfinished notes.** Optionally scan TeX comments for unfinished markers and private-note phrases without changing licensing, directives or meaningful comments. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PRV006` | **Literal shell-execution preflight.** Optionally flag supported literal write18 and shell-execution constructs in active source. This advisory scan does not replace runtime isolation. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG101` | **Configured filename length.** Apply an explicit maximum Unicode-character length to each file basename in the supplied bundle. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG102` | **Configured filename characters.** Apply a user-supplied literal allowed-character set to file basenames; this is separate from archive path-safety checks. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG103` | **Configured bundle file types.** Compare case-insensitive final filename extensions with an explicit allowlist; an extension does not establish the actual file format. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG104` | **Explicit required deliverables.** Require configured relative paths and check their declared kind using regular-file, PDF/ZIP signature or UTF-8-text evidence. Presence does not establish separate delivery or successful compilation. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG105` | **Compressed archive size.** Compare the actual final archive's byte length with a separate explicit compressed-size limit without extracting it. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG106` | **Assets outside the selected dependency graph.** Optionally list supported asset files not observed in the selected literal dependency graph. Dynamic, missing or ambiguous dependencies make this inconclusive; candidates are never deletion recommendations. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `PKG107` | **Author-supplied template references.** Compare explicit project/reference file pairs byte-for-byte and report whether supported ProvidesClass/ProvidesPackage version declarations agree. No registry, official-status or age inference is made. | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| `MAN201` | **Configured per-author field completeness.** Check each sequential literal preamble author record against user-selected associated commands; shared fields, conditional records and template semantics are not inferred. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PDF401` | **Explicit rendered heading expectations.** Find user-supplied heading title and number text on explicit PDF pages after whitespace normalization; section and appendix numbering semantics are not inferred. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PDF402` | **Tagged table row and cell structure.** Inspect declared Table, row-group, TR, TH and TD relationships. Untagged visual tables, meaningful headers, cell spans and PDF/UA conformance remain unverified. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PDF403` | **Link tag and annotation associations.** Match Link structure elements to page Link annotations and inspect supported target presence; accessible-name adequacy, navigation and remote target availability are not established. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PDF404` | **Nonempty tagged Figure alternative text.** Require nonempty supported Alt strings on declared Figure elements. This does not find every visible image or judge description quality. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PDF405` | **Structure tree and marked-content reference integrity.** Inspect an explicitly required structure tree, parent links, duplicate or invalid MCIDs and parent-tree backlinks. Stream coverage, natural reading order and accessibility conformance remain unverified. | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| `PKG301` | **Independent document packages.** Verify every explicitly selected document independently and release all packages only when every required check and archive rebuild succeeds. | {download}`test_multi_document.py <../tests/test_multi_document.py>` |
| `CMP101` | **Repeated baseline stability.** Repeat the clean baseline build the configured number of times and require identical page count, extracted text and exact 144-DPI renders before transformation. | {download}`test_multi_document.py <../tests/test_multi_document.py>` |
| `RPT101` | **Unmatched review decision.** Report an explicit suppression or accepted exception whose exact code and location scope match no finding. Decisions are not inferred or broadened. | {download}`test_reporting.py <../tests/test_reporting.py>` |
| `RPT102` | **Ineligible review decision.** Retain failures when a requested review decision targets incomplete evidence, an unwaivable safety/build/archive check or an error suppression. | {download}`test_reporting.py <../tests/test_reporting.py>` |
| `RPT103` | **Unverified diagnostic export.** Label sanitized diagnostic reports, diffs, logs and explicitly allowlisted text artifacts as unverified material, never a submission package. | {download}`test_reporting.py <../tests/test_reporting.py>` |
| `BLD201` | **Configured bibliography backend.** Compare the selected auto, BibTeX or Biber strategy with bounded fresh AUX/BCF control evidence. Explicit strategies disable the other backend; malformed control files remain inconclusive. | {download}`test_runtime_backends.py <../tests/test_runtime_backends.py>` |
| `BLD202` | **Required bibliography tool availability.** Verify and record the locally available backend version in isolation when fresh bibliography controls require BibTeX or Biber. Availability alone does not prove bibliography completion or toolchain compatibility. | {download}`test_runtime_backends.py <../tests/test_runtime_backends.py>` |
| `TEX201` | **Reviewed comment removal.** Propose removing selected comment bodies while retaining percent signs, line endings, literal regions, directives and license blocks; unclosed or dynamic lexical contexts are inconclusive. | {download}`test_source_transform.py <../tests/test_source_transform.py>` |
| `TEX202` | **Literal source input merging.** Plan recursive merging of standalone literal input lines within a bounded, single-root dependency graph; include/import execution semantics remain unsupported. | {download}`test_source_transform.py <../tests/test_source_transform.py>` |
| `TEX203` | **Generated bibliography inlining.** Replace one standalone bibliography command with an explicitly selected local BibTeX thebibliography environment; biblatex data and unresolved graphs are inconclusive. | {download}`test_source_transform.py <../tests/test_source_transform.py>` |
| `FMT101` | **Formatter protected regions.** Reject changes to literal environments, inline literals, formatter-off regions, directives or license blocks; unmatched regions are inconclusive. | {download}`test_formatting.py <../tests/test_formatting.py>` |
| `FMT102` | **Project formatting exclusions.** Select source with explicit project-relative glob exclusions and report unmatched patterns; unsafe linked source traversal remains inconclusive. | {download}`test_formatting.py <../tests/test_formatting.py>` |
| `FMT103` | **Explicit formatting blank-line policy.** Verify paragraph-separator preservation or collapse repeated blank lines outside protected spans; this adapter policy is independent of formatter CLI flags. | {download}`test_formatting.py <../tests/test_formatting.py>` |
| `PKG201` | **Explicit flat filename overrides.** Apply portable unique filename overrides with unchanged extensions to the flat map; renaming required root/class/style/bibliography identities is unsupported. | {download}`test_source.py <../tests/test_source.py>` |
| `BIB201` | **Lossless bibliography field layout.** Propose whitespace and trailing-comma layout for declared bibliography entries. Literal values, brace protection, concatenations, unknown fields, comments, string declarations, preambles, UTF-8 BOM and newline style are retained. Malformed or unreadable selected resources block the entire proposal; originals are never written. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `BIB202` | **Explicit bibliography field removal.** Remove explicitly named non-relationship fields in selected resources. No private-field list is assumed; unknown fields, values outside selected fields, string macros and percent comments are preserved. Invalid input blocks the complete proposal. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `BIB203` | **Conservative cited-only bibliography.** Propose removal of entries unreachable from the selected literal citation graph, preserving nocite, aliases, relationships, entry sets, preambles and all string declarations. Multiple roots, dynamic citations, filters, missing keys, ambiguous aliases and unresolved relationships block pruning. No arbitrary-TeX coverage is claimed. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `BIB204` | **Reviewed bibliography key mapping.** Apply explicit key renames and donor-to-retained-target merge mappings atomically to entry keys, supported literal citations and relationships. A merge retains target metadata exactly and drops the explicitly selected donor; identity and conflicts are never inferred. Dynamic uses, multiple roots, unknown keys and collisions block the complete proposal. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `BIB205` | **Guarded bibliography metadata edits.** Apply explicit key/field/expected/replacement rows to literal metadata, including capitalization brace protection. None represents field absence or removal. Expected contents must match exactly; macros, concatenations, repeated fields and invalid replacement syntax block the complete proposal. No metadata is guessed. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `BIB206` | **Preservable bibliography key ordering.** Opt-in literal key ordering moves intact entry bytes within a resource. Interleaved comments, free text and macros block moves across their boundaries. Sorting that would place a cross-reference parent before its child or requires resolving an external/macro parent is refused; bibliography backend semantics are not inferred. | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| `PDF301` | **Printed crop/registration-mark candidates.** Locate paired short strokes aligned with explicit trim corners; also identify crossed registration strokes. Raster/outlined marks remain unmeasured. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF302` | **Figure outer whitespace.** Measure the area outside a bounded rendered ink box in an explicit region against a supplied ratio/background. Input PDF figures may use their whole page. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF303` | **Figure boundary ink candidates.** Locate rendered ink in a supplied boundary band; touching ink can be an intentional border and never proves clipping. Unsupported page frames are inconclusive. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF304` | **Image and polygon drawing bounds.** Compare affine image and straight filled-path bounds after rectangular clipping with allowed regions. Text, curves, strokes and masks remain unmeasured. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF305` | **Rectangular clipping candidates.** Locate image/polygon extents crossing the page or active supported rectangular clip; intentional cropping remains a review decision. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF306` | **Observed artwork composition.** Inventory traced raster, vector or mixed operations for a selected PDF; text/backgrounds count, and unsupported compositing or empty evidence is inconclusive. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF307` | **Explicit artwork-category DPI.** Apply a supplied category threshold to each raster placement, including axes and the coarsest sampling direction under shear. Categories are never inferred. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF308` | **Type 3 glyph painting classification.** Distinguish image/vector painting in reachable declared Type 3 programs and Form XObjects, with an optional bitmap policy; opaque programs are inconclusive. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF309` | **Affine stroke-normal thickness.** Measure straight-segment normal thickness after nonuniform affine transforms. Curves/text need uniform transforms; hairlines, caps and joins are excluded. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF310` | **Raster-dominated region candidates.** Measure union coverage of axis-aligned raster placements in explicit regions. High coverage without page text is a scan/table candidate, not a diagnosis. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF311` | **Grayscale review preview.** Retain validated, bounded grayscale page previews in the inspection workspace. Preview creation does not establish semantic color independence. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| `PDF312` | **Rendered foreground/background sample contrast.** Compare composited RGB pixels from explicit foreground/background patches. Patches must be homogeneous unless a channel-spread tolerance is supplied. Conservative luminance bounds preserve threshold uncertainty; text semantics are not inferred. | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |

## Extending a check

Add an explicit code, descriptive rule name, scope and fix guidance to the
applicable data-only `*_check_rules.py` module, registered by
{download}`rules.py <../src/latexprep/rules.py>`. Link it to a unit test that runs the actual
checker on a meaningful fixture and asserts both the code and the intended
behavior. Include a valid counterpart, violating case, and unavailable or
ambiguous evidence case when applicable. Reuse an existing behavior test when it
already proves the contract; do not add a test that only constructs a Finding.

Keep evidence types and failure states intact when changing execution order.
Check scheduling and serial/parallel equivalence separately from rule behavior.
See the [parallel execution design](parallelization-plan.md) for the shared
resource budget and ordered verification gates.
