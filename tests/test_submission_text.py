"""Behavioral tests for safe source-only submission export."""

from __future__ import annotations

import json
import zipfile

from click.testing import CliRunner

from latexprep.submission_text import convert_text, read_submission_text
from latexprep.submission_text_command import (
    SUBMISSION_TEXT_SCHEMA_VERSION,
    submission_text_command,
)
from tests.support import ProjectTestCase


class SubmissionTextTests(ProjectTestCase):
    def write(self, text: str) -> None:
        (self.root / "main.tex").write_text(text, encoding="utf-8")

    def test_formatting_accents_comments_and_math_preserve_content(self) -> None:
        text, issues = convert_text(
            "A \\emph{well-known \\textbf{method}}\\\\\n"
            "caf\\'e foo% comment\nbar $n=100$ and $\\alpha$ \\% \\& "
            r"\url{https://example.org/a_b}"
        )
        self.assertEqual(
            text, "A well-known method café foobar n=100 and α % & https://example.org/a_b"
        )
        self.assertEqual(issues, [])
        self.assertEqual(convert_text("foo\nbar")[0], "foo bar")
        self.assertEqual(convert_text("foo% comment\n  bar")[0], "foobar")
        self.assertEqual(convert_text("\\emph{foo% } ignored\nbar}"), ("foobar", []))
        self.assertEqual(
            convert_text(r"\url{https://example.org/a%20b}"), ("https://example.org/a%20b", [])
        )

    def test_unknown_math_citations_and_macros_remain_visible(self) -> None:
        for value in (r"\cite{key}", r"$x^2$", r"\ours{method}", r"\ref{fig}"):
            with self.subTest(value=value):
                text, issues = convert_text(value)
                self.assertEqual(text, value)
                self.assertTrue(issues)

    def test_raw_fields_and_included_abstract(self) -> None:
        self.write(
            r"\documentclass{article}\title{A \emph{title}}"
            r"\begin{document}\begin{abstract}\input{body}\end{abstract}"
            r"\keywords{one, two}\end{document}"
        )
        (self.root / "body.tex").write_text("foo% comment\nbar $n=100$", encoding="utf-8")
        fields = read_submission_text(self.root, "main.tex")
        self.assertEqual(fields["title"].draft, "A title")
        self.assertEqual(fields["abstract"].draft, "foobar n=100")
        self.assertEqual(fields["abstract"].status, "ready")

    def test_multiple_conditional_and_missing_graph_are_unresolved(self) -> None:
        for text in (
            r"\title{One}\title{Two}",
            r"\iftrue\title{One}\fi",
            r"\title{One}\input{missing}",
        ):
            self.write(r"\documentclass{article}" + text)
            fields = read_submission_text(self.root, "main.tex")
            self.assertEqual(fields["title"].status, "unresolved")

    def test_cli_ready_stdout_and_unresolved_stderr(self) -> None:
        self.write(r"\documentclass{article}\title{A \emph{title}}\abstract{\cite{key}}")
        before = (self.root / "main.tex").read_bytes()
        runner = CliRunner()
        ready = runner.invoke(
            submission_text_command, [str(self.root), "--isolated", "--field", "title"]
        )
        self.assertEqual(ready.exit_code, 0, ready.output)
        self.assertEqual(ready.stdout, "A title\n")
        blocked = runner.invoke(
            submission_text_command, [str(self.root), "--isolated", "--field", "abstract"]
        )
        self.assertEqual(blocked.exit_code, 3, blocked.output)
        self.assertEqual(blocked.stdout, "")
        self.assertIn("Unsupported command", blocked.stderr)
        self.assertEqual((self.root / "main.tex").read_bytes(), before)

    def test_zip_json_and_configured_main(self) -> None:
        archive = self.root / "paper.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("paper/a.tex", r"\documentclass{article}\title{Selected}")
            stream.writestr("paper/b.tex", r"\documentclass{article}\title{Other}")
        (self.root / "fledge.toml").write_text('main = "a.tex"\n', encoding="utf-8")
        output = CliRunner().invoke(submission_text_command, [str(archive), "--json"])
        self.assertEqual(output.exit_code, 0, output.output)
        payload = json.loads(output.stdout)
        self.assertEqual(payload["schema_version"], SUBMISSION_TEXT_SCHEMA_VERSION)
        self.assertEqual(payload["fields"]["title"]["draft"], "Selected")

    def test_json_mode_prints_an_error_object_when_the_project_cannot_be_read(self) -> None:
        (self.root / "fledge.toml").write_text("jobs = 'many'\n", encoding="utf-8")
        runner = CliRunner()
        failed = runner.invoke(submission_text_command, [str(self.root), "--json"])
        self.assertEqual(failed.exit_code, 4, failed.output)
        payload = json.loads(failed.stdout)
        self.assertEqual(payload["schema_version"], SUBMISSION_TEXT_SCHEMA_VERSION)
        self.assertEqual(set(payload), {"schema_version", "error"})
        self.assertIn("Cannot export submission text", payload["error"])
        self.assertIn("jobs", payload["error"])
        self.assertIn("Cannot export submission text", failed.stderr)
        # Without --json nothing goes to stdout, as before.
        plain = runner.invoke(submission_text_command, [str(self.root)])
        self.assertEqual((plain.exit_code, plain.stdout), (4, ""))

    def test_definitions_and_unclosed_environments_are_not_ready(self) -> None:
        self.write(
            r"\documentclass{article}\newcommand{\sample}{\title{Example}}"
            r"\begin{document}\begin{abstract}Unclosed"
        )
        fields = read_submission_text(self.root, "main.tex")
        self.assertEqual(fields["title"].status, "missing")
        self.assertEqual(fields["abstract"].status, "unresolved")

    def test_grouped_prose_accents_and_urls_in_environment(self) -> None:
        self.write(
            r"\documentclass{article}\begin{document}\begin{abstract}"
            r"{A caf\'e} at \url{https://example.org/a_b}. $\alpha$"
            r"\end{abstract}\end{document}"
        )
        abstract = read_submission_text(self.root, "main.tex")["abstract"]
        self.assertEqual(abstract.status, "ready", abstract)
        self.assertEqual(abstract.draft, "A café at https://example.org/a_b. α")

    def test_keywords_preserve_explicit_phrase_separators(self) -> None:
        self.write(
            r"\documentclass{article}\keywords{machine learning\sep"
            r" formal verification\and reproducibility}"
        )
        keywords = read_submission_text(self.root, "main.tex")["keywords"]
        self.assertEqual(keywords.status, "ready")
        self.assertEqual(keywords.draft, "machine learning; formal verification; reproducibility")

    def test_abstract_spans_include_boundary(self) -> None:
        self.write(
            r"\documentclass{article}\begin{document}\input{opening}"
            r" final result.\end{abstract}\end{document}"
        )
        (self.root / "opening.tex").write_text(r"\begin{abstract}An included", encoding="utf-8")
        abstract = read_submission_text(self.root, "main.tex")["abstract"]
        self.assertEqual(abstract.status, "ready", abstract)
        self.assertEqual(abstract.draft, "An included final result.")

    def test_text_between_included_files_keeps_its_own_raw_source(self) -> None:
        # The space between the two inputs also occurs, blanked, inside the verbatim span.
        self.write(
            r"\documentclass{article}\begin{document}"
            r"\begin{abstract}\verb|ab| \input{first} \input{second}\end{abstract}"
            r"\end{document}"
        )
        (self.root / "first.tex").write_text("one", encoding="utf-8")
        (self.root / "second.tex").write_text("two", encoding="utf-8")
        abstract = read_submission_text(self.root, "main.tex")["abstract"]
        self.assertEqual(abstract.draft, r"\verb|ab| one two")
        self.assertNotIn("Could not recover original source text", abstract.issues)

    def test_control_text_is_visible_and_blocks_plain_export(self) -> None:
        self.write("\\documentclass{article}\\title{Safe\x1b[31m text\u202e}")
        runner = CliRunner()
        plain = runner.invoke(
            submission_text_command, [str(self.root), "--isolated", "--field", "title"]
        )
        self.assertEqual(plain.exit_code, 3)
        self.assertEqual(plain.stdout, "")
        preview = runner.invoke(submission_text_command, [str(self.root), "--isolated"])
        self.assertEqual(preview.exit_code, 3)
        self.assertNotIn("\x1b", preview.stdout)
        self.assertNotIn("\u202e", preview.stdout)
        self.assertIn(r"\u001b", preview.stdout)
        fields = read_submission_text(self.root, "main.tex")
        self.assertEqual(fields["title"].status, "unresolved")

    def test_private_use_text_is_visible_and_needs_review_like_other_unsafe_characters(
        self,
    ) -> None:
        self.write("\\documentclass{article}\\title{Symbol \ue000 here}")
        title = read_submission_text(self.root, "main.tex")["title"]
        self.assertEqual(title.status, "unresolved")
        self.assertEqual(title.draft, "Symbol \\ue000 here")
        self.assertIn("private-use", " ".join(title.issues))

    def test_cli_conflicting_config_and_import_budget(self) -> None:
        self.write(r"\documentclass{article}\title{A paper}")
        config = self.root / "fledge.toml"
        config.write_text("max_bytes = 10\n", encoding="utf-8")
        runner = CliRunner()
        conflict = runner.invoke(
            submission_text_command, [str(self.root), "--isolated", "--config", str(config)]
        )
        self.assertNotEqual(conflict.exit_code, 0)
        self.assertIn("cannot be combined", conflict.stderr)
        bounded = runner.invoke(submission_text_command, [str(self.root), "--field", "title"])
        self.assertEqual(bounded.exit_code, 4, bounded.output)
        self.assertEqual(bounded.stdout, "")
        self.assertIn("size limit", bounded.stderr)

    def test_locally_redefined_converter_preserves_fragment_and_blocks_export(self) -> None:
        for definition in (
            r"\renewcommand{\emph}[1]{#1 extra}",
            r"\newcommand{\emph}[1]{#1 extra}",
            r"\providecommand{\emph}[1]{#1 extra}",
            r"\def\emph#1{#1 extra}",
            r"\let\emph\relax",
        ):
            with self.subTest(definition=definition):
                self.write(r"\documentclass{article}" + definition + r"\title{A \emph{method}}")
                title = read_submission_text(self.root, "main.tex")["title"]
                self.assertEqual(title.status, "unresolved", title)
                self.assertEqual(title.draft, r"A \emph{method}")
                output = CliRunner().invoke(
                    submission_text_command, [str(self.root), "--isolated", "--field", "title"]
                )
                self.assertEqual(output.exit_code, 3)
                self.assertEqual(output.stdout, "")

    def test_definition_in_included_file_affects_only_used_commands(self) -> None:
        self.write(r"\documentclass{article}\input{macros}\title{A \emph{method}}")
        (self.root / "macros.tex").write_text(
            r"\renewcommand{\emph}[1]{#1 extra}", encoding="utf-8"
        )
        title = read_submission_text(self.root, "main.tex")["title"]
        self.assertEqual(title.status, "unresolved")
        (self.root / "macros.tex").write_text(r"\newcommand{\unrelated}{Extra}", encoding="utf-8")
        title = read_submission_text(self.root, "main.tex")["title"]
        self.assertEqual(title.status, "ready", title)
        self.assertEqual(title.draft, "A method")

    def test_nested_accent_and_math_shortcuts_respect_local_definitions(self) -> None:
        for command, fragment in (("i", r"\'{\i}"), ("j", r"\'{\j}"), ("alpha", r"$\alpha$")):
            with self.subTest(command=command):
                self.write(
                    r"\documentclass{article}\renewcommand{"
                    + "\\"
                    + command
                    + "}{X}"
                    + r"\title{An "
                    + fragment
                    + " example}"
                )
                title = read_submission_text(self.root, "main.tex")["title"]
                self.assertEqual(title.status, "unresolved", title)
                self.assertEqual(title.draft, "An " + fragment + " example")
                output = CliRunner().invoke(
                    submission_text_command, [str(self.root), "--isolated", "--field", "title"]
                )
                self.assertEqual(output.exit_code, 3)
                self.assertEqual(output.stdout, "")

    def test_active_character_definitions_preserve_fields_and_block_plain_export(self) -> None:
        for definition, field in (
            (r"\def~{CUSTOM}", "A~B"),
            (r"\let~=\relax", "A~B"),
            (r"\def={CUSTOM}", "$n=100$"),
        ):
            for included in (False, True):
                with self.subTest(definition=definition, included=included):
                    (self.root / "macros.tex").write_text(definition, encoding="utf-8")
                    self.write(
                        r"\documentclass{article}"
                        + (r"\input{macros}" if included else definition)
                        + r"\title{"
                        + field
                        + "}"
                    )
                    title = read_submission_text(self.root, "main.tex")["title"]
                    self.assertEqual(title.status, "unresolved", title)
                    self.assertEqual(title.draft, field)
                    output = CliRunner().invoke(
                        submission_text_command,
                        [str(self.root), "--isolated", "--field", "title"],
                    )
                    self.assertEqual(output.exit_code, 3)
                    self.assertEqual(output.stdout, "")
        for prefix, field, expected in (("", "A~B", "A B"), (r"\def~{CUSTOM}", "AB", "AB")):
            with self.subTest(prefix=prefix):
                self.write(r"\documentclass{article}" + prefix + r"\title{" + field + "}")
                title = read_submission_text(self.root, "main.tex")["title"]
                self.assertEqual((title.status, title.draft), ("ready", expected))

    def test_typewriter_punctuation_survives_nested_formatting_and_cli_export(self) -> None:
        literal = r"\texttt{--jobs foo--bar \emph{---} {``x''} a\_b}"
        field = "Configuring " + literal + " -- ordinary prose"
        self.write(r"\documentclass{article}\title{" + field + "}")
        title = read_submission_text(self.root, "main.tex")["title"]
        expected = "Configuring --jobs foo--bar --- ``x'' a_b – ordinary prose"
        self.assertEqual((title.status, title.draft), ("ready", expected))
        output = CliRunner().invoke(
            submission_text_command, [str(self.root), "--isolated", "--field", "title"]
        )
        self.assertEqual(output.exit_code, 0, output.output)
        self.assertEqual(output.stdout, expected + "\n")
        self.assertTrue(convert_text(r"\texttt{\unknown{--jobs}}")[1])
        self.assertEqual(
            convert_text(
                r"\texttt{outer-- \textrm{Roman--} \textsf{Sans--} \textnormal{Normal--} "
                r"{\normalfont reset--} still-- \emph{italic--}}"
            ),
            ("outer-- Roman– Sans– Normal– reset– still-- italic--", []),
        )
        self.assertEqual(convert_text(r"\texttt{\normalfont ordinary--}"), ("ordinary–", []))

    def test_brackets_after_spacing_commands_are_kept_but_line_breaks_take_options(self) -> None:
        for source, expected in (
            (r"see\ [12] for", "see [12] for"),
            (r"300\,[K] and", "300 [K] and"),
            (r"x\;[y]", "x [y]"),
            (r"a\par [b] c", "a [b] c"),
            (r"line\\[2pt] next", "line next"),
            (r"line\\ [2pt] next", "line next"),
            (r"line\linebreak[2] next", "line next"),
            (r"a\newline[b]", "a [b]"),
        ):
            with self.subTest(source=source):
                self.assertEqual(convert_text(source), (expected, []))
        self.assertEqual(
            convert_text(r"one\sep[two]\and [three]", keyword_separators=True),
            ("one; [two]; [three]", []),
        )
        self.write(r"\documentclass{article}\title{Data on 5\ [6] sites}")
        title = read_submission_text(self.root, "main.tex")["title"]
        self.assertEqual((title.status, title.draft), ("ready", "Data on 5 [6] sites"))

    def test_a_command_name_cannot_belong_to_two_text_handlers(self) -> None:
        from latexprep import submission_text

        handler = submission_text._Converter._HANDLERS["emph"]
        with self.assertRaisesRegex(RuntimeError, "emph"):
            submission_text._handler_table((["emph"], handler), (["emph"], handler))
