# Development and documentation

Read the [hosted documentation](https://nicolasschuler.github.io/fledge/) without
installing Fledge or its documentation tools.

Work from a source checkout or extracted source distribution. Keep private papers
outside the repository or in `.local/`. Review the paths to be staged: generated
outputs and environments are ignored, while manuscript PDFs, images, and other
source assets remain trackable. Fledge is released under the MIT License (`LICENSE`).

## Install from source

Python 3.11 or later is required. Create a virtual environment and install the
checkout in editable mode with the development tools:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,docs,build]'
fledge inspect examples/nested-paper
fledge check examples/nested-paper --main main.tex
fledge prepare examples/nested-paper --main main.tex --layout flat \
  --output /tmp/prepared-paper
```

`uv tool install .` or another package manager also exposes the `fledge`
executable.

The `dev` extra provides pytest with pytest-xdist, Ruff and ty; `docs` provides Sphinx and MyST; `build`
provides the Python distribution builder. None installs TeX or PDF tools.

## Build the documentation

From the source root, install the documentation extra and build HTML:

```sh
python -m pip install -e '.[docs]'
python -m sphinx -n -W --keep-going -b html docs docs/_build/html
```

Open `docs/_build/html/index.html` in a browser. The build uses Sphinx's built-in
Alabaster theme and MyST Markdown, with warnings treated as errors and no warning
suppression. This build needs no TeX, online checks, or external theme. Its local
source/test/example links are copied into the HTML output as downloads. The
HTML build does not validate the current availability of external websites.

The [Sphinx Markdown documentation](https://www.sphinx-doc.org/en/master/usage/markdown.html)
describes enabling MyST, and the
[MyST cross-reference documentation](https://myst-parser.readthedocs.io/en/latest/syntax/cross-referencing.html)
describes document and download links.

## Publish the documentation

The [Deploy documentation workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/docs.yml)
publishes this site to GitHub Pages from `main` when documentation or its included
source, tests, examples, scripts, or project metadata change. It installs the
`docs` extra and runs the strict Sphinx build above, then deploys the HTML in a
separate job only after the build succeeds. Only `main` can publish.

The repository's **Settings → Pages → Build and deployment → Source** must be
**GitHub Actions**. To publish manually, open the workflow link, choose **Run
workflow**, select `main`, and run it. The same page shows build and deployment
status; the deployed site is at <https://nicolasschuler.github.io/fledge/>.

## Run checks

```sh
python -m pytest tests -n auto
ruff check src tests docs/conf.py scripts
ruff format --check src tests docs/conf.py scripts
ty check src/latexprep
```

Ruff enforces, besides formatting and import order, the rule families `B`, `UP`, `C4`,
`PIE`, `PERF`, `PTH`, `SIM`, `RUF`, `PGH`, `T20`, `BLE`, `ARG`, `G`, `LOG` and `D` (the
[docstring convention](#docstring-convention) below), the
keyword-only rule for boolean parameters (`FBT001`, `FBT002`) and ceilings on complexity
(10), branches (12), statements (50) and arguments (8); `[tool.ruff.lint]` in
`pyproject.toml` names the few deliberate exceptions and says why. Split a function
instead of raising a ceiling. A blind `except Exception` is allowed only at a boundary
that reports the cause, with `# noqa: BLE001 - <reason>`, and a type-checker suppression
names its code and says why (`# ty: ignore[code]`).

`tests/__init__.py` puts `src/` first on the import path, so no `PYTHONPATH` is
needed, and it fails loudly if a stale installed `latexprep` would shadow the
checkout. Remove it with `python -m pip uninstall fledge-latex fledge latex-preparation`
(the current distribution name and the two former ones), then run
`python -m pip install -e .`.

Live tests are opt-in with `FLEDGE_RUN_INTEGRATION=1`. Run them separately
where application isolation and the relevant tools can actually start:

```sh
FLEDGE_RUN_INTEGRATION=1 python -m pytest tests -m live -v
```

Every live test carries the `@live_test(...)` decorator from `tests/support.py`,
which skips it unless the variable is set and applies the registered `live` marker.
Select them with `-m live`, or leave them out of a run with `-m 'not live'`.

`tests.test_integration_realistic` builds, checks and prepares projects with
constructs found in ordinary papers and journal templates (template classes,
`[0,1)` intervals, several matching graphics, a spare `.bib`, Biber). The other
`tests.test_integration_*` modules each run whole commands against real tools for
one theme: inputs (ZIP input, debris, awkward paths, identical output across
execution modes), policies (blocked runs, page limits, presets, `init`),
comparisons (reference PDFs, accepted exceptions with HTML previews, grayscale
previews, repeated baselines), sources (hostile constructs, `import` and
`subfiles`, local classes and styles, EPS figures), bibliography (Biber
transformations, `.bbl` inlining), engines (XeLaTeX, LuaLaTeX, mixed-engine
packages, failing PDF policies) and runtime (timeouts, `fmt` and `doctor`, an
interrupted build, the documented CI recipe). A test that needs a LaTeX package
BasicTeX may lack probes for it with `kpsewhich` and skips with a reason.
`tests/test_process_boundary.py` runs the command as a separate process without
TeX: exit codes, stream separation and the offline example page.

Tests distinguish component and workflow tests with controlled tool doubles from
real tool execution. Controlled doubles establish component behavior, not live
toolchain support, and skipped or blocked live checks must remain visible in
validation results. Every registered code links to tests that exercise detection
or measurement with source fixtures or controlled tool output; a catalogue
integrity test rejects missing associations. Each pytest-xdist worker is a
separate process that runs one test at a time, so Click's process-global stream
capture stays isolated. Online tests use
fake transports, so they verify request handling, not provider availability.
Optional `qpdf`/`mutool` adapters must remain inconclusive when their tools or
evidence are absent. The [limitations](limitations.md) page lists what has not
been validated live.

## Docstring convention

Every module, class, function and method in `src/` and `tests/` carries a docstring in
Google style, and ruff's pydocstyle rules (`D` with `convention = "google"`, plus `D401`)
enforce the layout. The rules that the linter cannot check are these:

- The summary is one imperative sentence that fits on the first line and ends with a
  period: "Read a regular file.", not "Reads ..." or "This function reads ...". A property
  is the exception; its summary is a noun phrase naming the value, as for an attribute.
- A blank line separates the summary from further prose, which says what the signature
  does not: intent, invariants, side effects, ordering, limits, and why a choice was made.
- Parameters, results and exceptions go in `Args:`, `Returns:` (or `Yields:`) and
  `Raises:` sections, in that order, each entry indented four spaces as
  `name: description` with the description wrapped under its own first word. An `Args:`
  section describes every parameter, so leave it out only when the summary and the
  signature already say everything, as for a one-argument helper. Once a docstring has
  any section it has all that apply: a function with two or more parameters gets
  `Args:`, and a function that returns or yields a value gets `Returns:` or `Yields:`.
  `Returns:` is omitted for a function that returns `None`, and `Raises:` lists the
  exceptions that belong to the contract, not every error a callee might raise.
- A yes/no function's summary reads "Return whether ...". `None`, `True` and `False` are
  written in double backticks like any other identifier.
- A class docstring lists its public attributes and dataclass fields under `Attributes:`,
  and an enum lists its members there (a pure name registry may instead say in one
  sentence where the meanings live). Constructor parameters that are not stored as public
  attributes go in a class-level `Args:` section. A class whose `__post_init__` validates
  its fields says so in one sentence before `Attributes:` ("Construction raises
  ``SettingsError`` when ..."). Constructors and magic methods need no docstring of their
  own, and a `#` comment on a field is dropped once the attribute entry says the same.
- A Protocol method carries the full contract. An implementation says "The arguments are
  those of :meth:`Protocol.method`." and adds only its own `Raises:` and caveats.
- Code identifiers in prose use double backticks, and cross-references use Sphinx roles
  such as :func:`scan_commands`. A docstring that contains a backslash (a LaTeX command,
  a regular expression) is a raw string, `r"""`.
- Test modules and test case classes say what they cover. A test method may omit its
  docstring when its name describes the case; test doubles and helpers are documented
  like production code.

```python
def read_regular_file(path: Path, limit: int) -> bytes:
    """Read a whole regular, unlinked file that holds at most ``limit`` bytes.

    The reasons the function itself gives (link, special file, size) are in
    ``strerror`` and name no path.

    Args:
        path: The file to read.
        limit: The largest number of bytes the file may hold.

    Returns:
        The contents of the file.

    Raises:
        OSError: The path is a link or a special file, or reading failed.
        FileTooLargeError: The file holds more than ``limit`` bytes, including when it
            grows while it is read.
    """
```

## Test structure and seams

Tests are `unittest.TestCase` classes, run by pytest. Shared fixtures live in three
support modules, never in a test module: a rule in
`tests/test_module_boundaries.py` rejects every way of importing one test module from
another (`from tests.test_x import ...`, `from tests import ...` naming a test module, and
`import tests.test_x`).

- `tests/support.py` holds project builders (`write_project`, `minimal_project`),
  the `ProjectTestCase` and `AsyncProjectTestCase` bases (a temporary `self.root`
  with `write` and `write_project`), the `@live_test` decorator, the controlled
  document backends below, the golden-file helpers and a small JSON Schema
  validator for the subset of keywords the report schema uses.
- `tests/fakes.py` holds `FakeCommandRunner`, a scripted implementation of the
  `CommandRunner` protocol that the PDF, formatting and comparison adapters
  depend on: outputs per tool, missing tools, a failure mode, events that hold a
  command, and a log of every call. `FakeBuildRunner` and `LifecycleRunner` are
  `ToolRunner` doubles for builds and process lifetimes; they use the `sandbox`,
  `toolchain` and `monitor_factory` parameters of `ToolRunner` instead of
  reaching into its private state.
- `tests/pdf_fakes.py` holds the PDF tool output fixtures (`trace_xml`, `object_json`,
  `color_trace`, `structured_pdf_objects`) and the runners built on `FakeCommandRunner`
  (`DetailRunner`, `ArtworkRunner`, `FakePdfRunner`, `ConfigurablePdfRunner`).

A job that would run TeX or the PDF tools takes its tool-driven operations from a
`DocumentBackends` value (`run_job(..., backends=...)`): the build, the PDF
inspection and the PDF comparison. The defaults run the real tools. Tests pass
`controlled_backends(build=..., inspect_pdf=..., compare_pdfs=...)` from
`tests/support.py` instead of patching module globals; each argument is an async
function, a value to return, or an exception to raise. Patch a module global only
to test genuinely global behaviour, such as the lookup of the host's tools or a
module-wide limit.

## Golden outputs

`tests/test_golden_outputs.py` compares outputs with files in `tests/golden/`:
the `--help` text of every command (at width 100, without colour), and the JSON
report of a `check` run with controlled backends together with its terminal
summary and its compact, CI-annotation and HTML renderings. The JSON report and
the run details inside the HTML are normalised first: paths, the version and
measured times and resource figures become placeholders, so a golden changes only
when the output does. The same report is validated against
`docs/_static/report.schema.json`.

After an intended change of an option, a message or the report, regenerate the
files and review the diff before committing:

```sh
FLEDGE_UPDATE_GOLDEN=1 python -m pytest tests/test_golden_outputs.py
git diff tests/golden
```

The help goldens follow the installed Click and Rich versions; regenerate them after
a dependency update that changes the layout.

## Continuous integration

The [Tests workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/tests.yml)
runs on pushes to `main`, pull requests, manual dispatch and weekly. Every run:

- lints with Ruff (`check` and `format --check`) and checks types with `ty`;
- lints `install.sh` with ShellCheck;
- builds the documentation with `-n -W`, as the Deploy documentation workflow does,
  so a broken link fails the pull request instead of the merge;
- builds the source archive and wheel, installs the wheel into a fresh
  environment, runs `fledge --version` from another directory, inspects the
  [offline example](examples.md) with that installation, and runs
  `tests/test_docs.py` and `tests/test_packaging.py` from the extracted source
  archive, so a file missing from either distribution fails the pull request;
- runs the unit tests with pytest on macOS and Ubuntu with Python 3.11, 3.12 and 3.13.

Manual and weekly runs add two live jobs that run the opt-in integration tests
(`FLEDGE_RUN_INTEGRATION=1`) without tool doubles. Each lists its test files by
hand, and `tests/test_packaging.py` fails when a test file with live tests
is missing from both lists:

- `live-macos` installs BasicTeX, Poppler, `tex-fmt` and Biber and also runs the
  macOS sandbox tests.
- `live-linux` installs TeX Live, Biber, Poppler and Bubblewrap from apt plus a
  pinned `tex-fmt` release, and runs the integration tests under Bubblewrap. It
  leaves out `test_runtime_inputs.py` and `test_runtime_monitoring.py`, whose live tests
  run only on macOS, and
  adds `test_runtime_backends.py` for the live Bubblewrap isolation test.
  It first passed on 2026-10-12. What the documentation says about Linux rests
  on this job, so a failure is a real result that must not be ignored: fix the
  cause or correct those statements. On Ubuntu 24.04 it sets
  `kernel.apparmor_restrict_unprivileged_userns=0` on the disposable runner, a
  CI setting that users must decide on for their own hosts (see
  [installation](installation.md#linux)).

Every third-party action in every workflow is pinned to a commit SHA, and
Dependabot (`.github/dependabot.yml`) proposes a grouped update each week. The
[Release workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/release.yml)
publishes to PyPI; see [release to PyPI](#release-to-pypi).

To run Fledge on a paper repository rather than on Fledge itself, see
[check your paper in CI](continuous-integration.md).

## Release to PyPI

Fledge is published on PyPI as `fledge-latex`. The PyPI project named `fledge` is
an unrelated project, which is why the distribution has a different name from the
command. The `fledge` and `latex-prep` commands and the `latexprep` module are
unchanged. The Release workflow uses PyPI trusted publishing (OpenID Connect), so
no PyPI token is stored in GitHub.

### One-time setup

1. On PyPI, sign in to the account that should own the project, which should have
   two-factor authentication enabled. Open the account menu, choose
   **Publishing**, and add a new **pending publisher** for GitHub Actions with
   these values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `fledge-latex` |
   | Owner | `NicolasSchuler` |
   | Repository name | `fledge` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. On GitHub, open **Settings → Environments** for the repository and create an
   environment named `pypi`. Adding yourself as a required reviewer makes every
   release wait for your approval before it uploads.

A pending publisher does not reserve the name, and the project does not exist
until the first release uploads. Someone else could register `fledge-latex`
first, so publish soon after registering. The first successful upload creates the
project and turns the pending publisher into an ordinary one. Because the
[installation guide](installation.md#install-with-pipx-or-uv) tells readers to run
`pipx install fledge-latex`, publish the first release soon after that page
reaches `main`.

### Each release

1. Set the new version in `__version__` in `src/latexprep/__init__.py`, the only
   place it is defined: `pyproject.toml` reads it through
   `[tool.setuptools.dynamic]`, and `fledge --version` reports it. Give the
   version's heading in `CHANGELOG.md` its release date. The workflow
   (`python scripts/release_checks.py version`) refuses to build if the version
   is defined anywhere else or if the release tag is not `v` followed by that version.
2. Merge to `main` and wait for the Tests workflow to pass. Run the live jobs
   from the Actions tab when the release depends on them.
3. Optional dry run: on the Release workflow page choose **Run workflow** on
   `main`. It builds the source archive and wheel, installs the wheel into a fresh
   environment, checks `fledge --version`, the offline example and the packaged
   presets, and uploads the files as the `python-distributions` artifact, but
   never publishes.
4. Create a GitHub release: choose or create the tag `vX.Y.Z` (for the current
   version, `v0.3.1`) on `main` and publish the release. Publishing starts the
   workflow, which verifies the tag, builds, smoke-tests and then uploads to PyPI
   from the `pypi` environment. Approve the deployment if you added a reviewer.
5. Check the result in a clean environment: `pipx install fledge-latex==X.Y.Z`,
   then `fledge --version`.

PyPI never accepts a replacement for a file it already has. If a release is
wrong, fix the problem and publish a new version; yank the bad one on PyPI if
needed.

## Agent skill

`skills/fledge/SKILL.md` is the Agent Skill described in
[use Fledge from an AI agent](agents.md). A test checks that every command and
option it names exists in the CLI, so update the skill together with CLI changes.

## Build local distributions

```sh
python -m build
```

This creates a wheel and source archive in `dist/`; it does not publish them.
The wheel installs the CLI. The source archive also carries the documentation,
examples, tests, agent skill, architecture, requirements, changelog and the scripts, so it can be
used to build these docs and run the documented source checks. It also includes
the opt-in macOS installer; installer tests use controlled commands, not native
Homebrew or MacTeX installation.

## Design and planning references

The requirements and the parallel execution design are historical design
records: they explain why Fledge is built as it is, were written on 6 to
12 October 2026, and are not updated with the code.

- {download}`Architecture <../architecture.md>`: component responsibilities,
  stage boundaries, isolation, and parallel execution design, including future
  interface work.
- {download}`Requirements <../requirements.md>`: the original product requirements
  and acceptance criteria (historical design record); use
  [what prepare does](workflow.md) and the [workflow reference](workflow-reference.md)
  for current scope.
- [Check backlog](check-backlog.md): implemented subsets, partial behavior,
  outstanding implementation, and missing validation.
- [Parallel execution design](parallelization-plan.md): rationale and acceptance
  criteria of the scheduler work, with the starting seams it replaced (historical
  design record).
- [Bounded benchmark](parallelization-benchmark.md): recorded local synthetic
  measurements and their limits.

`fledge rule CODE` lists the behavioral tests associated with each check.

## Extend an implemented check

Follow the current option and dispatch paths rather than treating the design
documents as an implemented plugin API. A check usually needs its typed options,
family rule metadata, execution dispatch, and selection policy in
{download}`check_policy.py <../src/latexprep/check_policy.py>` to agree. The
per-rule facts are fields of the rule itself: `mandatory` for a guard that check
selection never removes, `reviewable` when a reviewed exception may target it, and
`not_checked_area` for an opt-in area that a run reports as not checked. Only the
mapping from an opt-in code to its option fields stays in `check_policy.py`.

Add an explicit code, descriptive title, scope and fix text to the applicable
data-only `*_rules.py` module, registered by
{download}`rules.py <../src/latexprep/rules.py>`. Codes are never reused. A name that a finding
carries but that describes the run or the tool environment rather than the manuscript
belongs in `OPERATIONAL_RULE_NAMES` instead; a test fails for an emitted name that is
neither catalogued nor operational. Link
the rule to a unit test that runs the actual checker on a meaningful fixture and
asserts both the code and the intended behavior. Include a valid counterpart, a
violating case, and an unavailable or ambiguous evidence case when applicable.
Reuse an existing behavior test when it already proves the contract; do not add
a test that only constructs a `Finding`. Keep evidence types and failure states
intact when changing execution order, and check scheduling and serial/parallel
equivalence separately from rule behavior.

After changing a rule, regenerate the catalogue with
`python scripts/generate_check_docs.py`. It derives each code's group (on by
default, opt-in or always enforced) and its enabling settings from
`check_policy.py`, and shows the rule's fix text. A test fails while
`docs/checks.md` is stale; the generated tables are never edited by hand.

The package is flat but layered, and [the module map](modules.md) assigns each of its
modules to one layer with a one-line responsibility. A module imports from its own layer and
the layers below it, and only the presentation layer imports `click` or `rich`;
`tests/test_module_boundaries.py` reads every import and fails on an upward import, on a
cycle, and when `tests/module_layers.py` and the tables of the module map disagree. Add a
new module to both.

The job workflow starts at `run_job` in
{download}`core.py <../src/latexprep/core.py>`, which validates the request,
dispatches the command and publishes the result.
{download}`document_run.py <../src/latexprep/document_run.py>` carries one
document through analysis, baseline, staged transformations and verification;
{download}`document_tasks.py <../src/latexprep/document_tasks.py>` builds its
scheduled tasks; {download}`documents.py <../src/latexprep/documents.py>` runs
configured `workflow.documents`; and
{download}`publication.py <../src/latexprep/publication.py>` writes verified
outputs. Around them,
{download}`task_reporting.py <../src/latexprep/task_reporting.py>` writes task
results, builds and selections into the report,
{download}`document_builds.py <../src/latexprep/document_builds.py>` runs the
staged rebuilds and repeat baselines,
{download}`package_verification.py <../src/latexprep/package_verification.py>`
archives, re-extracts and re-checks the package, and
{download}`main_selection.py <../src/latexprep/main_selection.py>` chooses the
main file. A new check of the document's sources is one entry in `_SOURCE_CHECKS`
and one in `TaskName`, both in `document_tasks.py`; it then runs on the original
sources and again on the final extraction.

Small modules under the job workflow each own one concern and import nothing
above them. {download}`errors.py <../src/latexprep/errors.py>` defines the
expected failures: `PreparationError`, `RequestError`, and the kinds callers act
on, `ToolUnavailableError` (the missing `tool`), `SettingsError` (the `settings`
at fault) and `SandboxRefusedError`. Branch on these types and attributes, never
on the wording of a message. Raise them only for problems a researcher can fix;
a violated internal contract raises `ValueError` or `RuntimeError`, which
`defects.is_expected_failure` reports as a defect. Per-item failures are
findings; only `PreparationError` aborts a job. A check that cannot examine one
file, URL or entry reports a finding (or a gap in its evidence) and the run
continues. An exception that a module uses to stop its own work, such as the
BibTeX parser's, the transport's `NetworkFailure` or a bibliography proposal's
`ProposalBlocked`, is not a `PreparationError`: it never leaves its feature's
modules and is turned into a finding at their boundary.
{download}`findings.py <../src/latexprep/findings.py>` defines `Finding`,
{download}`report.py <../src/latexprep/report.py>` the `Report` with its
`Outcome` and exit codes, and
{download}`setting_choices.py <../src/latexprep/setting_choices.py>` the closed
values that settings accept. For concurrency,
{download}`cancellation.py <../src/latexprep/cancellation.py>` holds the
cooperative-cancellation helpers (modules that parse files import it, never the
scheduler),
{download}`resource_budget.py <../src/latexprep/resource_budget.py>` the shared
CPU, memory and tool-class reservations,
{download}`scheduler.py <../src/latexprep/scheduler.py>` the dependency
scheduling of tasks, and
{download}`process_control.py <../src/latexprep/process_control.py>` the start
and kill helpers for child processes.

Diagnostic output goes through `logging`, never `print`. A module that reports its progress
declares `logger = logging.getLogger(__name__)` and logs with lazy `%` arguments
(`logger.debug("task %s: start", name)`; Ruff's `G` rules reject f-strings). Log at DEBUG,
and at INFO only what an investigator should notice, such as a limit that stopped a tool.
A message names tools, hosts, task names and counts, never a path, a URL, a query string
or a contact address, and never an exception's text when that may contain one. The
library configures nothing: `latexprep/__init__.py` gives the package logger a
`NullHandler`, and the command line attaches one stderr handler for the length of a
command when `--debug` or `FLEDGE_DEBUG=1` asks for it
({download}`diagnostic_logging.py <../src/latexprep/diagnostic_logging.py>`). That handler
prints the time since start, the module name and the message, replaces the home and
temporary directories as reports do, and leaves stdout to the report. See the
[diagnostic log](troubleshooting.md#diagnostic-log) for the user's side.

Option dataclasses validate in `__post_init__` with the kit in
{download}`option_config.py <../src/latexprep/option_config.py>`, which also
converts TOML tables into those classes. `validate_scalars` checks every boolean,
integer, number and `Literal` field against its annotation and the limits in the
class's `FIELD_BOUNDS`; `require_unique_strings`, `require_text`, `require_pairs`
and their siblings check lists and text. A wrong type and an out-of-range value
therefore give the same message, which names the key as a settings file spells it
and states the accepted range. Write each limit once, as a named constant with its
unit, and build the message from it rather than repeating the number.

For normal preparation, online checks run once on the fresh archive extraction;
source inspection and dry runs use the initial snapshot. Preserve the request
budget per document when changing this dispatch. Controlled tests should make
the inspected content and phase explicit.

The architecture document includes target design: per-task timeout/artifact
descriptors and a structured progress-event schema are not implemented as
described there. Current execution uses progress strings and a shared whole-job
deadline alongside per-tool limits. Bibliography files and PDF measurements are
still parsed by each check that reads them. Checkers that read TeX source share two
public modules:
{download}`tex_lexer.py <../src/latexprep/tex_lexer.py>` provides the bounded
lexical subset (commands and their argument groups, comment and verbatim
masking, conditional spans), and
{download}`source_graph.py <../src/latexprep/source_graph.py>` builds the literal
dependency graph of one selected main file with `SourceGraph.build`. The graph
is split by concern: `source_loading.py` reads and lexes a file within the byte and
command budgets, `source_command_checks.py` judges its commands,
`source_resolution.py` turns a file name into a project file, and
`source_file_commands.py` holds the command tables they share. Reports about a graph
are in `source_analysis.py` (roots, dependencies, lint) and `flat_layout.py` (flat-layout
plans).
{download}`expanded_document.py <../src/latexprep/expanded_document.py>` splices
that graph into one document whose offsets map back to file and line
(`expand_document`), and
{download}`literal_text.py <../src/latexprep/literal_text.py>` holds the shared
command tables and plain-text reading used by manuscript, author-record and
submission-text checks. The vocabularies those readers have in common (TeX file
suffixes, math markers, macro-definition and citation commands) are in
{download}`tex_commands.py <../src/latexprep/tex_commands.py>`, and the wording of
unfinished-work and private-note markers is in
{download}`comment_markers.py <../src/latexprep/comment_markers.py>`. Add a command
or phrase there, not as a private list in a check.
The checks of one job do not each read the project again:
{download}`analysed_source.py <../src/latexprep/analysed_source.py>` holds the graph
and expanded document of one project root and main file (`AnalysedSource`), builds
each on first use, and `JobContext.sources` hands the same one to every task that
reads that root, however many threads ask. A check accepts it as an optional
`analysed` argument and builds its own when it runs stand-alone; an analysis of
another root or main file is refused. It is read-only, so the transformations that
plan each edit on a staged copy that earlier edits changed (`source_transform`,
`bibliography_transform`, `flat_layout`) build their own graph.
{download}`text_safety.py <../src/latexprep/text_safety.py>` escapes untrusted text
for the terminal, HTML reports, CI annotations and one-line messages. Checks that
scan every supplied file use the bounded inventory and per-scan byte budget in
{download}`file_scan.py <../src/latexprep/file_scan.py>`, comment extraction in
{download}`tex_comments.py <../src/latexprep/tex_comments.py>`, and bounded redaction
in {download}`redaction.py <../src/latexprep/redaction.py>` and
{download}`finding_text.py <../src/latexprep/finding_text.py>`. Every read of a
project, build or bibliography file goes through `read_regular_file` or
`read_file_prefix` in {download}`file_safety.py <../src/latexprep/file_safety.py>`,
which opens the file without following a final link, checks the opened
descriptor and never reads past its limit; do not reopen a file with
`Path.read_bytes` after checking its size. Path rules live in
{download}`path_safety.py <../src/latexprep/path_safety.py>`:
`is_relative_posix_path` validates a project-relative name with explicit options
(normalized spelling, whether `.` is allowed, how unusual characters are treated,
a per-segment pattern), and `is_contained` and `is_link_free_regular_file` decide
containment. The byte units and the few limits that several modules share are in
{download}`limits.py <../src/latexprep/limits.py>`; a limit that one module uses
stays beside its code. Bibliography checks,
transformations and online lookups share the bounded BibTeX reader in
{download}`bibtex_syntax.py <../src/latexprep/bibtex_syntax.py>`, the one loader
of declared .bib files (`read_declared_bibliographies`, with typed problems and
byte budgets) in
{download}`bibliography_files.py <../src/latexprep/bibliography_files.py>`, the
index of entries and their reach in
{download}`bibliography_index.py <../src/latexprep/bibliography_index.py>` and the
literal citation graph of the selected main file in
{download}`citation_graph.py <../src/latexprep/citation_graph.py>`
(`build_citation_graph`). The online checks are layered, each module importing
only those before it: `online_options` (settings and outcomes), `public_url`
(which addresses may be requested), `pinned_dns` (resolution in a disposable
child), `online_http_wire` (the HTTP/1.1 bytes, testable on a `StreamReader`),
`online_http_session` (the client, with `resolver` and `transport` seams that
tests replace), `crossref` (typed replies), `online_inputs` and `online_checks`
(the findings). No test needs a network.
PDF checkers share a few modules. {download}`pdf_tools.py <../src/latexprep/pdf_tools.py>`
holds the tool session that freezes the inputs and runs each Poppler, qpdf or MuPDF
command in its own directory, the one argument builder for each tool command, the
preflight (document, properties and page count) that marks every configured rule
unavailable when it fails, and `ToolMeasurement`, the one way a command's output becomes
findings or, when it cannot be read, inconclusive ones. The parsers for Poppler and
MuPDF output are in {download}`pdf_tool_output.py <../src/latexprep/pdf_tool_output.py>`,
the qpdf object graph is {download}`pdf_qpdf_graph.py <../src/latexprep/pdf_qpdf_graph.py>`,
page, color and render-size arithmetic is
{download}`pdf_geometry.py <../src/latexprep/pdf_geometry.py>`, and the builders for
measurement and unavailable-evidence findings are
{download}`measurement_findings.py <../src/latexprep/measurement_findings.py>`. The
option schema of the configured measurements is
{download}`pdf_check_options.py <../src/latexprep/pdf_check_options.py>`, and the
`pdf.*` rule names are the `PdfRule` enum in
{download}`pdf_rule_names.py <../src/latexprep/pdf_rule_names.py>`. A session remembers
each completed read-only measurement by its arguments, so two checks that need the same
output run the tool once. The check groups of one PDF in a pipeline run
({download}`pdf_check_pipeline.py <../src/latexprep/pdf_check_pipeline.py>`) go further:
they share one `SharedPdfSession`, which freezes the PDF on first use and hands each group
a view with its own working directory and its own tool record over one shared memo, so a
read-only measurement runs once per PDF for the whole run. The memo keeps the output of
each distinct read-only command (at most the runner's output limit each) until the run
ends and then releases it; renders are never kept. Each group entry point takes
`session=` and works unchanged without it. Each PDF adapter module states the external tools of its
configured checks (`required_pdf_check_tools`, `required_artwork_tools`,
`required_structure_tools`, `required_inspection_tools`, `COMPARISON_TOOLS`), and
`fledge doctor` unions them.
Binary image handling sits in two leaf modules that import no tool, runtime or
scheduler code:
{download}`png_support.py <../src/latexprep/png_support.py>` (the PNG encoder,
validator, strict chunk walker and header reader) and
{download}`raster_pixels.py <../src/latexprep/raster_pixels.py>` (the bounded PNM
reader and the ink, extrema and changed-pixel analysis, which use only
C-speed byte operations). Sharing further parsed
evidence between checkers is a future maintenance tradeoff; extending a check does
not require a new plugin framework.
