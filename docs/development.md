# Development and documentation

Read the [hosted documentation](https://nicolasschuler.github.io/fledge/) without
installing Fledge or its documentation tools.

Work from a source checkout or extracted source distribution. Keep private papers
outside the repository or in `.local/`. Review the paths to be staged: generated
outputs and environments are ignored, while manuscript PDFs, images, and other
source assets remain trackable. The project has no selected license.

## Install development tools

After creating and activating a virtual environment:

```sh
python -m pip install -e '.[dev,docs,build]'
```

The `dev` extra provides Ruff and ty; `docs` provides Sphinx and MyST; `build`
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
python -m unittest discover -s tests -t . -v
ruff check src tests docs/conf.py scripts
ruff format --check src tests docs/conf.py scripts
ty check src/latexprep
```

`tests/__init__.py` puts `src/` first on the import path, so no `PYTHONPATH` is
needed, and it fails loudly if a stale installed `latexprep` would shadow the
checkout. Remove it with `python -m pip uninstall fledge latex-preparation`, then
run `python -m pip install -e .`.

Live tests are opt-in with `FLEDGE_RUN_INTEGRATION=1` (the older
`LATEX_PREP_RUN_INTEGRATION=1` and `LATEXPREP_RUN_SANDBOX_TESTS=1` still work).
Run them separately where application isolation and the relevant tools can
actually start:

```sh
FLEDGE_RUN_INTEGRATION=1 python -m unittest tests.test_integration \
  tests.test_integration_checks tests.test_integration_remaining \
  tests.test_integration_backends tests.test_integration_realistic \
  tests.test_runtime_parallel -v
```

`tests.test_integration_realistic` builds, checks and prepares projects with
constructs found in ordinary papers and journal templates (template classes,
`[0,1)` intervals, several matching graphics, a spare `.bib`, Biber).

Controlled tool doubles establish component behavior, not live toolchain support.
Skipped or blocked live checks must remain visible in validation results. Online
tests use fake transports; `qpdf` and `mutool` controlled-output tests do not
establish live-tool validation. The [workflow reference](workflow.md#development-checks)
retains the detailed testing qualifications.

## Continuous integration

The [Tests workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/tests.yml)
runs on pushes to `main`, pull requests, manual dispatch and weekly. It lints with
Ruff and `ty`, then runs the unit tests on macOS and Ubuntu with Python 3.11 and
3.13. Manual and weekly runs add a live macOS job that installs BasicTeX, Poppler,
`tex-fmt` and Biber and runs the opt-in integration and sandbox tests. Every
third-party action in both workflows is pinned to a commit SHA.

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
examples, tests, agent skill, architecture, requirements, and benchmark script, so it can be
used to build these docs and run the documented source checks. It also includes
the opt-in macOS installer; installer tests use controlled commands, not native
Homebrew or MacTeX installation.

## Design and planning references

These source documents remain available without claiming that every planned
capability is implemented:

- {download}`Architecture <../architecture.md>`: component responsibilities,
  stage boundaries, isolation, and parallel execution design, including future
  interface work.
- {download}`Requirements <../requirements.md>`: the broader planned release and
  acceptance criteria; use the [workflow reference](workflow.md) for current scope.
- [Check backlog](check-backlog.md): implemented subsets, partial behavior,
  outstanding implementation, and missing validation.
- [Parallel execution design](parallelization-plan.md): rationale and acceptance
  criteria, with current implementation status separated from historical seams.
- [Bounded benchmark](parallelization-benchmark.md): recorded local synthetic
  measurements and their limits.

The [check catalogue](checks.md) links each diagnostic to its behavioral tests.
Those links are downloads in the built HTML, so the evidence remains accessible
without relying on a hosted repository URL.

## Extend an implemented check

Follow the current option and dispatch paths rather than treating the design
documents as an implemented plugin API. A check usually needs its typed options,
family rule metadata, execution dispatch, and selection policy in
{download}`check_policy.py <../src/latexprep/check_policy.py>` to agree. If it is
eligible for a reviewed exception, check the review policy too. Exercise the
relevant initial and final verification phases and add behavioral tests for
valid, violating, and unavailable or ambiguous evidence. See
[extending a check](checks.md#extending-a-check) for catalogue requirements.
After changing a rule's description, regenerate the catalogue with
`python scripts/generate_check_docs.py`; a test fails while `docs/checks.md` is
stale, and its table is never edited by hand.

For normal preparation, online checks run once on the fresh archive extraction;
source inspection and dry runs use the initial snapshot. Preserve the request
budget per document when changing this dispatch. Controlled tests should make
the inspected content and phase explicit.

The architecture document includes target design: per-task timeout/artifact
descriptors and a structured progress-event schema are not implemented as
described there. Current execution uses progress strings and a shared whole-job
deadline alongside per-tool limits. Some evidence is parsed repeatedly, and
checker modules share private parsers. Extracting shared evidence or parser
interfaces is a future maintenance tradeoff; extending a check does not require
a new plugin framework.
