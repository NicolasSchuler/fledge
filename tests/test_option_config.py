from __future__ import annotations

import re
import types
import unittest
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from functools import partial
from typing import ClassVar, Literal, Union, get_args, get_origin, get_type_hints

from latexprep.config import Settings
from latexprep.errors import SettingsError
from latexprep.option_config import (
    EIGHT_BIT_VALUE,
    NONZERO_FRACTION,
    POSITIVE_INTEGER,
    Bounds,
    TextFormat,
    closest_names,
    read_options,
    read_top_level_options,
    require_choice,
    require_int,
    require_integers,
    require_number,
    require_numbers,
    require_pairs,
    require_records,
    require_text,
    require_unique_strings,
    validate_scalars,
    with_suggestions,
)


class KitTestCase(unittest.TestCase):
    def assert_accepts(self, check: Callable[[object], None], values: list[object]) -> None:
        for value in values:
            with self.subTest(accepted=value):
                check(value)

    def assert_rejects(
        self, check: Callable[[object], None], values: list[object], message: str
    ) -> None:
        for value in values:
            with self.subTest(rejected=value), self.assertRaises(SettingsError) as raised:
                check(value)
            self.assertEqual(str(raised.exception), message)
            self.assertEqual(raised.exception.settings, ("x",))


class RequireIntTests(KitTestCase):
    def test_limits_decide_the_wording_and_the_accepted_values(self) -> None:
        cases: tuple[tuple[dict[str, int], list[object], list[object], str], ...] = (
            ({"minimum": 1}, [1, 40], [0, -3], "x must be a positive integer"),
            ({"minimum": 0}, [0, 7], [-1], "x must be a nonnegative integer"),
            ({"minimum": 1, "maximum": 64}, [1, 64], [0, 65], "x must be an integer from 1 to 64"),
            ({"minimum": 256}, [256], [255], "x must be an integer of at least 256"),
            ({"maximum": 5}, [-4, 5], [6], "x must be an integer of at most 5"),
            ({}, [-4, 0, 99], [], "x must be an integer"),
        )
        for limits, accepted, rejected, message in cases:
            check = partial(require_int, "x", **limits)
            with self.subTest(limits=limits):
                self.assert_accepts(check, accepted)
                self.assert_rejects(check, [*rejected, True, 2.0, "3", None, [1]], message)

    def test_none_is_accepted_only_on_request(self) -> None:
        require_int("x", None, minimum=1, allow_none=True)
        with self.assertRaises(SettingsError):
            require_int("x", None, minimum=1)

    def test_large_limits_are_written_in_full(self) -> None:
        with self.assertRaisesRegex(SettingsError, "from 1024 to 4194304"):
            require_int("x", 0, minimum=1024, maximum=4_194_304)


class RequireNumberTests(KitTestCase):
    def test_limits_decide_the_wording_and_the_accepted_values(self) -> None:
        cases: tuple[tuple[dict[str, object], list[object], list[object], str], ...] = (
            (
                {"minimum": 0, "exclusive_minimum": True},
                [0.001, 3, 1e9],
                [0, -1, 0.0],
                "x must be a positive number",
            ),
            ({"minimum": 0}, [0, 0.0, 12.5], [-0.1], "x must be a nonnegative number"),
            (
                {"minimum": 0, "maximum": 1},
                [0, 0.5, 1],
                [1.01, -0.01],
                "x must be a number from 0 to 1",
            ),
            (
                {"minimum": 0, "maximum": 1, "exclusive_minimum": True},
                [0.01, 1],
                [0, 1.5],
                "x must be a number greater than 0 and at most 1",
            ),
            ({"minimum": 1.5}, [1.5, 2], [1.4], "x must be a number of at least 1.5"),
            (
                {"minimum": 0.5, "exclusive_minimum": True},
                [0.6],
                [0.5],
                "x must be a number greater than 0.5",
            ),
            ({"maximum": 60}, [-5, 60], [60.5], "x must be a number of at most 60"),
            ({}, [-5, 0, 2.5], [], "x must be a finite number"),
        )
        for limits, accepted, rejected, message in cases:
            check = partial(require_number, "x", **limits)  # type: ignore[arg-type]
            with self.subTest(limits=limits):
                self.assert_accepts(check, accepted)
                bad = [*rejected, True, "1", None, float("nan"), float("inf"), float("-inf")]
                self.assert_rejects(check, bad, message)

    def test_none_is_accepted_only_on_request(self) -> None:
        require_number("x", None, allow_none=True)
        with self.assertRaises(SettingsError):
            require_number("x", None)


class RequireChoiceTests(KitTestCase):
    def test_a_value_must_be_one_of_the_choices_in_the_order_given(self) -> None:
        check = partial(require_choice, "x", choices=("pdflatex", "xelatex", "lualatex"))
        self.assert_accepts(check, ["pdflatex", "lualatex"])
        self.assert_rejects(
            check,
            ["latex", "", None, 3, ["pdflatex"]],
            "x must be one of pdflatex, xelatex, lualatex",
        )

    def test_types_are_compared_too(self) -> None:
        self.assert_rejects(
            partial(require_choice, "x", choices=(1, 2)), [True, 1.0], "x must be one of 1, 2"
        )
        self.assert_rejects(
            partial(require_choice, "x", choices=(True,)), [1], "x must be one of True"
        )

    def test_none_is_accepted_only_on_request(self) -> None:
        require_choice("x", None, ("a",), allow_none=True)
        with self.assertRaises(SettingsError):
            require_choice("x", None, ("a",))


class RequireTextTests(KitTestCase):
    def test_text_must_be_nonempty_and_free_of_nul(self) -> None:
        check = partial(require_text, "x")
        self.assert_accepts(check, ["a", " padded ", "ünï"])
        self.assert_rejects(
            check, ["", "  \t", "a\0b", None, 3, b"a"], "x must be a nonempty string"
        )

    def test_expected_text_and_pattern_shape_the_message(self) -> None:
        check = partial(
            require_text, "x", expected="a version such as 3.3", pattern=re.compile(r"\d+\.\d+")
        )
        self.assert_accepts(check, ["3.3", "10.20"])
        self.assert_rejects(check, ["3", "v3.3", "3.3 "], "x must be a version such as 3.3")

    def test_length_limit_is_named_in_the_message(self) -> None:
        check = partial(require_text, "x", max_length=4)
        self.assert_accepts(check, ["abcd"])
        self.assert_rejects(check, ["abcde"], "x must be at most 4 characters long")

    def test_blank_text_and_none_are_accepted_only_on_request(self) -> None:
        require_text("x", "", allow_blank=True)
        require_text("x", "   ", allow_blank=True)
        require_text("x", None, allow_none=True)
        with self.assertRaises(SettingsError):
            require_text("x", None)
        with self.assertRaises(SettingsError):
            require_text("x", "a\0", allow_blank=True)


class RequireUniqueStringsTests(KitTestCase):
    def test_a_tuple_of_distinct_nonempty_strings_is_accepted(self) -> None:
        check = partial(require_unique_strings, "x")
        self.assert_accepts(check, [(), ("a",), ("a", "b")])
        self.assert_rejects(
            check,
            [("a", ""), ("a", 3), ("a", "  "), ("a\0",), "ab", {"a"}, 3, None],
            "x must be a list of nonempty strings",
        )

    def test_a_list_object_is_named_as_one(self) -> None:
        with self.assertRaisesRegex(SettingsError, "x must be given as a tuple, not a list"):
            require_unique_strings("x", ["a"])

    def test_repeated_entries_are_named_once_each(self) -> None:
        self.assert_rejects(
            partial(require_unique_strings, "x"),
            [("b", "a", "b", "a", "b"), ("a", "b", "a", "b")],
            "x lists 'a', 'b' more than once",
        )

    def test_surrounding_whitespace_is_rejected_unless_allowed(self) -> None:
        with self.assertRaisesRegex(SettingsError, "x entry ' a' must not start or end with white"):
            require_unique_strings("x", (" a",))
        require_unique_strings("x", (" a", "b "), allow_surrounding_whitespace=True)

    def test_entry_count_and_length_limits_are_named(self) -> None:
        with self.assertRaisesRegex(SettingsError, "x may list at most 2 entries"):
            require_unique_strings("x", ("a", "b", "c"), max_items=2)
        require_unique_strings("x", ("a", "b"), max_items=2)
        with self.assertRaisesRegex(SettingsError, "x entries may be at most 3 characters long"):
            require_unique_strings("x", ("abcd",), max_length=3)
        require_unique_strings("x", ("abc",), max_length=3)

    def test_choices_and_formats_restrict_the_entries(self) -> None:
        with self.assertRaisesRegex(SettingsError, "x entry 'date' must be one of title, author"):
            require_unique_strings("x", ("title", "date"), choices=("title", "author"))
        with self.assertRaisesRegex(SettingsError, "x entry 'Nope' must be one of Author, Title"):
            require_unique_strings("x", ("Title", "Nope"), choices=frozenset({"Title", "Author"}))
        extension = TextFormat(re.compile(r"\.\w+"), "a dot-prefixed extension")
        require_unique_strings("x", (".tex", ".bib"), item_format=extension)
        with self.assertRaisesRegex(
            SettingsError, "x entry 'tex' must be a dot-prefixed extension"
        ):
            require_unique_strings("x", (".tex", "tex"), item_format=extension)

    def test_a_long_entry_is_shortened_in_the_message(self) -> None:
        with self.assertRaises(SettingsError) as raised:
            require_unique_strings("x", ("a" * 500,), item_format=TextFormat(re.compile("b"), "b"))
        self.assertLess(len(str(raised.exception)), 120)

    def test_none_is_accepted_only_on_request(self) -> None:
        require_unique_strings("x", None, allow_none=True)
        with self.assertRaises(SettingsError):
            require_unique_strings("x", None)


@dataclass(frozen=True)
class Record:
    page: int


class RequireRecordsTests(KitTestCase):
    def test_a_tuple_of_records_is_accepted(self) -> None:
        check = partial(require_records, "x", record_type=Record)
        self.assert_accepts(check, [(), (Record(1), Record(1))])
        self.assert_rejects(
            check, [(Record(1), 3), ("a",), Record(1), None], "x must be a list of Record records"
        )

    def test_count_limit_and_distinct_records_are_named(self) -> None:
        with self.assertRaisesRegex(SettingsError, "x may list at most 1 entries"):
            require_records("x", (Record(1), Record(2)), Record, max_items=1)
        require_records("x", (Record(1), Record(2)), Record, distinct_by=lambda record: record.page)
        with self.assertRaisesRegex(SettingsError, "x must not list the same page more than once"):
            require_records(
                "x",
                (Record(1), Record(1)),
                Record,
                distinct_by=lambda record: record.page,
                distinct_description="page",
            )


class RequirePairsTests(KitTestCase):
    def test_every_item_is_checked_with_its_index_path(self) -> None:
        check = partial(
            require_pairs,
            "x",
            first=require_text,
            second=partial(require_number, minimum=0, exclusive_minimum=True),
        )
        check((("photo", 300), ("line", 1.5)))
        for rows, message in (
            ((("photo", 0),), "x[0][1] must be a positive number"),
            ((("photo", 3), ("", 3)), "x[1][0] must be a nonempty string"),
            ((("photo", 3), ("photo", 4)), "x lists 'photo' more than once"),
            ((("photo",),), "x must be a list of two-item lists"),
            ([("photo", 3)], "x must be given as a tuple, not a list"),
            (("photo",), "x must be a list of two-item lists"),
        ):
            with self.subTest(rows=rows), self.assertRaises(SettingsError) as raised:
                check(rows)
            self.assertEqual(str(raised.exception), message)

    def test_first_items_may_repeat_on_request_and_the_count_is_limited(self) -> None:
        require_pairs(
            "x",
            (("a", "1"), ("a", "2")),
            first=require_text,
            second=require_text,
            unique_first=False,
        )
        with self.assertRaisesRegex(SettingsError, "x may list at most 1 entries"):
            require_pairs(
                "x", (("a", "1"), ("b", "2")), first=require_text, second=require_text, max_items=1
            )


class RequireIntegersAndNumbersTests(KitTestCase):
    def test_each_integer_is_checked_with_its_index(self) -> None:
        require_integers("pages", (1, 5), minimum=1)
        for values, message in (
            ((1, 0), "pages[1] must be a positive integer"),
            ((True,), "pages[0] must be a positive integer"),
            (["a"], "pages must be given as a tuple, not a list"),
        ):
            with self.subTest(values=values), self.assertRaises(SettingsError) as raised:
                require_integers("pages", values, minimum=1)
            self.assertEqual(str(raised.exception), message)
        with self.assertRaisesRegex(SettingsError, "pages may list at most 1 entries"):
            require_integers("pages", (1, 2), minimum=1, max_items=1)

    def test_a_fixed_number_of_numbers_is_required(self) -> None:
        check = partial(
            require_numbers, "margins", count=3, minimum=0, maximum=1, description="(r, g, b)"
        )
        check((0, 0.5, 1))
        for values, message in (
            ((0, 1), "margins must be a list of 3 numbers (r, g, b)"),
            ((0, 0.5, 1.5), "margins[2] must be a number from 0 to 1"),
            ((0, "a", 1), "margins[1] must be a number from 0 to 1"),
        ):
            with self.subTest(values=values), self.assertRaises(SettingsError) as raised:
                check(values)
            self.assertEqual(str(raised.exception), message)
        require_numbers("margins", None, count=3, allow_none=True)


@dataclass(frozen=True)
class Sample:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "count": Bounds(1, 5),
        "gray": EIGHT_BIT_VALUE,
        "share": NONZERO_FRACTION,
        "seconds": POSITIVE_INTEGER,
    }

    flag: bool = False
    count: int = 2
    gray: int | None = None
    share: float = 0.5
    seconds: int = 3
    mode: Literal["fast", "slow"] = "fast"
    maybe_mode: Literal["a", "b"] | None = None
    label: str = "text"
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_scalars(self)


class ValidateScalarsTests(unittest.TestCase):
    def test_defaults_and_valid_values_are_accepted(self) -> None:
        Sample()
        Sample(flag=True, count=5, gray=0, share=1, seconds=1, mode="slow", maybe_mode="b")

    def test_each_field_kind_is_checked_against_its_annotation_and_bounds(self) -> None:
        cases = (
            ({"flag": 1}, "flag must be a boolean (true or false)"),
            ({"flag": None}, "flag must be a boolean (true or false)"),
            ({"count": 6}, "count must be an integer from 1 to 5"),
            ({"count": True}, "count must be an integer from 1 to 5"),
            ({"gray": 256}, "gray must be an integer from 0 to 255"),
            ({"share": 0}, "share must be a number greater than 0 and at most 1"),
            ({"share": float("nan")}, "share must be a number greater than 0 and at most 1"),
            ({"seconds": 0}, "seconds must be a positive integer"),
            ({"mode": "medium"}, "mode must be one of fast, slow"),
            ({"mode": None}, "mode must be one of fast, slow"),
            ({"maybe_mode": "c"}, "maybe_mode must be one of a, b"),
        )
        for values, message in cases:
            with self.subTest(values=values), self.assertRaises(SettingsError) as raised:
                Sample(**values)  # type: ignore[arg-type]
            self.assertEqual(str(raised.exception), message)
            self.assertEqual(raised.exception.settings, (next(iter(values)),))

    def test_optional_fields_accept_none(self) -> None:
        Sample(gray=None, maybe_mode=None)


def default_option_groups() -> list[tuple[str, object]]:
    """Every option dataclass of Settings that can be built from its defaults."""
    groups: list[tuple[str, object]] = [("Settings", Settings())]
    for name, kind in get_type_hints(Settings).items():
        if isinstance(kind, type) and is_dataclass(kind) and name != "checks":
            groups.append((name, getattr(Settings(), name)))
    groups.append(("Settings.workflow.comparison", Settings().workflow.comparison))
    return groups


class EveryOptionGroupValidatesItsScalarsTests(unittest.TestCase):
    """A scalar field that a class forgot to validate would accept a value of the wrong type."""

    def test_wrong_types_are_rejected_for_every_scalar_field(self) -> None:
        wrong = {"boolean": 1, "integer": True, "number": "1", "choice": 12345}
        checked = 0
        for group, options in default_option_groups():
            kind = type(options)
            hints = get_type_hints(kind)
            for item in fields(kind):  # type: ignore[arg-type]
                annotation = hints[item.name]
                tag = _scalar_tag(annotation)
                if tag is None:
                    continue
                with self.subTest(group=group, field=item.name):
                    with self.assertRaises(SettingsError) as raised:
                        replace(options, **{item.name: wrong[tag]})  # type: ignore[type-var]
                    self.assertIn(item.name, str(raised.exception))
                    checked += 1
        self.assertGreater(checked, 100)


def _scalar_tag(annotation: object) -> str | None:
    if get_origin(annotation) in {Union, types.UnionType}:
        variants = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(variants) != 1:
            return None
        annotation = variants[0]
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if get_origin(annotation) is Literal:
        return "choice"
    return None


class ReadOptionsTests(unittest.TestCase):
    def test_a_wrongly_typed_value_still_states_the_allowed_range(self) -> None:
        for value in ("high", 9, 0, True, 2.5):
            with self.subTest(value=value), self.assertRaises(SettingsError) as raised:
                read_options(Sample, {"count": value}, "sample")
            self.assertEqual(str(raised.exception), "sample.count must be an integer from 1 to 5")
            self.assertEqual(raised.exception.settings, ("sample",))

    def test_choices_and_numbers_name_their_location(self) -> None:
        with self.assertRaisesRegex(SettingsError, "sample.mode must be one of fast, slow"):
            read_options(Sample, {"mode": "medium"}, "sample")
        with self.assertRaisesRegex(
            SettingsError, "sample.share must be a number greater than 0 and at most 1"
        ):
            read_options(Sample, {"share": 1.5}, "sample")

    def test_a_list_becomes_a_tuple_and_a_scalar_is_named_as_wrong(self) -> None:
        self.assertEqual(read_options(Sample, {"names": ["a", "b"]}, "sample").names, ("a", "b"))
        with self.assertRaisesRegex(SettingsError, "sample.names must be a list"):
            read_options(Sample, {"names": "a"}, "sample")

    def test_an_unknown_key_suggests_the_closest_name_and_lists_the_valid_ones(self) -> None:
        with self.assertRaises(SettingsError) as raised:
            read_options(Sample, {"secnods": 3}, "sample")
        message = str(raised.exception)
        self.assertIn("Unknown sample setting(s): secnods (did you mean seconds?)", message)
        self.assertIn(
            "Valid sample settings: count, flag, gray, label, maybe_mode, mode, names, seconds, "
            "share.",
            message,
        )
        self.assertEqual(raised.exception.settings, ("sample",))

    def test_unknown_keys_without_a_close_match_are_listed_plainly(self) -> None:
        with self.assertRaisesRegex(
            SettingsError, r"Unknown sample setting\(s\): qqq, zzz\. Valid sample settings: "
        ):
            read_options(Sample, {"zzz": 1, "qqq": 2}, "sample")

    def test_a_failed_construction_is_reported_under_the_table(self) -> None:
        @dataclass(frozen=True)
        class Ordered:
            low: int = 1
            high: int = 2

            def __post_init__(self) -> None:
                if self.low > self.high:
                    raise SettingsError("low must not exceed high", ("low", "high"))

        with self.assertRaises(SettingsError) as raised:
            read_options(Ordered, {"low": 3, "high": 2}, "ordered")
        self.assertEqual(
            str(raised.exception), "Invalid ordered settings: low must not exceed high"
        )
        self.assertEqual(raised.exception.settings, ("ordered",))

    def test_an_existing_instance_and_a_non_table_are_handled(self) -> None:
        sample = Sample()
        self.assertIs(read_options(Sample, sample, "sample"), sample)
        with self.assertRaisesRegex(SettingsError, "sample must be a settings table"):
            read_options(Sample, 3, "sample")


class ReadTopLevelOptionsTests(unittest.TestCase):
    def test_each_key_is_its_own_location(self) -> None:
        self.assertEqual(read_top_level_options(Sample, {"count": 3, "names": ["a"]}).names, ("a",))
        for values, message, names in (
            ({"count": 0}, "count must be an integer from 1 to 5", ("count",)),
            ({"count": "many"}, "count must be an integer from 1 to 5", ("count",)),
            ({"names": "a"}, "names must be a list", ("names",)),
            ({"names": [3]}, "names[0] must be a string", ("names",)),
            ({"mode": "x"}, "mode must be one of fast, slow", ("mode",)),
        ):
            with self.subTest(values=values), self.assertRaises(SettingsError) as raised:
                read_top_level_options(Sample, values)
            self.assertEqual(str(raised.exception), message)
            self.assertEqual(raised.exception.settings, names)

    def test_errors_of_the_options_keep_their_own_names(self) -> None:
        @dataclass(frozen=True)
        class Together:
            width: float | None = None
            height: float | None = None

            def __post_init__(self) -> None:
                if (self.width is None) != (self.height is None):
                    raise SettingsError("width and height go together", ("width", "height"))

        with self.assertRaises(SettingsError) as raised:
            read_top_level_options(Together, {"width": 3})
        self.assertEqual(raised.exception.settings, ("width", "height"))


class SuggestionTests(unittest.TestCase):
    def test_the_closest_known_name_is_suggested_only_when_one_resembles(self) -> None:
        known = ["max_pages", "jobs", "engine"]
        self.assertEqual(closest_names(["max_pagse", "zzz"], known), {"max_pagse": "max_pages"})

    def test_suggestions_follow_the_names_they_belong_to(self) -> None:
        self.assertEqual(with_suggestions(["a", "b"], {"b": "bee"}), "a, b (did you mean bee?)")


if __name__ == "__main__":
    unittest.main()
