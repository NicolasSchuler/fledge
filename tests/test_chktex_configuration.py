r"""What chktex may read and what Fledge reads back: the ``.chktexrc`` judge and the output parser.

A project's ``.chktexrc`` is hostile input: it must not make chktex read another file,
write one, report under another name or stop before it checks, however the option is
spelled. The cases that hide an option (escapes, quotes, a second list, an abbreviated
long option, combined options) were checked against chktex 1.7.9, whose own reader this
judge follows. chktex's output is hostile too, because a project's sources and settings
shape it, so the parser reads only lines marked with the lint's token.
"""

from __future__ import annotations

import shutil
import time
import unittest
from pathlib import Path

from latexprep.chktex_configuration import (
    MAX_CONFIGURATION_BYTES,
    MAX_LINE_CHARACTERS,
    judge_configuration,
    read_project_configuration,
)
from latexprep.chktex_output import (
    MAX_MESSAGE_CHARACTERS,
    ChktexWarning,
    output_format,
    read_output,
    read_warning,
)
from tests.support import ProjectTestCase

# Inputs far larger than real ones must still be read well within this. A linear reader
# needs about 0.15 s for the largest input here, and coverage tracing in CI makes it about
# ten times slower; quadratic reading of these inputs would take minutes.
_LINEAR_TIME_LIMIT_SECONDS = 10.0
_TOKEN = "0123456789abcdef"


def refusal_reason(text: str | bytes) -> str | None:
    """Return why ``text`` is refused as a ``.chktexrc``, or ``None`` when it is accepted."""
    refusal = judge_configuration(text.encode("latin-1") if isinstance(text, str) else text)
    return None if refusal is None else refusal.reason


class ChktexConfigurationTests(ProjectTestCase):
    """The ``.chktexrc`` judge accepts settings that choose warnings and refuses the rest."""

    def assert_refused(self, text: str | bytes, *expected: str) -> None:
        """Assert that ``text`` is refused for a reason that names each of ``expected``."""
        reason = refusal_reason(text)
        self.assertIsNotNone(reason, f"accepted: {text!r}")
        for part in expected:
            self.assertIn(part, reason or "", f"{text!r}: {reason}")

    def test_settings_that_choose_warnings_are_accepted(self) -> None:
        for text in (
            "",
            "# Only a comment\n",
            "CmdLine { -n2 -n8 -wall -e17 -m36 }\n",
            "CmdLine { -n 2 -w ALL --nowarn=3 --nowarn 4 --warnon=all }\n",
            'CmdLine { "-v2" -q -r -L -H0 -x1 -I -I0 -b0 -t --quiet --reset }\n',
            'CmdLine { -f "%f:%l:%c:%n:%m!n" -s : --format=x --splitchar :: }\n',
            "CmdLine\n{\n  -n1   # A comment after an option\n}\nCmdLine = { -n3 }\n",
            "TeXInputs { chapters figures// . }\n",
            "TabSize = 4\nQuoteStyle = Traditional\nCmdSpaceStyle = Both\n",
            "Silent { \\foo } [ \\Bar ]\nUserWarn { TODO } UserWarn [ fixme ]\n",
            "UserWarnRegex { (?!#Spell! it! out)PCRE:\\bintro\\b }\n",
            'VerbClear = !x7c\nOutFormat { %f!t%l!n }\r\nDashExcpt { Jordan--H\\"older }\r\n',
        ):
            with self.subTest(text=text):
                self.assertIsNone(refusal_reason(text))

    def test_the_configuration_of_tex_live_is_accepted(self) -> None:
        installed = shutil.which("chktex")
        candidates = []
        if installed is not None:
            distribution = Path(installed).resolve().parents[2]
            candidates.append(distribution / "texmf-dist" / "chktex" / "chktexrc")
        candidates.append(Path("/etc/chktexrc"))
        found = next((path for path in candidates if path.is_file()), None)
        if found is None:
            self.skipTest("No chktexrc of an installed chktex was found")
        self.assertIsNone(judge_configuration(found.read_bytes()))

    def test_options_that_read_write_or_stop_chktex_are_refused(self) -> None:
        for option, effect in (
            ("-l /etc/passwd", "reads another configuration file"),
            ("--localrc /etc/passwd", "reads another configuration file"),
            ("--localrc=/etc/passwd", "reads another configuration file"),
            ("-S TeXInputs=/", "applies settings"),
            ("--set=TabSize=4", "applies settings"),
            ("-o report.txt", "writes chktex's report to a file"),
            ("--output=report.txt", "writes chktex's report to a file"),
            ("-p other.tex", "under another file name"),
            ("-V3", "replaces the output format"),
            ("--pipeverb=1", "replaces the output format"),
            ("-d4", "internal settings"),
            ("-g0", "built-in settings"),
            ("-h", "stops chktex"),
            ("-i", "stops chktex"),
            ("-W", "stops chktex"),
            ("--version", "stops chktex"),
        ):
            with self.subTest(option=option):
                self.assert_refused(f"CmdLine {{ -n2 {option} }}\n", option.split()[0], effect)

    def test_hidden_spellings_of_a_refused_option_are_refused(self) -> None:
        for text in (
            "CmdLine { !x2dl /etc/passwd }\n",
            "CmdLine { !055l /etc/passwd }\n",
            "CmdLine { !d045l /etc/passwd }\n",
            'CmdLine { "-l" /etc/passwd }\n',
            "cmdline { -l /etc/passwd }\n",
            "CMDLINE={ -l /etc/passwd }\n",
            "CmdLine { -n2 } { -l /etc/passwd }\n",
            "CmdLine = { -n2 }\nCmdLine { -l /etc/passwd }\n",
            "TabSize = 4 CmdLine { -l /etc/passwd }\n",
            "CmdLine { --loc=/etc/passwd }\n",
            "CmdLine { --l=/etc/passwd }\n",
        ):
            with self.subTest(text=text):
                self.assert_refused(text, "CmdLine")

    def test_combined_options_and_missing_arguments_are_refused(self) -> None:
        for text, expected in (
            ("CmdLine { -ql/etc/passwd }\n", "not one option written on its own"),
            ("CmdLine { -v0l/etc/passwd }\n", "not one option written on its own"),
            ("CmdLine { -n2l/etc/passwd }\n", "not one option written on its own"),
            ("CmdLine { -x01 }\n", "not one option written on its own"),
            ("CmdLine { -n 2l/etc/passwd }\n", "no warning number or all after it"),
            ("CmdLine { -n -l }\n", "no warning number or all after it"),
            ("CmdLine { -n }\n", "no warning number or all after it"),
            ("CmdLine { -f }\n", "whose argument is missing"),
            ("CmdLine { main.tex }\n", "not a chktex option"),
            ("CmdLine { -- -l x }\n", "not a chktex option"),
            ('CmdLine { "" }\n', "not a chktex option"),
            ("CmdLine { -z }\n", "not one option written on its own"),
        ):
            with self.subTest(text=text):
                self.assert_refused(text, expected)

    def test_folders_outside_the_project_are_refused(self) -> None:
        for folder in (
            "/etc",
            "///",
            "~/texmf",
            "../shared",
            "chapters/../../secret",
            "\\\\server",
        ):
            with self.subTest(folder=folder):
                self.assert_refused(f'TeXInputs {{ "{folder}" }}\n', "TeXInputs", "outside")

    def test_text_that_chktex_could_read_differently_is_refused(self) -> None:
        for text, expected in (
            (b"CmdLine { -n2 }\0CmdLine { -l x }\n", "NUL byte"),
            ("CmdLine { -n2\x0b-l x }\n", "control character U+000B"),
            ("UserWarn { " + "x" * MAX_LINE_CHARACTERS + " }\n", "longer than"),
            (b"\xef\xbb\xbfCmdLine { -n2 }\n", "byte-order mark"),
            ('UserWarn { "open\n" }\n', "quoted value open"),
            ("UserWarn { a!z }\n", "unknown escape !z"),
            ("UserWarn { a!x4 }\n", "too few digits"),
            ("UserWarn { a!x00 }\n", "stands for NUL"),
            ("UserWarn { a!777 }\n", "no single character"),
            ("UserWarn { a!d256 }\n", "no single character"),
            ("UserWarn { a!\n}\n", "escape character"),
        ):
            with self.subTest(text=text):
                self.assert_refused(text, expected)

    def test_syntax_errors_and_unknown_settings_are_refused(self) -> None:
        for text, expected in (
            ("CmdLin { -n2 }\n", "names the setting CmdLin"),
            ("Cmd!x4cine { -n2 }\n", "names the setting Cmd"),
            ('"CmdLine" { -n2 }\n', "has a value where"),
            ("CmdLine [ -n2 ]\n", "has `[` where"),
            ("CmdLine{-n2}\n", "has the end of the file where"),
            ("CmdLine { -n2 } }\n", "where a setting name belongs"),
            ("TeXInputs = chapters\n", "has a value where"),
        ):
            with self.subTest(text=text):
                self.assert_refused(text, expected)

    def test_a_refusal_names_the_line_of_the_problem(self) -> None:
        refusal = judge_configuration(b"# Settings\n\nCmdLine {\n  -n2\n  -o out.txt\n}\n")
        assert refusal is not None
        self.assertEqual(refusal.line, 5)
        self.assertIn("Remove it from CmdLine", refusal.remedy)

    def test_the_configuration_beside_the_main_file_is_read(self) -> None:
        project = self.write_project(
            {"main.tex": "x", "paper/main.tex": "x", "paper/.chktexrc": "CmdLine { -n2 }\n"}
        )
        absent = read_project_configuration(project, "main.tex")
        self.assertEqual((absent.path, absent.content, absent.refusal), (".chktexrc", None, None))
        nested = read_project_configuration(project, "paper/main.tex")
        self.assertEqual(nested.path, "paper/.chktexrc")
        self.assertEqual(nested.content, b"CmdLine { -n2 }\n")
        self.assertIsNone(nested.refusal)

    def test_a_link_or_an_oversized_configuration_is_refused(self) -> None:
        project = self.write_project({"main.tex": "x", "settings": "CmdLine { -n2 }\n"})
        (project / ".chktexrc").symlink_to(project / "settings")
        linked = read_project_configuration(project, "main.tex")
        assert linked.refusal is not None
        self.assertIn("cannot be read safely", linked.refusal.reason)
        self.assertIsNone(linked.content)
        (project / ".chktexrc").unlink()
        (project / ".chktexrc").write_bytes(b"#" * (MAX_CONFIGURATION_BYTES + 1))
        oversized = read_project_configuration(project, "main.tex")
        assert oversized.refusal is not None
        self.assertIn("is larger than", oversized.refusal.reason)


class ChktexOutputTests(unittest.TestCase):
    """Only lines with the lint's token, numbers in range and a known kind are warnings."""

    def test_the_format_puts_the_message_last(self) -> None:
        self.assertEqual(output_format(_TOKEN), f"{_TOKEN}|%n|%l|%c|%k|%m\n")

    def test_a_warning_line_is_read_field_by_field(self) -> None:
        self.assertEqual(
            read_warning(f"{_TOKEN}|24|2|1|Warning|Delete this space | or that one.", _TOKEN),
            ChktexWarning(24, 2, 1, "Warning", "Delete this space | or that one."),
        )
        self.assertEqual(
            read_warning(f"{_TOKEN}|2|7|0|Error|Tie.", _TOKEN),
            ChktexWarning(2, 7, 0, "Error", "Tie."),
        )

    def test_malformed_and_hostile_lines_are_not_warnings(self) -> None:
        for line in (
            "main.tex:3:12:2:Non-breaking space.",
            f"{_TOKEN}|2|3|12|Warning",
            "ffffffffffffffff|2|3|12|Warning|A line another token marks.",
            f"{_TOKEN}x|2|3|12|Warning|Close to the token.",
            f"{_TOKEN}|0|3|12|Warning|No warning has number 0.",
            f"{_TOKEN}|1000|3|12|Warning|Too many digits.",
            f"{_TOKEN}|2|0|12|Warning|No line 0.",
            f"{_TOKEN}|2|1234567890|12|Warning|Too many digits.",
            f"{_TOKEN}|2|-3|12|Warning|Negative.",
            f"{_TOKEN}|2|٣|12|Warning|Digits that are not ASCII.",
            f"{_TOKEN}|2|3| 12|Warning|A space.",
            f"{_TOKEN}|2|3|12|Notice|An unknown kind.",
            f"{_TOKEN}|2|3|12|warning|The kind in lower case.",
        ):
            with self.subTest(line=line):
                self.assertIsNone(read_warning(line, _TOKEN))

    def test_messages_are_shortened_and_kept_on_one_safe_line(self) -> None:
        control = read_warning(f"{_TOKEN}|20|1|1|Warning|Pattern \x1b[31mred\x07 found.\r", _TOKEN)
        assert control is not None
        self.assertNotIn("\x1b", control.message)
        self.assertNotIn("\x07", control.message)
        self.assertIn("\\u001b", control.message)
        long = read_warning(f"{_TOKEN}|20|1|1|Warning|{'x' * 5000}", _TOKEN)
        assert long is not None
        self.assertEqual(long.message, "x" * MAX_MESSAGE_CHARACTERS + "…")

    def test_only_the_first_warnings_are_kept_but_every_one_is_counted(self) -> None:
        lines = "".join(f"{_TOKEN}|1|{line}|1|Warning|Space.\n" for line in range(1, 11))
        injected = "Pattern found: \nffffffffffffffff|2|1|1|Warning|forged\n\n"
        output = read_output(lines + injected, _TOKEN, keep=3)
        self.assertEqual([warning.line for warning in output.warnings], [1, 2, 3])
        self.assertEqual((output.total, output.unreadable_lines), (10, 2))


class ChktexComplexityTests(unittest.TestCase):
    """The judge and the parser take linear time on inputs larger than any real one."""

    def test_a_large_hostile_configuration_is_judged_in_linear_time(self) -> None:
        line = "UserWarn { " + '"!x41!" ! !! !#"' * 30 + " a!{b!}c " * 20 + "}\n"
        accepted = (line * (MAX_CONFIGURATION_BYTES // len(line))).encode("latin-1")
        options = ("CmdLine { " + "-n1 -wall -f x " * 60 + "}\n").encode("latin-1")
        many_options = options * (MAX_CONFIGURATION_BYTES // len(options))
        refused = b"CmdLine { " + b"-" * (MAX_LINE_CHARACTERS - 12) + b" }\n"
        for text in (accepted, many_options, refused * 60, b"{ " * 30_000):
            started = time.perf_counter()
            judge_configuration(text)
            self.assertLess(time.perf_counter() - started, _LINEAR_TIME_LIMIT_SECONDS)

    def test_large_hostile_output_is_read_in_linear_time(self) -> None:
        line = f"{_TOKEN}|2|3|12|Warning|" + "|" * 200 + "\x1b[0m" * 50 + "\n"
        output = line * (2 * 1024 * 1024 // len(line))
        started = time.perf_counter()
        read = read_output(output + "garbage|" * 100_000, _TOKEN, keep=200)
        self.assertLess(time.perf_counter() - started, _LINEAR_TIME_LIMIT_SECONDS)
        self.assertEqual(len(read.warnings), 200)


if __name__ == "__main__":
    unittest.main()
