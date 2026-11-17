# Frequently asked questions

## Does Fledge change my project?

No. Every command reads a snapshot of your folder or ZIP, and `prepare` writes
its result into a separate output folder outside the project. Edits you ask
for, such as `--layout flat` or comment removal, apply only to that copy.

## Does a pass mean the venue will accept my paper?

No. A pass covers the checks that ran: the ZIP contains what the build needed,
rebuilds from a fresh extraction and matches your PDF, and the checks you
configured found nothing. Fledge maintains no venue rules, so a page limit or a
required class is checked only once you set it. The `Not checked` line of a
passing run names what did not run. See [limitations](limitations.md).

## What do the exit codes mean?

| Exit code | Meaning |
| --- | --- |
| 0 | Passed (or a `--dry-run` plan) |
| 1 | Passed with warnings that need your review; the submission was written |
| 2 | Passed because of exceptions you accepted in the settings file |
| 3 | Blocked: the paper needs a fix |
| 4 | Fledge could not check the paper: a missing tool, the sandbox, a time limit, an unreadable input or invalid settings |
| 64 | The command line is invalid |
| 70 | A defect in Fledge; please report it |
| 130 | Interrupted |

[Reports and exit codes](reports.md#exit-codes) explains each one.

## My run passed but exited with code 1. Is the submission usable?

Yes, once you have looked at the warnings. A warning, such as a TODO marker or
an unused bibliography entry, does not stop the release, but nobody has decided
on it yet; the nonzero code makes scripts and CI notice.

## How do I choose XeLaTeX or LuaLaTeX?

Put `% !TEX program = xelatex` (or `lualatex`) among the first lines of the main
file, as most editors do, set `engine = "xelatex"` in `fledge.toml`, or pass
`--engine xelatex`. Without any of them, Fledge builds with pdfLaTeX. `BLD106`
records which engine built the document and why. See
[choose the TeX engine](configuration.md#choose-the-tex-engine).

## It builds in my editor. Why does it fail in Fledge?

Fledge builds in a sandbox that is stricter than your editor: no shell escape,
no bitmap font generation, and no access to your personal TeX tree or to fonts
installed only for your account, which a receiving service would not have
either. Packages such as
minted or svg, EPS figures, a missing `cm-super` and personal fonts are the usual
causes; the first `BLD009` finding names the cause and the fix. See
[build failed](troubleshooting.md#build-failed).

## Should I ship the `.bbl` file?

Only if the service does not run BibTeX or Biber itself. Most do, arXiv
included, so by default `prepare` ships your `.bib` files and leaves a
regenerated `.bbl` out. If your venue needs the `.bbl`, set
`submission_checks.venue_runs_bibliography_tools = false` and `prepare` ships the
one its verified build generated. Index and glossary files ship either way. See
[generated files](preparation-options.md#generated-files).

## The output folder already exists. How do I run again?

Add `--force`. It replaces an output that Fledge wrote there earlier, and only
once the new run has passed; any other existing folder or file is refused. An
existing empty folder works without `--force`.

## Can I use my Overleaf project?

Yes. Download the project's source as a ZIP from Overleaf and pass the ZIP file
instead of a folder. Fledge compiles with your local TeX installation, which may
be a different TeX Live release than Overleaf's.

## Does Fledge use the network?

Not unless you ask. Online reference checks need both an individual switch under
`[online_checks]` and `--online`; builds never have network access. See
[online evidence](configuration.md#select-online-evidence-deliberately).

## What can I do about a finding I disagree with?

It depends on its severity:

- A **suggestion**: put `% fledge: ignore CODE` at the end of its line or on the
  line before it ([suggestions](reports.md#suggestions)).
- A **warning**: record a suppression with a written reason for that location
  under `[[reporting.suppressions]]`, which marks it as reviewed, or switch the
  check off with `[checks] ignore = ["CODE"]`.
- An **error** of a check you switched on that should not stop the run: list
  its code under `[checks] advisory`, and it is reported as a warning
  ([configuration](configuration.md#report-a-check-without-letting-it-block)).
- An **error**: fix it. A few eligible failures, such as an intended visual
  change, can be accepted with a reason under `[[reporting.accepted_exceptions]]`;
  the run then exits with 2. See
  [preservation and explicit exceptions](preparation-options.md#preservation-and-explicit-exceptions).

## Does Fledge run on Windows?

`inspect` and `bib check` run anywhere Python does. Building needs a sandbox,
which native Windows lacks, so `check` and `prepare` need WSL 2 with the Linux
instructions; see [installation](installation.md#windows).

## Is Fledge on PyPI?

Not yet; a release there as `fledge-latex` is planned. Until then, install a
release tag from GitHub as described in
[installation](installation.md#install-with-pipx-or-uv). The PyPI package named
`fledge` is an unrelated project.

## Where do I report a problem?

Keep the command, the JSON report (`--report report.json`) and, for an
unexplained wait or tool failure, the diagnostic log of a rerun with `--debug`.
[Getting help](troubleshooting.md#getting-help) lists what to include. Fledge
sends nothing automatically.
