# Configuration

Place `fledge.toml` in the input project's directory. Start with only the
constraints that apply to your submission. For example:

```toml
main = "main.tex"
max_pages = 10
jobs = 2

[manuscript_checks]
required_metadata = ["title", "author"]

[online_checks]
online = false
```

Here, `10` is an example limit you must replace with your actual requirement.
The page limit includes references and appendices; no page limit is assumed by
default. Run source checks first, then compile to measure the PDF:

```sh
fledge inspect path/to/paper
fledge check path/to/paper
```

The selected configuration path is
recorded in the report's `execution.config_path`; `null` means built-in defaults
and command-line options were used. The {download}`example configuration <../examples/project-config.toml>`
shows the defaults and a few alternatives. Run `fledge rules` or
`fledge rule TEX001` to discover available codes and their meanings.

## Choose checks

To suppress unfinished-text-marker diagnostics while preserving other defaults,
use `[checks] ignore = ["TEX001"]`. For a narrower source/bibliography selection:

```toml
[checks]
select = ["TEX", "BIB1"]
ignore = ["TEX001", "BIB108"]
```

`select` defaults to `["ALL"]`; `ignore` defaults to `[]`. Supported selectors
are `ALL`, an exact public code such as `TEX001`, a complete family such as
`TEX`, or a family plus numeric prefix such as `BIB1`. A prefix must match at
least one registered code. Selectors are case-sensitive; empty strings, partial
family names such as `TE`, and unknown codes are configuration errors. Use
`select = []` to disable selectable checks.

The most specific matching selector wins, regardless of list order. `ignore`
wins ties. For example, `select = ["ALL", "TEX001"]` with `ignore = ["TEX"]`
keeps the unfinished-text-marker check while excluding the other selectable
`TEX` checks. An exact ignored code overrides a selected family.

Selection filters checks that the command and its existing options would run.
It does not change the command's workflow or invent policy:

- Selecting `PDF` does not supply a page limit, region, font policy, or expected
  metadata. Configure these values separately.
- Selecting `NET` does not authorize requests. Network checks still require
  `--online` or `online_checks.online = true` and their individual options.
- Selecting transformation codes does not authorize changes. Preparation still
  requires the relevant transformation options and writes a separate copy.
- Source confinement, safe transformations, build integrity, required comparison
  and archive verification remain enforced. Execution failures remain visible.

Checks that are disabled do not become passes. A successful report describes
the checks actually selected and configured, subject to required workflow
invariants.

Required guards include dependency resolution (`TEX005`–`TEX007`), bibliography
parsing/key/relationship integrity (`BIB001`, `BIB002`, `BIB004`, `BIB005`),
successful/converged builds, safe requested edits, and PDF/archive preservation
(`CMP`). Ignoring their code does not bypass the corresponding guard. JSON
reports list these codes under
`execution.check_selection.always_enforced_when_applicable`; this is not a claim
that every listed guard ran in a source-only command. Shared parsers may still
collect optional observations during required work, but deselected observations
are omitted before determining the outcome.

## Discovery and precedence

The `fledge` names below are preferred. The earlier `latex-prep` file names and
`[tool.latex-prep]` table still work, so there is no need to rename existing
settings files.

Discovery starts at the original input directory. For a ZIP or PDF input, it
starts at that file's parent directory. Configuration inside a ZIP is never
loaded automatically.

The nearest directory containing a configuration wins. Within one directory,
the priority is:

1. `.fledge.toml`
2. `fledge.toml`
3. `.latex-prep.toml` (legacy)
4. `latex-prep.toml` (legacy)
5. `pyproject.toml` containing `[tool.fledge]` or `[tool.latex-prep]` (legacy)

A `pyproject.toml` with both tables is a configuration error; keep one. An
unrelated `pyproject.toml` is skipped. Discovery checks a repository root
(a directory containing `.git` or `.hg`) and then stops. It stops before a home
directory or filesystem-root ancestor, so there is no user-home fallback. When
the input itself is the home directory or filesystem root, only that starting
directory is checked. Configuration files are not merged.

`--config path/to/settings.toml` uses exactly that file and skips discovery.
Relative paths inside configuration, such as template references, resolve from
the configuration file's directory. `--isolated` skips discovery and uses the
defaults plus explicit CLI options; it cannot be combined with `--config`.
Explicit CLI options override the chosen file, including negative options such
as `--no-format` and `--offline`.

```sh
fledge inspect paper --config review.toml
fledge inspect paper --isolated --jobs 1
```

Invalid TOML, unknown settings, invalid option types, and unknown selectors
produce configuration errors before the job starts. Invalid discovered files
are not silently replaced by an ancestor configuration.

## Use pyproject.toml

Only the `[tool.fledge]` table (or the legacy `[tool.latex-prep]`) and its
subtables are loaded; unrelated project and tool settings are ignored. An
explicitly supplied `pyproject.toml` must contain exactly one of these tables.

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

`prepare` includes the selected document's needed project inputs by default,
using complete build traces and its supported literal dependency graph. Unrelated
files are omitted only from the staging copy; `--no-cleanup` does not retain them.

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
[workflow reference](workflow.md#how-package-contents-are-selected) describes the
selection and verification sequence.

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
