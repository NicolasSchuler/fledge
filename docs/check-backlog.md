# Check implementation backlog

The implementation now includes explicit source and bibliography transformations,
independent document packages, review/reporting controls, and additional bounded
PDF, metadata and structure checks. These additions close specific absence claims;
they do not complete the full requirements. Broader syntax/geometry support,
image preparation, richer PDF review output and platform/tool validation remain
engineering work. Interpretation of scientific meaning, accessibility adequacy
and anonymity still requires human judgment.

This inventory reconciles the {download}`requirements <../requirements.md>` and original
check ideas with the current checker implementations on 7 October 2026. The
[catalogue](checks.md) has **156 registered diagnostic codes**, including subchecks
and preparation guards. That is not a denominator for completed requirements;
the grouped rows below also have different sizes and must not be used to compute
a completion percentage. No code is reserved here for an unimplemented check.

## How to read this backlog

- **Implemented** means the narrowly stated behavior exists, within the stated
  boundary. It does not mean every related requirement or platform is covered.
- **Partial** means related behavior exists, but the row names the missing part.
- **Not implemented** means no corresponding checker or operation currently exists.
- **Unverified** in a row means implementation exists but the named evidence or
  live-platform validation is missing; it is not an implementation claim.
- **Intentionally excluded** means it must not become an implicit product feature.
- **Next** prioritizes remaining first-release requirements; **Extended** covers
  deeper analysis; **Optional** requires explicit selection and, for remote
  services, explicit data-sharing authorization. **None** means no pending work
  for the narrow row or an intentional exclusion. These are planning priorities,
  not new release commitments.

All numerical limits, required fields, package policies and identity terms must
come from explicit user settings. An inventory is not a policy pass. Deterministic
observations and derived measurements must remain separate from heuristic
suspicions and semantic advice. Missing tools or uncertain interpretation must
produce an incomplete result, not a pass. Current settings are defined in
{download}`Settings <../src/latexprep/config.py>`,
{download}`ManuscriptOptions <../src/latexprep/manuscript.py>`,
{download}`PdfOptions <../src/latexprep/pdf.py>`, and the typed options in the additional
`*_checks.py` modules. The {download}`additional-check example <../examples/checks.toml>`
shows the nested tables. No template registry, package policy or publisher limit
is maintained by the application. Online checks require both selection and
network permission; unavailable providers are not passes.

The [preparation options](preparation-options.md) and
{download}`example <../examples/preparation.toml>` describe the new opt-in operations.
`pdf_artwork` applies to the final document; `figure_artwork` applies separately
to input PDFs. Regions and sample patches belong to each inspected PDF's own
coordinate system. Final-document coordinates are not reused for figures.

Concrete remaining engineering includes image optimization/externalized-figure
adapters, annotated PDF pages and rendered before/after views, broader literal
source forms and drawing/clip/mask coverage, OCR/automatic figure-table
segmentation, Windows isolation and a web interface. Linux Bubblewrap, qpdf and
MuPDF live validation is incomplete on the macOS development host.
These gaps are separate from semantic questions that no current check answers,
such as reference identity, declaration adequacy and natural reading order.

## Compilation, templates and source structure

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Implemented | None | Clean build, final-log errors/rerun requests, missing glyph and font warnings; reject stale or partial PDFs. | `BLD001`–`BLD009`; fresh build directories, bounded `latexmk`, complete log/recorder requirements. Missing-character evidence comes from logs, not visual glyph recognition. See [build evidence](#build-evidence). |
| Partial | Next | Dependency completeness for every explicitly selected document; extend literal adapters without guessing execution context. | `TEX005`–`TEX007` and recorder checks cover each selected package; `TEX008` is an advisory naming the file TeX's lookup order selects. With a complete build trace, remaining lexical uncertainty is a `package.dependencies` warning, while flattening and source transformations still need a complete literal graph. Literal import/subimport aliases, constrained subfiles, selected data readers and explicit local font files now resolve and rewrite paths. Dynamic/custom readers, inferred font faces, legacy subfiles preambles and content-dependent subfile checks remain unsupported or inconclusive. |
| Implemented | None | Apply a user-supplied overfull-box tolerance and report the threshold comparison. | `BLD101` compares supported numerical final-log overflow with an explicit tolerance in TeX points. Incomplete logs/builds cannot pass; `BLD006`/`BLD007` retain the underlying advisories. |
| Implemented | None | Check expected literal document class and required/forbidden class options. | `TEX101`, `TEX102`; indirect, conditional or macro-generated declarations are inconclusive. See [manuscript evidence](#manuscript-evidence). |
| Partial | Extended | Inventory actual loaded classes/packages/options and identify existing local shadow copies. | `BLD301` records actual normalized/raw kernel option registers after execution, including forwarded options, and matches origins to the recorder. Defaults, options consumed without recorded registers, and later package setup semantics remain outside that inventory. `BLD104` checks local-shadow candidates through sandboxed system lookup; `PKG003` protects flattening from creating a new shadow. |
| Partial | Extended | Compare supplied class/template files for integrity and version compatibility against a user-provided reference. | `PKG107` compares explicit local project/reference pairs byte-for-byte and reports supported declared-version agreement. `BLD103` checks explicit minimum header dates for loaded packages. Semantic compatibility, authenticity and official/outdated status are not inferred. |
| Implemented | None | Enforce package deny and allow policies over a clearly stated loaded-package scope. | `TEX103` and `MAN001` cover direct literal declarations; `BLD102` applies explicit allow/deny/required sets to recorder-observed transitive `.sty`/`.cls` inputs. Their scopes remain separate, and incomplete recorder evidence is inconclusive. No policy lists are maintained. |
| Partial | Extended | Diagnose draft leftovers and suspicious spacing/layout manipulation: negative spacing, manual page breaks, shrinking, forced floats, margin/text-width/line-spacing overrides. | `TEX001`/`TEX002` detect markers/editing commands (`\color` and `\textcolor` are not editing commands; local `.sty`/`.cls` files are not scanned); `MAN002` adds literal spacing, page-break, shrinking, forced-float and layout-override candidates. Supported literal examples/comments are excluded. These advisories do not infer intent or discover every template-specific dummy field. |
| Implemented | None | Require a nonempty supported abstract; apply explicit abstract-word and keyword limits. | `TEX104`–`TEX106` use documented lexical counting, not rendered word counts. Unknown macros and ambiguous declarations are inconclusive. |
| Partial | Extended | Detect abstract citations and apply an explicitly requested abbreviation policy. | `MAN003` enforces an explicit no-citation policy for supported literal abstracts; `MAN004` applies selected forbid/define heuristics to uppercase abbreviation candidates. Contextual abbreviation meaning and arbitrary declaration forms remain unverified. |
| Partial | Extended | Heading depth, configured numbering, apparently empty sections, and hard-coded reference numbers. | `TEX107` and `MAN005`–`MAN007` check literal source headings and candidates. `PDF401` matches supplied title/number text on explicit PDF pages. Automatic rendered-heading detection, appendix/counter semantics and generated headings remain outside this interpretation. |
| Partial | Extended | Unique labels and resolved references with generated/custom syntax support where feasible. | `TEX003`/`TEX004` scan literal labels/references as heuristics; `BLD002`/`BLD003` consume TeX diagnostics. No general macro expansion or proof of reference semantics. |

## Pages, geometry and fonts

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Implemented | None | Configured total page budget, expected page dimensions and consistent dimensions. | `PDF001`, `PDF101`, `PDF102`; total pages include references and appendices. Page boxes are inventoried. See [PDF evidence](#pdf-evidence). |
| Implemented | None | Section-specific page budgets, with explicit treatment of references, appendices and supplements. | `PDF201` checks author-supplied inclusive page ranges against explicit section budgets. Out-of-document ranges are inconclusive. Section boundaries, reference sections and supplements are not inferred. |
| Partial | Extended | Detect unintended spillover/leaking pages, near-empty pages and a blank final page. | `PDF205` measures bounded grayscale ink coverage against a supplied threshold/background and explicit exemptions. `CMP001` compares page counts. Blank/spillover candidates remain advisory; intent and image-only/scan classification are not established. |
| Partial | Extended | Printable-region and column bounds, including rotated/cropped pages and legitimate full-width material. | `PDF203` checks extractable text against explicit allowed regions; `PDF304` adds affine image and straight filled-path bounds after supported rectangular clipping. Arbitrary rotations/crops, curves, strokes, masks and complete content bounds remain unmeasured or inconclusive. |
| Partial | Extended | Overflowing equations, figures, tables and captions; overlap and clipping candidates. | `PDF203`/`PDF204` report text bounds and overlap candidates. `PDF303`/`PDF305` add boundary-ink and supported image/polygon clip crossings. Semantic object attribution, complex clips/masks and intentional-cropping decisions remain unresolved. |
| Implemented | None | Configured embedded-font and Type 3 policies for font resources reported in the final PDF. | `PDF104`, `PDF105`. Zero reported fonts cannot establish embedding coverage of outlined/image-only text. Type 3 is a font type, not proof of bitmap glyphs. |
| Partial | Extended | Distinguish bitmap painting from vector painting in Type 3 glyph programs. | `PDF308` inspects supported reachable declared Type 3 programs and nested Form XObjects, with an explicit bitmap policy. Opaque or unsupported programs are inconclusive; actual glyph-use coverage and every PDF operator/compositing form are not established. The adapter needs qpdf; live validation remains outstanding. |
| Partial | Extended | Minimum rendered manuscript, equation, caption, table and figure-label text sizes. | `PDF202` checks reported extractable-text sizes against global or explicitly located regional minima. Integer rounding, partial region overlap, absent text and unsupported geometry are inconclusive. Semantic text categories, outlined/raster lettering and OCR remain unimplemented. |

## Figures and tables

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Implemented | None | Check both effective raster-resolution axes at each placement reported by Poppler against an explicit minimum. | `PDF103` retains repeated placements and reports unmeasured DPI. It consumes Poppler's placement-scale measurements, including masks; it does not map them to source figure labels. See [PDF evidence](#pdf-evidence). |
| Partial | Extended | Classify observed artwork composition and apply supplied category thresholds. | `PDF306` inventories raster/vector/mixed drawing operations for a selected PDF; text and backgrounds count. `PDF307` applies explicit category DPI thresholds to repeated placements, including shear/rotation sampling. Automatic figure segmentation, category inference and final-page-to-source attribution remain unimplemented; empty/unsupported evidence is inconclusive. |
| Partial | Extended | Inspect included PDF figure fonts and attribute final-PDF font problems to an input figure when supported. | `PDF212` separately inspects supplied input PDF figures for explicit embedding/Type 3 policies and retains source paths. Zero reported fonts is inconclusive. Reliable mapping of final-PDF font failures back to a particular included figure is not established. |
| Partial | Extended | Measure excessive figure whitespace/borders and possible cropping at supplied boundaries. | `PDF302` measures rendered outer ink-box whitespace under an explicit background/threshold; `PDF303` reports boundary-band ink. Regions are supplied for final pages or use input-PDF whole pages. Automatic figure boundaries, arbitrary page frames and intent are not inferred; edge ink does not prove clipping. |
| Partial | Extended | Check rendered line weight, including scaled figure strokes. | `PDF213` covers supported uniform transforms; `PDF309` measures straight-segment normal thickness after nonuniform affine transforms. Curves/text need uniform transforms. Hairlines, caps, joins, filled outlines and raster lines remain outside measured coverage. |
| Partial | Extended | Inspect configured color encoding and measurable foreground/background contrast. | `PDF214` applies a rendering color-space allowlist; `PDF215` uses an explicit background assumption. `PDF312` compares actual composited RGB pixels in supplied foreground/background patches with explicit homogeneity tolerance. Automatic patch selection, general text/graphic attribution, source encoding and perceptual adequacy remain unresolved. |
| Partial | Extended | Figure caption/label presence, label placement, unreferenced figures and first-reference order. | `MAN011`–`MAN015` check supported source float captions, labels, label placement, external references and first-reference order. Custom counters/subfloats may be inconclusive; rendered float order and arbitrary template-generated floats remain unverified. |
| Partial | Extended | Table caption/label presence, label placement, references and first-reference order. | `MAN011`–`MAN015` also check supported table environments, separately ordering table references. Complex/custom float structures and rendered order remain outside the source model. |
| Partial | Extended | Table overflow, minimum rendered font size and rasterized-table candidates. | `PDF202`/`PDF203` check explicit table text regions. `PDF310` measures union coverage of axis-aligned raster placements in selected regions and reports candidates with text evidence. Automatic table identification, OCR and raster/outlined lettering measurements remain unimplemented; raster coverage does not diagnose a table or scan. |

## Bibliography and citation correctness

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Implemented | None | Local syntax, duplicate keys, repeated fields, undefined strings and supported relationship targets. | `BIB001`–`BIB005`; bounded scanner preserves unknown fields and leaves damaged files unchanged. Incomplete parse coverage is explicit. See [bibliography evidence](#bibliography-evidence). |
| Implemented | None | Resolve used citation keys before compilation as well as after it. | `BIB101` resolves supported literal citation keys against bibliography resources declared by the selected graph, or explicitly selected all-root graphs. Dynamic/incomplete evidence is inconclusive; `BLD001` remains the independent compilation check. |
| Partial | Next | Detect uncited entries and offer a conservative cited-only bibliography. | `BIB102` reports usage candidates in one finding that lists every entry; opt-in `BIB203` proposes cited-only resources while retaining supported nocite/alias/relationship/set closure and declarations. Incomplete/dynamic scope, subfile content, filters, missing keys and ambiguous relationships block pruning. Arbitrary-TeX usage proof remains outside scope. |
| Partial | Extended | Distinguish exact duplicate identifiers from likely duplicate works. | `BIB007`/`BIB002` report exact identifiers; `BIB108` adds bounded title-similarity candidates. Explicit `BIB204` merge mappings retain chosen target metadata and redirect supported uses atomically. Work identity, conflict resolution and deciding which record to retain are not inferred. |
| Implemented | None | Validate supported literal DOI syntax and offer visible DOI-prefix normalization. | `BIB006` plus `normalize_dois`; neither performs DOI resolution nor verifies that the DOI denotes the cited work. Macros and ambiguous markup remain unchanged. |
| Implemented | None | Entry-type/user-configured required fields; missing title/authors/year/venue; page-range and URL syntax; capitalization protection. | `BIB103` applies explicit per-type requirements and field alternatives; `BIB105`/`BIB106` check supported page-range/URL syntax. Optional `BIB107` flags capitalization-protection candidates without editing braces or values. Macros, inheritance and unfamiliar syntax remain inconclusive. |
| Implemented | None | Report missing DOI fields when applicable. | `BIB104` reports missing/empty DOI fields for explicitly selected entry types as advisories. It does not assert that a DOI exists or that references without DOIs are invalid. |
| Partial | Optional | Resolve DOIs and compare title, authors, year, venue and version with authoritative metadata. | `NET001` resolves selected literal DOIs; `NET002` compares supported fields with Crossref metadata. Resolution, provider agreement and cited-version identity remain separate. Requests are bounded and explicitly authorized; provider coverage and unsupported metadata remain visible. |
| Partial | Optional | Suggest missing DOI candidates and published versions of preprints; merge only after explicit review. | `NET003`/`NET004` surface Crossref candidates with ambiguity. `BIB204` can apply a user-supplied donor-to-target mapping; it does not turn a remote candidate into an authorized match or merge. Version identity and replacement decisions remain unverified. |
| Implemented | Optional | Check reference URLs and replication/artifact links. | `NET006`/`NET007` make explicitly selected bounded public-network requests for literal reference/source URLs and supplied artifact links. Redirects, authentication, throttling and temporary failures remain distinct. Availability does not establish artifact completeness or reproducibility. |
| Partial | Optional | Surface authoritative correction/retraction notices. | `NET005` surfaces attributed Crossref update metadata and related notices, distinguishing correction/retraction types. Absence or agreement from one provider is not exhaustive evidence; broader provider coverage and human interpretation remain necessary. |
| Partial | Next | Bibliography formatting, explicit field removal, cited-only output, reviewed key/merge mappings, guarded metadata edits and optional ordering. | `BIB201`–`BIB206` implement copy-only proposals for the documented literal subset, preserving unaffected values/comments/macros and rejecting partial/ambiguous plans. Citation/key/relationship edits are atomic. Automatic metadata correction, work-identity decisions, dynamic citation expansion and arbitrary backend ordering semantics remain unsupported; preparation rebuilds and compares rendered output. |

## Author metadata, declarations and anonymity

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Partial | Next | Required author, affiliation, email and corresponding-author fields. | `MAN008` checks supported literal metadata presence. `MAN201` checks each sequential preamble author record against explicitly selected associated commands. Shared-field association, arbitrary template grammars, generated metadata and identity/ownership remain unverified. |
| Implemented | None | ORCID syntax and checksum. | `MAN009` validates supported literal ORCID commands with the ISO 7064 MOD 11-2 checksum. Syntax/checksum success does not establish identity or ownership. |
| Partial | Next | Required title/author/publication/rights metadata, source-to-PDF agreement and explicit sanitization. | `PDF206`/`PDF207` and source matching check supported properties. `PRV101` proposes exact expected-before literal source metadata replacements; `PRV102` rechecks selected rebuilt-PDF properties and removed values. Generated/ambiguous declarations, binary-object sanitization and publication/rights semantics remain outside coverage. |
| Partial | Extended | Presence of Data Availability, competing interests, funding, CRediT/author contributions, AI-use and ethics declarations. | `MAN010` requires literal content under user-named headings or supported declaration commands; `TEX107` also checks heading presence. Template-specific/generated declaration forms, applicability and statement adequacy are not inferred. |
| Not implemented | Optional | Semantic adequacy of declarations or consistency with the study. | Human review is required; optional advice needs explicit selection. Do not infer that ethics/AI-use statements are applicable or generate their contents. |
| Partial | Extended | Anonymous-review scans of names/affiliations, acknowledgements, comments, filenames and metadata. | `PRV001`–`PRV003` scan configured terms and identifying-language candidates. `PRV001`/`PRV002` match whole words unless `identity_term_matching = "substring"`. `PRV103` adds bounded PNG text/EXIF and pre-scan JPEG EXIF/XMP/extended XMP/IPTC/comment inspection; MakerNote and other unsupported blocks are recorded as limitations. Unsupported image formats/metadata, raster text, indirect clues and semantic anonymity remain unverified; image metadata is not rewritten. |
| Partial | Extended | Identifying artifact URLs, institution/domain hints, self-identifying language and cross-document leaks. | Configured `PRV001` terms match literal/URL-decoded identities across supplied text files and document roots. `PRV003` separates heuristic self-identifying phrases from direct matches. Arbitrary institutional inference and semantic cross-document anonymity are not established. |
| Partial | Next | Required separate deliverables: title page, highlights, graphical abstract, cover/response letter, supplements and replication link. | `PKG104` checks explicit paths and file/PDF/ZIP/text evidence. `workflow.documents` and `PKG301` assign independent source packages and verify each archive rebuild. Content adequacy, actual separate delivery and unselected roots remain unverified; noncompiled deliverables are not forced through TeX. |

## Accessibility

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Implemented | None | Required nonempty figure descriptions, including supported `Description` markup, and obvious placeholders. | `MAN016` requires nonempty supported literal `Description` commands per figure and flags obvious placeholders. Unknown/generated content stays inconclusive. This is a source-presence check, not description-quality or PDF-accessibility validation. |
| Not implemented | Optional | Description quality and adequacy. | Requires human judgment or explicitly enabled, labeled advice with data-sharing consent; source presence cannot prove quality. |
| Partial | Extended | Document language, PDF tagging, reading order, accessible links and table structure. | `PDF208`/`PDF209` inspect tagging/language. `PDF402`–`PDF405` check supported declared table/link/Figure structure, Alt presence, parent links and MCID/backlink consistency. Complete stream coverage, natural reading order, meaningful headers/names/descriptions and PDF/UA conformance remain unverified. Object adapters require qpdf and are not live-validated on this host. |
| Partial | Next | Searchable/readable text and possible scan-only or rasterized regions. | `pdf.text` measures page extraction; `PDF310` adds explicit-region raster-coverage candidates with text evidence. No OCR or reliable semantic scan classification exists. Image-only, outlined and blank pages are not treated as equivalent. |
| Partial | Extended | Color-only encoding checks, measurable contrast, and grayscale previews. | `PDF215`/`PDF216` retain nominal-color checks; `PDF312` measures explicit rendered foreground/background samples. `PDF311` exports selected bounded grayscale previews outside the source archive. Color-only meaning, automatic sample/region choice, raster palette semantics and perceptual separability still require further analysis or review. |

## Submission bundle and PDF object checks

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Partial | Next | Remove known debris and select needed project inputs without guessing uncertain dependencies. | Preliminary `plan_cleanup` preserves observed dependencies, unknown files and `.bbl` files, and removes OS/editor debris and rebuildable auxiliary files. Preparation separately selects inputs from complete build traces and the selected literal dependency graph, retaining files listed in `[package] include` (and, for compatibility, required deliverables and template files) and omitting unrelated files only in staging. A missing or incomplete build trace blocks packaging; an incomplete literal graph alone is advisory. Optional `PKG106` remains an advisory inventory. No global minimality or all-root unused-file proof is claimed. |
| Partial | Next | Comment/private-note cleanup and unfinished-source checks. | `TEX001`/`TEX002` and optional `PRV005` inspect draft material. Opt-in `TEX201` proposes removal of selected comment bodies in `.tex`/`.ltx`/`.latex` files only, retaining percent signs, line endings, literals, directives and license blocks. Dynamic lexical behavior and unclosed regions block proposals. Active editing-command removal and arbitrary TeX semantics remain unsupported. |
| Implemented | None | Reject unsafe archive paths/links and supported literal external source paths. | Import rejects links, special files, absolute paths, traversal and normalized collisions; names that are only nonportable (`<>:"|?*`, trailing dot or space, reserved Windows names, control characters) are kept with a `project.nonportable_filename` warning, and macOS `Icon\r` and `._*` files are dropped as OS metadata. `TEX006` rejects supported absolute/escaping references; build recorder checks undeclared external inputs. This does not scan arbitrary prose/private paths. |
| Implemented | None | Explain source constructs that require shell escape before building. | Optional advisory `PRV006` flags supported literal `write18`/shell-execution constructs before building. Runtime still prohibits shell escape and confines processes; the lexical preflight does not replace enforcement. |
| Implemented | None | Scan likely secrets/private tokens with redacted findings and false-positive handling. | Optional heuristic `PRV004` scans supported local text, comments, bibliography notes, filenames and supplied PDF evidence for bounded credential patterns. Candidate values are redacted, false positives require review, and unscanned binary content remains outside scope. |
| Implemented | None | Configurable allowed filename characters/lengths/types and final ZIP size. | `PKG101`–`PKG103` apply explicit basename character-count, literal character-set and final-extension policies. `PKG105` applies separate `max_archive_bytes` to actual compressed output. Existing import limits and `PKG001`–`PKG004` flattening guards remain in force; extensions do not prove file formats. |
| Implemented | None | Deterministic ZIP and fresh exact-extraction rebuild for each explicitly selected source package. | `create_archive`, `run_job` and `PKG301` verify each package independently, including flat-layout checks. Multi-document output is released only when every selected package passes required gates. Unselected roots and external delivery are outside that result. |
| Implemented | None | Configured absence of PDF encryption/password protection, forms, JavaScript and embedded attachments as reported by current tools. | `PDF106`–`PDF109`; inaccessible/unrecognized evidence is inconclusive. These checks are limited to `pdfinfo`/`pdfdetach` reporting, not exhaustive object inspection. |
| Partial | Extended | Inspect annotations and arbitrary active content/media. | `PDF210` inspects resolved page-annotation subtypes against an explicit forbidden list; `PDF211` scans reachable dictionaries for forbidden action-name candidates. Opaque streams, orphan objects, arbitrary media and exhaustive active-content absence remain unverified. |
| Partial | Next | Detect located crop/registration-mark candidates against explicit trim boundaries. | `PDF301` identifies supported paired corner strokes and outside-trim registration crosses, with explicit thresholds and exemptions. Raster/outlined marks, unsupported transforms and intent remain unmeasured; candidates stay advisory and do not establish universal crop-mark detection. |

## Preparation and workflow backlog, outside the check-code count

| Status | Priority | Requirement and remaining behavior | Current related behavior |
| --- | --- | --- | --- |
| Partial | Next | Automatic flat hierarchy and command-aware rewriting for a documented literal subset. | `plan_flatten` adds literal import/subimport aliases, constrained subfiles, selected data/font paths and `PKG201` explicit filename overrides to existing inputs/graphics/bibliography/class/style handling. Protected identities and ambiguous/dynamic contexts block unsafe maps. Broader readers and source execution semantics remain engineering work. |
| Implemented | None | Explicit tex-fmt check/diff/application, project exclusions, protected regions and blank-line policies for supported source. | `FMT001`/`FMT002` retain isolated proposals/idempotence; `FMT101`–`FMT103` verify protected bytes, report exclusion scope and enforce formatter/preserve/collapse blank-line policies. The installed formatter exposes no blank-line flag, so this policy is adapter-side. Custom literal semantics remain outside the subset; final PDF comparison is still required. |
| Implemented | None | Assign explicit document roots and independent supplementary source packages; rebuild each solely from its archive. | `workflow.documents` materializes each selected directory/file set with its own root, optional engine and reference PDF. `PKG301` requires all selected packages to verify before output release. Selection is explicit; no automatic assignment or content-adequacy claim is made. |
| Partial | Next | Validate engines and explicit bibliography backends on restricted platform runtimes. | pdfLaTeX/BibTeX workflows and XeLaTeX/LuaLaTeX smoke builds pass live on macOS. `BLD201`/`BLD202` validate fresh bibliography controls and required tool evidence. On macOS, Biber runs from a runtime-owned, sealed single-architecture copy (see [Biber on macOS](workflow.md#biber-on-macos)) and passes live, including a realistic biblatex project; no unconfined fallback exists. Linux Bubblewrap has controlled-command tests but no live Linux run. Windows isolation is unimplemented. |
| Implemented | None | Compare baseline, prepared, archive-rebuilt and explicitly supplied PDFs separately. | `CMP001`–`CMP003` compare page count, extracted text and RGB renders at 144 DPI. Supplied-PDF, repeated-baseline and archive comparisons remain exact; explicit tolerances/reviewed changes apply only to baseline versus prepared. Unavailable required evidence blocks preparation. |
| Partial | Next | Baseline stability, explicit comparison tolerances and reviewed expected-output changes. | `CMP101` checks 2–5 configured independent baselines when repeats are requested. Explicit channel/pixel tolerances and eligible scoped exceptions apply only to baseline versus prepared; archive/reference comparisons cannot be relaxed. Inspection accepts up to 5000 pages, but renders (comparisons and rendered measurements) remain bounded to 300 pages/16 million pixels per page and do not prove semantic equivalence, PDF internals or subpixel fidelity. |
| Implemented | None | Scoped review controls, standalone PDF inspection and explicitly unverified diagnostic exports. | `RPT101`–`RPT103` cover unmatched/ineligible decisions and diagnostic export; CLI pdf check, exact scoped suppressions/accepted exceptions with reasons and original evidence retention exist. Missing evidence, build/isolation failures and archive mismatches cannot be waived. Accepted exceptions remain a distinct outcome. |
| Partial | Extended | Offline HTML reports, located-region diagrams, source before/after views and CI annotations; richer PDF review remains. | Escaped HTML, two-column source diffs, SVG coordinate diagrams, local PDF page links and CI annotations now exist. Embedded page overlays, annotated PDF output and rendered before/after views are not implemented. Reports and diagnostics remain outside source archives. |
| Not implemented | Optional | Web interface using the same core, with service-wide budgets and isolated cancellation. | CLI only. This is an interface/workflow item, not an additional check. See the separate [parallel execution plan](parallelization-plan.md) for scheduling work. |
| Partial | Optional | Reviewed source merging, bibliography inlining, image optimization and externalized-figure preparation. | `TEX202` merges bounded standalone literal input lines; `TEX203` inlines an explicitly selected standalone BibTeX thebibliography file. Both propose copies/diffs under rebuild comparison; include/import merging and biblatex inlining remain unsupported. Image optimization and externalized-figure preparation still have no adapters and must remain opt-in. |
| Intentionally excluded | None | Maintained publisher/venue/year profiles, package catalogs, template registries and scraped submission policies. | Users provide current constraints/reference files; generic checks must not claim publisher certification. |
| Intentionally excluded | None | Implicit online checks/LLM advice, manuscript rewriting, scientific fact checking, venue upload and guaranteed acceptance/anonymity/accessibility. | Optional remote/advice modules require explicit opt-in; human decisions and unsupported semantic claims cannot become deterministic passes. |

## Evidence map and verification boundary

The entries below identify inspected implementations and behavioral tests supporting
the implemented portions. They do not claim a fresh full-suite or live-tool pass
from this documentation audit. Controlled-output unit tests establish adapter
behavior; live tests are a separate gate and require an operational sandbox and
tools. Absence statements above were checked against the implementation modules
and configuration schema, not inferred from an unassigned code.

### Additional-check evidence

The catalogue links each new code to intended-behavior tests, including valid,
violating and unavailable/ambiguous evidence. The implementation/test pairs are:

| Scope | Implementation | Behavioral tests |
| --- | --- | --- |
| Bibliography use and fields | {download}`bibliography_checks.py <../src/latexprep/bibliography_checks.py>` | {download}`test_bibliography_checks.py <../tests/test_bibliography_checks.py>` |
| Build tolerance and package policy | {download}`build_checks.py <../src/latexprep/build_checks.py>` | {download}`test_build_checks.py <../tests/test_build_checks.py>` |
| Actual loaded option registers | {download}`loaded_options.py <../src/latexprep/loaded_options.py>` | {download}`test_loaded_options.py <../tests/test_loaded_options.py>` |
| Manuscript, metadata and floats | {download}`manuscript_checks.py <../src/latexprep/manuscript_checks.py>` | {download}`test_manuscript_checks.py <../tests/test_manuscript_checks.py>` |
| Privacy and submission policies | {download}`submission_checks.py <../src/latexprep/submission_checks.py>` | {download}`test_submission_checks.py <../tests/test_submission_checks.py>` |
| PDF measurements and object policies | {download}`pdf_checks.py <../src/latexprep/pdf_checks.py>` | {download}`test_pdf_checks.py <../tests/test_pdf_checks.py>` |
| Optional online references | {download}`online_checks.py <../src/latexprep/online_checks.py>` | {download}`test_online_checks.py <../tests/test_online_checks.py>` |
| Source transformations | {download}`source_transform.py <../src/latexprep/source_transform.py>` | {download}`test_source_transform.py <../tests/test_source_transform.py>` |
| Bibliography transformations | {download}`bibliography_transform.py <../src/latexprep/bibliography_transform.py>` | {download}`test_bibliography_transform.py <../tests/test_bibliography_transform.py>` |
| Artwork geometry, pixels and glyph programs | {download}`pdf_artwork.py <../src/latexprep/pdf_artwork.py>` | {download}`test_pdf_artwork.py <../tests/test_pdf_artwork.py>` |
| Metadata edits and image metadata | {download}`metadata_privacy.py <../src/latexprep/metadata_privacy.py>` | {download}`test_metadata_privacy.py <../tests/test_metadata_privacy.py>` |
| Author records and PDF structure | {download}`structure_checks.py <../src/latexprep/structure_checks.py>` | {download}`test_structure_checks.py <../tests/test_structure_checks.py>` |
| Review decisions and offline outputs | {download}`reporting.py <../src/latexprep/reporting.py>` | {download}`test_reporting.py <../tests/test_reporting.py>` |
| Independent packages and repeated baselines | {download}`core.py <../src/latexprep/core.py>`, {download}`workflow_options.py <../src/latexprep/workflow_options.py>` | {download}`test_workflow_extensions.py <../tests/test_workflow_extensions.py>` |
| Linux command construction and bibliography backend | {download}`runtime.py <../src/latexprep/runtime.py>` | {download}`test_runtime_backends.py <../tests/test_runtime_backends.py>` |

Online tests use fake transports, not live provider requests. Tool-output fixtures
exercise Poppler/qpdf/MuPDF interpretation without claiming that each optional
tool is installed or works on every PDF. Missing qpdf/mutool, unsupported object
encoding, geometry uncertainty and unmeasured text cannot become successful
checks. These component evidence boundaries also apply to the rows above.

Current macOS live smoke evidence covers two independent flat source packages
with selected bibliography formatting/pruning, private-comment removal, input
merging, repeated baselines and exact archive rebuilds. Separate literal-import,
subfile and input-merge fixtures passed page-count, extracted-text and exact
144-DPI render comparisons with unchanged originals. Actual kernel-option
instrumentation passed against an uninstrumented pdfLaTeX build; XeLaTeX and
LuaLaTeX option/build smoke tests also passed. This does not validate all options
or combinations. qpdf and MuPDF are not installed, and Linux isolation has not run
on Linux. The live Biber test and a realistic biblatex/Biber preparation test
pass on macOS with the sealed Biber copy; they run only with
`FLEDGE_RUN_INTEGRATION=1`.

### Build evidence

{download}`build_project and _log_findings <../src/latexprep/runtime.py>` are exercised by
{download}`BuildTests <../tests/test_runtime.py>`:
`test_failed_tool_never_accepts_partial_pdf`,
`test_final_rerun_request_blocks_success`,
`test_build_copy_has_fresh_output_and_safe_main`,
`test_external_recorder_dependency_blocks_success`, and
`test_build_log_check_codes_match_diagnostics`.
`RuntimeTests.test_missing_backend_never_runs_command` covers unavailable isolation.
{download}`LiveIntegrationTests <../tests/test_integration.py>` has separate
`test_confined_process_cannot_read_or_write_outside_its_workspace` and
`test_confined_process_cannot_connect_to_network` tests.

### Source evidence

{download}`_Inspection and plan_flatten <../src/latexprep/source.py>` are exercised by
{download}`SourceTests <../tests/test_source.py>`:
`test_markers_labels_and_refs_are_heuristics_not_blockers`,
`test_nested_paths_and_collisions_are_rewritten_not_globally_replaced`,
`test_dynamic_and_unsupported_commands_are_flat_blockers`,
`test_relocated_style_cannot_shadow_system_style`,
`test_sidecar_relationship_breakage_is_blocked`, and
`test_flattening_is_idempotent_and_keeps_utf8_bom_crlf`.
New context/adapter cases include
`test_import_contexts_resolve_nested_inputs_and_restore_outer_paths`,
`test_literal_subfile_body_and_parent_reference_are_flattened`,
`test_literal_reader_adapters_preserve_nonfile_arguments`,
`test_subfiles_bibliography_path_and_legacy_mode`, and `test_filename_overrides`.
The separate source-transformation tests cover original-byte retention,
significant-percent/newline preservation, comment-wrapper retention, atomic
blocked plans, recursive input merging and supported/unsupported bibliography
inlining.

### Manuscript evidence

{download}`check_manuscript <../src/latexprep/manuscript.py>` is exercised by
{download}`ManuscriptTests <../tests/test_manuscript.py>`:
`test_expected_document_class_matches_and_mismatch_blocks`,
`test_class_options_report_missing_and_forbidden_values`,
`test_forbidden_packages_check_direct_local_graph_declarations`,
`test_required_abstract_rejects_missing_and_empty_content`,
`test_abstract_word_limits_use_documented_math_and_markup_counting`,
`test_keyword_limits_count_phrases_and_common_environments`,
`test_required_sections_match_literal_headings_not_body_claims`, and
`test_conditional_and_macro_generated_declarations_are_inconclusive`.

### PDF evidence

{download}`inspect_pdf <../src/latexprep/pdf.py>` is exercised by
{download}`PdfConstraintTests <../tests/test_pdf.py>`:
`test_page_limit_boundary_and_overflow`,
`test_expected_dimensions_measure_every_page_with_pdf_point_tolerance`,
`test_page_consistency_uses_full_range_not_just_first_page`,
`test_image_resolution_checks_both_axes_and_repeated_placements`,
`test_font_embedding_violation_changes_status_and_zero_fonts_are_inconclusive`,
`test_forbidden_type3_font_policy_detects_only_type3`,
`test_encryption_policy_uses_explicit_reported_feature`,
`test_forms_policy_detects_acroform_and_xfa`,
`test_javascript_policy_requires_explicit_reported_value`, and
`test_attachments_policy_counts_embedded_files`.
`test_unconfigured_constraints_do_not_report_compliance` and
`test_partial_text_extraction_is_not_a_readability_pass` guard evidence boundaries.

### Bibliography evidence

{download}`check_bibliography and normalize_dois <../src/latexprep/bibliography.py>` are
exercised by {download}`BibliographyTests <../tests/test_bibliography.py>`:
`test_duplicate_keys_and_dois_include_conflicts_without_merging`,
`test_literal_relationships_and_builtin_months_are_checked`,
`test_invalid_plain_dois_and_ambiguous_markup_are_distinct`,
`test_doi_case_is_not_collapsed_for_exact_duplicate_detection`,
`test_malformed_file_is_never_partially_normalized`, and
`test_prefix_edits_preserve_braces_comments_case_and_unknown_fields`.

### Packaging evidence

{download}`import_project, plan_cleanup and create_archive <../src/latexprep/project.py>`
are exercised by {download}`ProjectTests <../tests/test_project.py>`:
`test_unsafe_zip_names_are_rejected`,
`test_normalized_case_and_unicode_collisions_are_rejected`,
`test_zip_symlinks_and_special_files_are_rejected`,
`test_zip_actual_stream_bytes_are_bounded_independently_of_metadata`,
`test_cleanup_retains_dependencies_bibliography_styles_and_unknown_files`, and
`test_archive_bytes_ignore_input_modes_timestamps_and_creation_order`.

Those cleanup tests cover the preliminary debris pass. Preparation's mandatory
dependency selection is a separate stage, described under
[package contents](configuration.md#package-contents); `--no-cleanup` disables
only the preliminary pass.

### Formatting evidence

{download}`format_project <../src/latexprep/formatting.py>` is exercised by
{download}`FormattingTests <../tests/test_formatting.py>`:
`test_only_idempotent_output_is_proposed`,
`test_formatting_differences_preserve_original`, and
`test_resource_and_output_limits_block_even_with_zero_return_code`.
`test_project_exclusions`, `test_protected_regions` and `test_blank_line_policy`
exercise matching, rejected modifications and incomplete protected contexts.
These are adapter-level checks, not proof that arbitrary TeX can be formatted.

### Workflow evidence

{download}`run_job <../src/latexprep/core.py>` is exercised by
{download}`WorkflowTests <../tests/test_workflow.py>`:
`test_flat_bundle_from_exact_extraction_and_serial_equivalence` and
`test_failed_final_build_never_releases_output`.
{download}`compare_pdfs <../src/latexprep/pdf.py>` is exercised by
{download}`PdfTests <../tests/test_pdf.py>`:
`test_rendered_change_is_detected_when_text_matches`,
`test_extracted_text_change_is_detected_when_raster_matches`,
`test_page_count_difference_identifies_missing_pages`, and
`test_missing_renderer_is_inconclusive_and_blocking`.
The separate live acceptance test is
{download}`LiveIntegrationTests.test_nested_sources_bibliography_images_formatting_and_zip_rebuild <../tests/test_integration.py>`.

Before closing a backlog row, implement the stated generic behavior, expose any
required explicit settings, and add meaningful valid/violating/uncertain fixtures.
Measure false positives for visual heuristics before promoting them to blockers.
Update this inventory and the code catalogue together; passing a nearby test or
adding a registry entry alone does not close the requirement.
