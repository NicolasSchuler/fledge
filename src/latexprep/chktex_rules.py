"""Data-only catalogue entry for the optional chktex lint, whose warnings are suggestions."""

from __future__ import annotations

from .rule_definition import Rule

# The one rule every finding of the chktex lint carries, whatever chktex reported.
CHKTEX_WARNING = Rule(
    code="CHK001",
    name="chktex.warning",
    title="chktex warning",
    description=(
        "Suggestion: run the installed chktex, the LaTeX lint tool that ships with TeX "
        "Live, inside the sandbox on each TeX file that the selected main file uses, and "
        "report every warning at its file and line with chktex's number and message. "
        "chktex honours a .chktexrc beside the main file and % chktex N and "
        "% chktex-file N comments; a .chktexrc that could make chktex read other files, "
        "write files or stop before checking is refused. Not checked when chktex is not "
        "installed. A run lists a bounded number of warnings and counts the rest."
    ),
    tests=(
        "tests.test_chktex_lint.ChktexLintTests.test_each_warning_becomes_a_suggestion_at_its_file_and_line",
        "tests.test_chktex_lint.ChktexLintTests.test_a_document_without_warnings_passes",
        "tests.test_chktex_lint.ChktexLintTests.test_a_missing_chktex_is_reported_as_not_checked",
        "tests.test_chktex_lint.ChktexLintTests.test_an_unsafe_project_configuration_is_refused_before_chktex_runs",
        "tests.test_chktex_lint.ChktexLintTests.test_warnings_beyond_the_bound_are_counted",
    ),
    fix=(
        "Follow chktex's advice. To keep a construct on purpose, end its line with "
        "% chktex N or % fledge: ignore CHK001, use % chktex-file N for a whole file, or "
        "switch the warning off with CmdLine { -nN } in a .chktexrc beside the main file."
    ),
    suggestion=True,
    reference_url="https://mirrors.ctan.org/support/chktex/ChkTeX.pdf",
)

RULES = (CHKTEX_WARNING,)
