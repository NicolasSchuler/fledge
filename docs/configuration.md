# Configuration

Settings live in `fledge.toml` in the project directory. Configure only the
constraints that apply to your submission; nothing is assumed by default. For
example:

```toml
main = "main.tex"
max_pages = 10  # replace with your venue's limit, including references and appendices

[manuscript_checks]
required_metadata = ["title", "author"]
```

`fledge init /path/to/paper` writes a commented starter file. The report's
`execution.config_path` records which file was used (`null` means none). The
{download}`example configuration <../examples/project-config.toml>` shows the
defaults and a few alternatives. `fledge COMMAND --help` lists the common options
first and the advanced ones after them.

## Start from a preset

A preset is a commented settings file that switches on existing generic checks
for a common situation. Each one is a starting point, not a certification of
venue compliance: values such as the page limit, ZIP size, identity terms and
image resolution stay as placeholders for you to fill in.

| Preset | Switches on |
| --- | --- |
| `arxiv` | Embedded fonts; private-comment, secret, shell-escape and unused-file scans; a required generated `.bbl` that covers every cited key; removes private comments from the prepared copy. Placeholders: ZIP size, filename characters, TeX Live year |
| `anonymous-review` | Embedded fonts, no attachments; identity-hint, private-comment, secret and unused-file scans; layout-manipulation and float-structure checks. Placeholders: page limit, identity terms, image-metadata terms, ZIP size, review line numbers |
| `camera-ready` | Embedded fonts without Type 3, consistent page size, no encryption, JavaScript or attachments; title and author metadata, ORCID, float structure and hygiene scans. Placeholders: page limit and size, document class, image resolution, declarations, PDF metadata, forbidden line numbers, TeX Live year, figure color space |

```sh
fledge init --list
fledge init /path/to/paper --preset arxiv
fledge check /path/to/paper --preset arxiv
```

`fledge init [DIRECTORY]` writes `fledge.toml` in the directory (default: the
current one): the preset's text with `--preset`, otherwise a minimal starter. It
refuses to replace an existing `fledge.toml` unless you add `--force`, and warns
when a higher-priority file such as `.fledge.toml` would shadow the new one.

`--preset NAME` on `inspect`, `check` and `prepare` applies a preset without
writing a file. The preset is the lowest layer: your settings file overrides it
and command-line options override both. Tables merge key by key, so a file's
`[submission_checks]` replaces only the keys it names and the preset's other keys
stay in effect. Top-level keys and arrays replace the preset's value outright.
The JSON report records the preset in `execution.preset`. The `arxiv` preset's
comment removal (`source_transforms.comment_policy = "private"`) affects only
`prepare`.

## Choose checks

Every check has a code, and [the catalogue](checks.md) shows which run by
default and which are opt-in. Filter them in the configuration file or on the
command line:

```toml
[checks]
select = ["TEX", "BIB1"]
ignore = ["TEX001", "BIB108"]
```

```sh
fledge inspect /path/to/paper --ignore TEX001
fledge check /path/to/paper --select TEX --select BIB1
```

`--select` replaces `checks.select` from the file; `--ignore` adds to
`checks.ignore`. Both are repeatable and available on `inspect`, `check` and
`prepare`. An unknown selector stops the run as a request error (exit code 4) and
suggests the closest codes.

`select` defaults to `["ALL"]` and `ignore` to `[]`. A selector is `ALL`, an
exact code such as `TEX001`, a family such as `TEX`, or a family plus numeric
prefix such as `BIB1`; it must match at least one registered code. Selectors are
case-sensitive, and empty strings, partial family names such as `TE` and unknown
codes are errors. `select = []` disables every selectable check.

The most specific matching selector wins, regardless of list order, and `ignore`
wins ties. For example, `select = ["ALL", "TEX001"]` with `ignore = ["TEX"]` keeps
the TODO-marker check while excluding the other `TEX` checks.

Selection only filters what the command would run. It does not invent policy:

- Selecting `PDF` supplies no page limit, region, font policy or expected
  metadata; configure those values.
- Selecting `NET` grants no network access; online checks still need
  `--online` and their individual switches.
- Selecting transformation codes changes nothing; edits still need their
  options.
- [Always-enforced guards](checks.md#always-enforced) stay on, and ignoring
  their code does not bypass them. JSON reports list them under
  `execution.check_selection.always_enforced_when_applicable`.

Checks that are off do not become passes. Terminal reports end with a short
**Not checked** line naming the opt-in areas that did not run, so a clean result
is not mistaken for a full audit. JSON reports list the same areas in
`execution.not_checked`, each with its codes and the settings that enable them.

Some default checks concern submission readiness: a note when no generated
`.bbl` is shipped for services that do not run BibTeX or Biber, such as arXiv
(`TEX009`; `submission_checks.require_bbl = true` makes it a warning), packages
declared before `hyperref` that must follow it (`TEX010`), EPS figures in a PDF
build (`TEX011`) and private-note markers in comments (`PRV007`).
Related opt-in policies check review line numbers (`MAN017`), figure color
spaces (`PDF313`), the TeX Live release (`BLD105`) and whether a shipped `.bbl`
covers every cited key (`BIB109`).

## Discovery and precedence

Discovery starts at the input directory, or at the parent directory of a ZIP or
PDF input; configuration inside a ZIP is never loaded. The nearest directory
with a configuration file wins. Within one directory the order is:

1. `.fledge.toml`
2. `fledge.toml`
3. `pyproject.toml` containing `[tool.fledge]`

Older `latex-prep` names are read after these; see
[migrating from latex-prep](installation.md#migrating-from-latex-prep). An
unrelated `pyproject.toml` is skipped. Discovery stops at a repository root (a
directory containing `.git` or `.hg`) and never climbs into your home directory
or the filesystem root, so there is no user-wide configuration. Only one file is
used; files from different directories are not merged.

Precedence, from highest to lowest: command-line options, the selected settings
file, a `--preset`, and the built-in defaults. Negative options such as
`--no-format` and `--offline` override the file too.

```sh
fledge inspect paper --config review.toml
fledge inspect paper --isolated --jobs 1
```

`--config FILE` uses exactly that file and skips discovery. `--isolated` skips
discovery and uses the defaults plus command-line options; it cannot be combined
with `--config`. Relative paths inside a file, such as template references,
resolve from that file's directory. Invalid TOML, unknown settings, wrong types
and unknown selectors stop the run before it starts. The message names the file
or option the value came from and suggests the closest valid setting. An invalid file is never silently replaced by an
ancestor's.

## Use pyproject.toml

Only the `[tool.fledge]` table and its subtables are loaded; unrelated project and tool settings are ignored. An
explicitly supplied `pyproject.toml` must contain it.

```toml
[tool.fledge]
jobs = 2
max_pages = 10

[tool.fledge.checks]
select = ["ALL"]
ignore = ["TEX001"]

[tool.fledge.manuscript_checks]
required_metadata = ["title", "author"]
```

For a dedicated settings file, omit the `tool.fledge` prefix:

```toml
jobs = 2
max_pages = 10

[checks]
select = ["ALL"]
ignore = ["TEX001"]

[manuscript_checks]
required_metadata = ["title", "author"]
```

Thresholds, identity terms, expected values, transformations, and network options
keep their existing typed tables. See [the check catalogue](checks.md),
{download}`constraint settings <../examples/settings.toml>`, and
{download}`additional check settings <../examples/checks.toml>` for supported policies.

## Package contents

`prepare` ships the inputs the build needs, using the build trace and the literal
dependency graph. Unrelated files are left out of the copy, and `--no-cleanup`
does not retain them.

List extra files that must ship even when the build never reads them:

```toml
[package]
include = ["extras/cover-letter.txt", "data/results.csv"]
```

Entries are exact paths relative to the input project (or the selected document's
source directory); globs are rejected. They follow the flat filename map, so keep
them source-relative with `--layout flat`. `include` is a packaging choice, not a
check: it ships the files whatever the check selection, and a listed file that is
missing or unsafe blocks preparation.

To also verify that a file is present and has the expected kind, use
`submission_checks.required_deliverables` (`PKG104`). Its files, and
project-side `template_references` files, are still retained for compatibility,
but `[package] include` is the recommended way to ship extras.

```toml
[submission_checks]
required_deliverables = { "extras/cover-letter.txt" = "text", "data/results.csv" = "file" }
# template_references = { "styles/template.cls" = "../approved/template.cls" }
```

Supported deliverable kinds are `file`, `text`, `pdf`, and `zip`; presence/type
checks do not assess content adequacy. External template-reference paths resolve
from the settings file's directory.

For `workflow.documents`, `include` globs restrict the candidate inputs available
to that document. They do not force unused matches into the archive; include the
dependencies and any listed extras in that candidate set. Omitting the patterns
makes all files in the source directory candidates, not automatic deliverables.
A missing or incomplete build trace blocks preparation. The
[workflow reference](workflow-reference.md#how-package-contents-are-selected)
describes the selection and verification sequence.

## Select online evidence deliberately

Offline is the default. An individual remote check and network permission are
separate choices:

```toml
[online_checks]
online = true
online_metadata = true
# online_contact_email = "you@example.org"
```

- `--online` and `--offline` on `inspect`, `check`, `prepare` and `bib check`
  override permission only; they do not select checks. A selected check without
  permission is reported as skipped, and with no check selected no request is made.
- Requests disclose DOI/URL targets to the destination and selected reference
  titles to Crossref for searches. Manuscript and bibliography files are not
  uploaded.
- Entries come from the `.bib` resources the selected document declares, so an
  unused backup copy spends no requests; without a resolvable declaration every
  `.bib` file in the project is used.
- `online_max_requests` (default 40, at most 1000) is one budget shared by all
  selected online checks of a document. Entries beyond it report `request_limit`
  and are never treated as verified.
- `online_contact_email` adds `mailto:` to the request's `User-Agent` header, which
  lets Crossref use its polite pool. No finding contains it, but the JSON report
  records it with the other effective settings.
- A normal `prepare` checks the final archive extraction once, after
  transformations, so it inspects the references in the prepared package; if
  preparation stops earlier, an info `online.not_reached` note says the selected
  checks did not run. `inspect`, `check`, `bib check` and `prepare --dry-run` check
  the initial snapshot. Preparation spends no second request budget on the
  baseline.
- Remote failures remain advisory evidence, never successful validation or
  permission to apply automatic corrections.
