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
prefix such as `BIB1`; it must match at least one registered code. The name of one
check stands for its code: the dotted name that reports show (`source.edit_marker`)
and the hyphenated name of earlier releases (`source-edit-marker`) both select
`TEX001`, in the file and on the command line, and `fledge rule` explains a check
by any of its names. Selectors are case-sensitive, and empty strings, partial
family names such as `TE` and unknown codes are errors. `select = []` disables
every selectable check.

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

Some default checks concern submission readiness: a note, listed only with
`--show-passed`, when no generated `.bbl` is shipped for services that do not
run BibTeX or Biber, such as arXiv (`TEX009`;
`submission_checks.require_bbl = true` makes it a warning), packages
declared before `hyperref` that must follow it (`TEX010`), EPS figures in a PDF
build (`TEX011`) and private-note markers in comments (`PRV007`).
Related opt-in policies check review line numbers (`MAN017`), figure color
spaces (`PDF313`), the TeX Live release (`BLD105`), whether a shipped `.bbl`
covers every cited key (`BIB109`) and whether a shipped biblatex `.bbl` has the
format version the receiving service expects (`BIB110`,
`submission_checks.biblatex_bbl_format_version`).

## Discovery and precedence

Discovery starts at the input directory, or at the parent directory of a ZIP or
PDF input; configuration inside a ZIP is never loaded. The nearest directory
with a configuration file wins. Within one directory the first of these that
exists is used, and the others are ignored:

1. `.fledge.toml`
2. `fledge.toml`
3. `.latex-prep.toml`
4. `latex-prep.toml`
5. `pyproject.toml` containing `[tool.fledge]` (or the older `[tool.latex-prep]`)

The two `latex-prep` file names are the older spellings; see
[migrating from latex-prep](installation.md#migrating-from-latex-prep). They rank
above `pyproject.toml`, so a leftover `latex-prep.toml` shadows a `[tool.fledge]`
table beside it. A `pyproject.toml` without a `[tool.fledge]` or `[tool.latex-prep]`
table is skipped, but one that is not valid TOML stops the run with "Cannot read
settings": Fledge cannot know that the file was not meant for it. Repair the file,
or choose a settings file with `--config` or skip discovery with `--isolated`.
Discovery stops at a repository root (a directory containing `.git` or `.hg`).

Discovery never climbs into your home directory, the system temporary directory or
the filesystem root, so there is no user-wide configuration, and a file another user
left in `/tmp` cannot configure a project that lies inside it. It reads such a
directory only when you named it yourself: when the input is that directory, or is a
ZIP or PDF file lying directly in it (`fledge inspect /tmp/paper.zip` reads
`/tmp/fledge.toml`). Nothing above that directory is read, and a project folder such as
`/tmp/paper` does not read the configuration of `/tmp`. Only one file is used; files from
different directories are not merged.

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

## Size, time and memory limits

Fledge bounds what one job may import, write and run. The defaults suit ordinary
papers; raise a limit when an error message names it.

| Setting | Default | What it bounds |
| --- | --- | --- |
| `max_files` | 10000 | Files and folders together in an imported project or ZIP |
| `max_depth` | 32 | Folder levels in an imported project or ZIP |
| `max_bytes` | 268435456 (256 MiB) | The default of `max_import_bytes` and `max_generated_file_bytes` below |
| `max_import_bytes` | `max_bytes` | Combined size of all files in a project folder or ZIP import, of the copy each build makes of the sources, and of the prepared archive when it is extracted again to verify it |
| `max_generated_file_bytes` | `max_bytes` | Size of any one file a tool may write, and the largest PDF or image a tool opens |
| `timeout_seconds` | 120 | One tool command (`--timeout-seconds`) |
| `job_timeout_seconds` | 600 | The whole job (`--job-timeout-seconds`) |
| `build_memory_mb` | 1024 | Memory of one build command |
| `memory_mb` | 2048 | Memory shared by all running tools; it must allow `build_memory_mb` plus 128 MiB for the coordinator |
| `max_temporary_bytes` | 2147483648 (2 GiB) | Temporary data of the whole job |

`max_bytes` is the one number that older settings files use for both byte limits,
and it keeps working: set alone, it gives `max_import_bytes` and
`max_generated_file_bytes` the same value. An explicit `max_import_bytes` or
`max_generated_file_bytes` wins for its own purpose, and the other limit keeps
following `max_bytes`. The two limits answer different questions, so a large
data-heavy project can raise the import total without letting a runaway tool write a
file of that size:

```toml
max_bytes = 268435456                  # default for both limits below: 256 MiB
max_import_bytes = 1073741824          # projects, ZIP files and build copies up to 1 GiB
max_generated_file_bytes = 268435456   # no tool file above 256 MiB
```

A tool workspace may hold at most four times `max_generated_file_bytes` in total, and
that includes the copy of your sources. Fledge therefore rejects settings in which
`max_import_bytes` is more than four times `max_generated_file_bytes`, and the error
names both. The example is the largest import total that 256 MiB per file allows. A limit
you leave unset follows `max_bytes`, so lowering `max_generated_file_bytes` alone also
needs a lower `max_import_bytes` (or a lower `max_bytes`). A job that exceeds a limit
stops with a finding that names it. Reports record all three byte settings under
`settings`; `max_import_bytes` and `max_generated_file_bytes` are `null` when you did not
set them.

## Similar names, different checks

A few settings look alike but belong to different checks. They are independent: set
whichever applies, or both with different values.

- **`min_stroke_width_pt`** exists in two places. Under `[pdf_checks]` it enables
  `PDF213`, which reads the stroke width that MuPDF reports for each drawn stroke and
  scales it by the placement transform, but only where that transform is uniform;
  hairlines and nonuniform transforms are not measured. Under
  `[rendered_pdf_artwork]` and `[included_figure_artwork]` it enables `PDF309`, which
  measures the thickness of each straight segment perpendicular to its direction after
  a full affine transform, so it also catches a figure that was stretched in one
  direction (curves and text still need a uniform transform). `rendered_pdf_artwork` applies it to the final PDF and
  `included_figure_artwork` to each included PDF figure.
- **`require_embedded_fonts`** is a top-level setting that requires every font in the
  final PDF to be embedded (`PDF104`). **`require_embedded_figure_fonts`**, under
  `[pdf_checks]`, applies the same requirement to each PDF figure you include
  (`PDF212`).

## Renamed settings

Three settings have clearer names. Their former names are still accepted, but
a settings file or preset layer must not use both spellings of one setting.
Reports record the current names.

| Former name | Current name |
| --- | --- |
| `format` | `apply_formatting` (the `--format` option is unchanged) |
| `pdf_artwork` | `rendered_pdf_artwork`: artwork checks of the final PDF |
| `figure_artwork` | `included_figure_artwork`: artwork checks of each included PDF figure |

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
- `online_contact_email` adds `mailto:` to the `User-Agent` header of requests to
  `api.crossref.org` only, which lets Crossref use its polite pool. Requests to
  every other host, such as a DOI resolver or a linked site, never carry it. No
  finding contains it, and the settings that the JSON report, the HTML report and the
  diagnostics bundle record show `"<set>"` instead of the address, so a shared report
  does not reveal it.
- A normal `prepare` checks the final archive extraction once, after
  transformations, so it inspects the references in the prepared package; if
  preparation stops earlier, an info `online.not_reached` note says the selected
  checks did not run. `inspect`, `check`, `bib check` and `prepare --dry-run` check
  the initial snapshot. Preparation spends no second request budget on the
  baseline.
- Remote failures remain advisory evidence, never successful validation or
  permission to apply automatic corrections.
