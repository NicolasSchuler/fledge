# Limitations

Fledge checks what can be checked mechanically and says when it cannot decide.
This page collects what it does not cover, so the step-by-step guides can stay
direct. No Fledge result guarantees that a venue will accept a submission.

## Platforms and tools

- **macOS is the main tested platform.** Builds, Biber, multi-document packages
  and a corpus of realistic projects pass live on the macOS development host.
- **The macOS installer's Homebrew and MacTeX steps** are tested with controlled
  commands and dry runs, not with a live installation.
- **Linux** needs a manual installation and Bubblewrap with working user
  namespaces. Fledge's live integration tests run weekly in CI under Bubblewrap
  on a GitHub-hosted Ubuntu 24.04 runner, with TeX Live from Ubuntu's packages.
  Other distributions and TeX installations are not tested.
- **Native Windows is not supported.** Fledge relies on file locking that only
  macOS and Linux provide, so no command starts there, and Windows has no build
  sandbox either. Use WSL 2 with the Linux instructions in
  [installation](installation.md#windows); that route has not been tested live.
- **Some commands need no TeX.** On macOS and Linux, `inspect`, `bib check`,
  `submission-text`, `init`, `rules` and `rule` need only the Python package; see
  [tools](installation.md#tools).
- **No unsandboxed fallback exists.** If the sandbox cannot start, for example
  inside another sandbox or under a restrictive host policy, builds cannot run
  and the run ends with exit code 4.
- **Optional `qpdf` and MuPDF checks are mostly tested against recorded tool
  output.** The weekly live tests on macOS and Linux install both tools and run
  four of their checks against them: the catalog language (`PDF209`), forbidden
  annotations (`PDF210`), stroke width (`PDF213`) and color spaces (`PDF214`).
  The other checks that read `qpdf` or MuPDF output, such as the tagged-structure
  and artwork checks, are not tested against the live tools. If either tool is
  missing, its checks are reported as incomplete.
- **The chktex lint (`CHK001`) is only lightly tested on Linux.** It has run live
  under the macOS sandbox; its Bubblewrap configuration on Linux has not yet been
  confirmed in a live run. See [lint with chktex](chktex.md).
- **Builds never generate bitmap fonts.** The build environment disables
  `mktexpk`, so a font that exists only as METAFONT source, for example
  T1-encoded Computer Modern without the `cm-super` package, ends the build
  with a missing-font finding instead of a PDF with bitmap glyphs. Install the
  Type 1 fonts (`tlmgr install cm-super`) or use a font that has them.
- **Builds see neither personal TeX trees nor personal fonts.** A class or package
  in `TEXMFHOME` (such as `~/Library/texmf` or `~/texmf`) or in `TEXMFLOCAL`
  (`/usr/local/texlive/texmf-local`), and a font installed only for your account
  (`~/Library/Fonts`, `~/.fonts`), are invisible to the build, as they would be to
  the receiving service. Put such files into the project. Fonts in the system font
  folders are visible, and `BLD108` warns that an archive using them will not rebuild
  on a computer without them.
- **Only pdfLaTeX, XeLaTeX and LuaLaTeX build documents.** The DVI route (`latex`
  with `dvips`), upLaTeX and pLaTeX, Tectonic and ConTeXt are not supported. The
  engine comes from `--engine`, the `engine` setting or a `% !TEX program` comment,
  never from a project `latexmkrc`, which is not executed.
- **Glossaries, lists of acronyms and nomenclatures are built with makeindex,**
  using the style file that the `glossaries` package records and `nomencl`'s style.
  Glossaries that need `xindy` or `bib2gls`, and glossary types other than the main
  glossary and the acronyms, are not built. For `xindy`, run makeglossaries locally and
  supply the resulting `.gls` or `.acr` beside the main file. The build reads the supplied
  list as-is, and preparation ships it as an input. If the document wrote entries but
  the list is missing or empty, `TEX012` reports that the PDF lacks the list.
- **The memory of a PDF measurement follows the size of the PDF, not its images.**
  `pdfimages -list` opens every image, and a JPEG 2000 image takes far more memory
  to open than its compressed size suggests. A small PDF with a very large image of
  that kind can exceed the limit of 64 MiB plus 2 MiB for each MiB of the PDF; the
  run then ends with exit code 4 and a message that names the limit.
- **A double quote in the temporary directory path** makes `latexmk` refuse to
  build. Fledge reports the failed build; point `TMPDIR` at a path without
  quotes. Spaces and non-ASCII characters in that path are fine.
- **LuaLaTeX builds font caches once per job.** On macOS the first LuaLaTeX build
  of a job lets `luaotfload` build its font database once, which takes a few
  seconds, and every later build of the job reuses a read-only copy; each build
  still caches the fonts it loads that the warm-up did not. Under Linux Bubblewrap
  every LuaLaTeX build still builds the database itself, so raise `timeout_seconds`
  if a build that loads many fonts is stopped.

## Venue rules

Fledge maintains no publisher, venue or year profiles. Page limits, page sizes,
required classes, identity terms and similar policies come from you. The presets
(`arxiv`, `anonymous-review`, `camera-ready`) switch on generic checks and leave
venue values as placeholders; they are starting points, not certified policies.
Read the venue's current instructions and configure what they require.

## Questions that need human judgment

These questions need a person, even when a related check passes:

- whether declarations (ethics, data availability, AI use) are adequate and
  accurate; Fledge checks only that a required section is present and nonempty;
- whether a submission is anonymous; identity checks find only the terms and
  hints you configure;
- whether figures and the PDF are accessible; alternate-text and tag checks do not
  establish description quality, reading order or PDF/UA conformance;
- whether the scientific content, citations and wording are right; Fledge never
  rewrites them.

## Source and PDF coverage

- The source scanner understands literal TeX commands, not arbitrary macros.
  Dynamic file paths, font-family lookup and legacy `subfiles` preamble behavior
  are unsupported; flattening stops instead of guessing.
- Build entry filenames must use ASCII letters, digits, underscores, dots or
  hyphens and end in `.tex`. A source file that is not UTF-8, such as Latin-1, is read
  for the checks with its other bytes taken as Windows-1252, so findings may quote its
  accented words differently from the paper; it is never changed, so a `prepare`
  that asks for an edit to it, such as comment removal, is blocked, and the privacy
  scans of its text stay incomplete (`TEX328`). In such a file a label, citation key
  or file name with letters outside ASCII is compared as Fledge reads it, not as the
  bytes TeX compares, so `inspect` can accept a name that the build rejects. UTF-16
  and other text with NUL bytes, and text that may be in Shift-JIS, GBK or Big5, is not
  read (`TEX301`).
- PDF comparison checks page count, extracted text and 144-DPI renders. A document
  with a page too large for that resolution, such as an A0 poster, is rendered at a
  lower one, down to 36 DPI, which the report names. Details below the resolution
  used and differences inside PDF internals can go unnoticed. Rendering stops at 300
  pages.
- Figure metadata is reported, never removed. Fledge does not rewrite figure
  files, and `suppress_included_pdf_information` (`TEX204`) keeps only the
  information dictionary and file name of included PDFs out of the built PDF:
  pdfLaTeX and LuaLaTeX still copy each included page's XMP and application data
  (PieceInfo). Re-export a figure whose metadata must not be shared.
- HTML captures at most eight affected page pairs and 12 MiB of PNG data.
  Visual previews explain recorded differences, not every page or PDF object.
- Submission-text export supports literal declarations and common formatting.
  Custom macros, citations, references and complex math remain unresolved and
  require manual conversion; no TeX macro expansion is attempted.
- Drawing checks handle a limited subset of PDF graphics; curves, complex clips,
  masks, OCR and automatic figure segmentation are not covered.
- Online checks depend on the services' current data. Their tests use simulated
  responses, so provider availability and coverage are not verified.

## Not yet implemented

Image optimization, externalized-figure preparation, annotated PDF output,
Windows isolation and a web interface. The
[check backlog](check-backlog.md) separates partial implementations, missing
validation and intentional exclusions in detail.
