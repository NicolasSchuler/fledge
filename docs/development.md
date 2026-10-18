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

`tests/__init__.py` puts `src/` first on the import path, so no `PYTHONPATH` is
needed, and it fails loudly if a stale installed `latexprep` would shadow the
checkout. Remove it with `python -m pip uninstall fledge-latex fledge latex-preparation`
(the current distribution name and the two former ones), then run
`python -m pip install -e .`.

Live tests are opt-in with `FLEDGE_RUN_INTEGRATION=1`. Run them separately
where application isolation and the relevant tools can actually start:

```sh
FLEDGE_RUN_INTEGRATION=1 python -m pytest tests/test_integration.py \
  tests/test_integration_checks.py tests/test_integration_remaining.py \
  tests/test_integration_backends.py tests/test_integration_realistic.py \
  tests/test_runtime_parallel.py -v
```

`tests.test_integration_realistic` builds, checks and prepares projects with
constructs found in ordinary papers and journal templates (template classes,
`[0,1)` intervals, several matching graphics, a spare `.bib`, Biber).

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

## Continuous integration

The [Tests workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/tests.yml)
runs on pushes to `main`, pull requests, manual dispatch and weekly. Every run:

- lints with Ruff (`check` and `format --check`) and checks types with `ty`;
- lints `install.sh` with ShellCheck;
- builds the documentation with `-n -W`, as the Deploy documentation workflow does,
  so a broken link fails the pull request instead of the merge;
- builds the source archive and wheel, installs the wheel into a fresh
  environment, runs `fledge --version` from another directory, and runs
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
  leaves out `test_runtime_parallel.py`, whose live tests run only on macOS, and
  adds `test_runtime_backends.py` for the live Bubblewrap isolation test.
  **This job is unvalidated until a maintainer has run it from the Actions tab
  and it passes**; until then the documentation must keep saying that Linux has
  not run live. On Ubuntu 24.04 it sets
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
   environment, checks `fledge --version` and the packaged presets, and uploads
   the files as the `python-distributions` artifact, but never publishes.
4. Create a GitHub release: choose or create the tag `vX.Y.Z` (for the current
   version, `v0.3.0`) on `main` and publish the release. Publishing starts the
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
{download}`check_policy.py <../src/latexprep/check_policy.py>` to agree. If it is
eligible for a reviewed exception, check the review policy in
{download}`review_policy.py <../src/latexprep/review_policy.py>` too.

Add an explicit code, descriptive title, scope and fix text to the applicable
data-only `*_rules.py` module, registered by
{download}`rules.py <../src/latexprep/rules.py>`. Codes are never reused. Link
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

The job workflow starts at `run_job` in
{download}`core.py <../src/latexprep/core.py>`, which validates the request and
publishes the result.
{download}`document_run.py <../src/latexprep/document_run.py>` carries one
document through analysis, baseline, staged transformations and verification;
{download}`document_tasks.py <../src/latexprep/document_tasks.py>` builds its
scheduled tasks; {download}`documents.py <../src/latexprep/documents.py>` runs
configured `workflow.documents`; and
{download}`publication.py <../src/latexprep/publication.py>` writes verified
outputs. A new scheduled check needs a task in `document_tasks.py`, named in
`models.TaskName`.

For normal preparation, online checks run once on the fresh archive extraction;
source inspection and dry runs use the initial snapshot. Preserve the request
budget per document when changing this dispatch. Controlled tests should make
the inspected content and phase explicit.

The architecture document includes target design: per-task timeout/artifact
descriptors and a structured progress-event schema are not implemented as
described there. Current execution uses progress strings and a shared whole-job
deadline alongside per-tool limits. Some evidence is parsed repeatedly. Checkers
that read TeX source share two public modules:
{download}`tex_lexer.py <../src/latexprep/tex_lexer.py>` provides the bounded
lexical subset (commands and their argument groups, comment and verbatim
masking, conditional spans), and
{download}`source_graph.py <../src/latexprep/source_graph.py>` builds the literal
dependency graph of one selected main file with `SourceGraph.build`.
{download}`expanded_document.py <../src/latexprep/expanded_document.py>` splices
that graph into one document whose offsets map back to file and line
(`expand_document`), and
{download}`literal_text.py <../src/latexprep/literal_text.py>` holds the shared
command tables and plain-text reading used by manuscript, author-record and
submission-text checks. Checks that scan every supplied file use the bounded
inventory, reads, comment extraction and redaction in
{download}`file_scan.py <../src/latexprep/file_scan.py>`. Bibliography checks,
transformations and online lookups share the bounded BibTeX reader in
{download}`bibtex_syntax.py <../src/latexprep/bibtex_syntax.py>` and the literal
citation graph of the selected main file in
{download}`citation_graph.py <../src/latexprep/citation_graph.py>`
(`build_citation_graph`).
PDF checkers share
{download}`pdf_support.py <../src/latexprep/pdf_support.py>`: a tool session that
freezes the inputs and runs each Poppler, qpdf or MuPDF command in its own
directory, the parsers for their output, and the builders for measurement and
unavailable-evidence findings. Sharing further parsed
evidence between checkers is a future maintenance tradeoff; extending a check does
not require a new plugin framework.
