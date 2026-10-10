# Limitations

Fledge checks what can be checked mechanically and says when it cannot decide.
This page collects what it does not cover, so the step-by-step guides can stay
direct. No Fledge result guarantees that a venue will accept a submission.

## Platforms and tools

- **macOS is the tested platform.** Builds, Biber, multi-document packages and a
  corpus of realistic projects pass live on the macOS development host.
- **The macOS installer's Homebrew and MacTeX steps** are tested with controlled
  commands and dry runs, not with a live installation.
- **Linux** needs a manual installation and Bubblewrap with working user
  namespaces. Its sandbox has controlled-command tests but has not run on a live
  Linux host.
- **Windows** has no build sandbox, so `check` and `prepare` cannot build there.
  `inspect` and `bib check` need only Python.
- **No unsandboxed fallback exists.** If the sandbox cannot start, for example
  inside another sandbox or under a restrictive host policy, builds are blocked.
- **Optional `qpdf` and MuPDF checks** are tested against recorded tool output,
  not against the live tools. If either tool is missing, its checks are
  reported as incomplete.

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
  hyphens and end in `.tex`. Source analysis requires UTF-8 text.
- PDF comparison checks page count, extracted text and 144-DPI renders. Details
  below that resolution and differences inside PDF internals can go unnoticed.
  Rendering stops at 300 pages.
- Drawing checks handle a limited subset of PDF graphics; curves, complex clips,
  masks, OCR and automatic figure segmentation are not covered.
- Online checks depend on the services' current data. Their tests use simulated
  responses, so provider availability and coverage are not verified.

## Not yet implemented

Image optimization, externalized-figure preparation, annotated PDF pages,
rendered before/after views, Windows isolation and a web interface. The
[check backlog](check-backlog.md) separates partial implementations, missing
validation and intentional exclusions in detail.
