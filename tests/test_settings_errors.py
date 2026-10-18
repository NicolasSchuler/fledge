"""A misconfigured settings file names the key at fault and states what it accepts."""

from __future__ import annotations

import unittest
from pathlib import Path

from latexprep.config import SETTING_NAMES, parse_settings_text
from latexprep.errors import SettingsError

# Each case is the TOML text, the top-level settings the error names, and the exact message
# (or, for an invalid value inside a table, the message after "Invalid <table> settings: ").
TOP_LEVEL_CASES = (
    ("jobs = 0", ("jobs",), "jobs must be an integer from 1 to 64"),
    ("jobs = 65", ("jobs",), "jobs must be an integer from 1 to 64"),
    ('jobs = "4"', ("jobs",), "jobs must be an integer from 1 to 64"),
    ("render_jobs = 100", ("render_jobs",), "render_jobs must be an integer from 1 to 64"),
    ("memory_mb = 100", ("memory_mb",), "memory_mb must be an integer of at least 256"),
    (
        "build_memory_mb = 64",
        ("build_memory_mb",),
        "build_memory_mb must be an integer of at least 128",
    ),
    ("timeout_seconds = 0", ("timeout_seconds",), "timeout_seconds must be a positive integer"),
    ("max_pages = 0", ("max_pages",), "max_pages must be a positive integer"),
    (
        "source_date_epoch = -1",
        ("source_date_epoch",),
        "source_date_epoch must be an integer from 0 to 253402300799",
    ),
    ('engine = "word"', ("engine",), "engine must be one of pdflatex, xelatex, lualatex"),
    ('layout = "tidy"', ("layout",), "layout must be one of preserve, flat"),
    (
        "apply_formatting = 1",
        ("apply_formatting",),
        "apply_formatting must be a boolean (true or false)",
    ),
    ('main = ""', ("main",), "main must be a nonempty relative filename"),
    (
        'match_source_pdf_metadata = ["date"]',
        ("match_source_pdf_metadata",),
        "match_source_pdf_metadata entry 'date' must be one of title, author",
    ),
    (
        'match_source_pdf_metadata = ["title", "title"]',
        ("match_source_pdf_metadata",),
        "match_source_pdf_metadata lists 'title' more than once",
    ),
    ("wrap_length = 5", ("wrap_length",), "wrap_length must be an integer of at least 20"),
    ("tab_size = 40", ("tab_size",), "tab_size must be an integer from 1 to 16"),
)

# The manuscript and pdf constraints are top-level keys, so each key is its own location.
FLATTENED_CASES = (
    (
        "abstract_min_words = -1",
        ("abstract_min_words",),
        "abstract_min_words must be a nonnegative integer",
    ),
    (
        'abstract_min_words = "ten"',
        ("abstract_min_words",),
        "abstract_min_words must be a nonnegative integer",
    ),
    (
        "keywords_max_count = true",
        ("keywords_max_count",),
        "keywords_max_count must be a nonnegative integer",
    ),
    (
        "abstract_min_words = 20\nabstract_max_words = 10",
        ("abstract_min_words", "abstract_max_words"),
        "abstract_min_words must not exceed abstract_max_words",
    ),
    ('required_sections = "intro"', ("required_sections",), "required_sections must be a list"),
    ("required_sections = [3]", ("required_sections",), "required_sections[0] must be a string"),
    (
        'required_class_options = ["draft", "draft"]',
        ("required_class_options",),
        "required_class_options lists 'draft' more than once",
    ),
    (
        'forbidden_packages = [" amsmath"]',
        ("forbidden_packages",),
        "forbidden_packages entry ' amsmath' must not start or end with whitespace",
    ),
    (
        'expected_document_class = "a,b"',
        ("expected_document_class",),
        "expected_document_class must be a single nonempty class name",
    ),
    ("require_abstract = 1", ("require_abstract",), "require_abstract must be a boolean"),
    (
        "expected_page_width_pt = 612",
        ("expected_page_width_pt", "expected_page_height_pt"),
        "expected_page_width_pt and expected_page_height_pt must be set together",
    ),
    ("min_image_dpi = 0", ("min_image_dpi",), "min_image_dpi must be a positive number"),
    ('min_image_dpi = "high"', ("min_image_dpi",), "min_image_dpi must be a positive number"),
    (
        "page_size_tolerance_pt = -1",
        ("page_size_tolerance_pt",),
        "page_size_tolerance_pt must be a nonnegative number",
    ),
    ("forbid_forms = 1", ("forbid_forms",), "forbid_forms must be a boolean"),
)

# An invalid value inside a table names the table, and the message names the key.
TABLE_CASES = (
    ("pdf_checks", "raster_dpi = 5", "pdf_checks.raster_dpi must be an integer from 18 to 144"),
    (
        "pdf_checks",
        'raster_dpi = "high"',
        "pdf_checks.raster_dpi must be an integer from 18 to 144",
    ),
    (
        "pdf_checks",
        "background_gray = 300",
        "pdf_checks.background_gray must be an integer from 0 to 255",
    ),
    (
        "pdf_checks",
        "ink_difference = 0",
        "pdf_checks.ink_difference must be an integer from 1 to 255",
    ),
    (
        "pdf_checks",
        "overlap_min_area_ratio = 1.5",
        "pdf_checks.overlap_min_area_ratio must be a number greater than 0 and at most 1",
    ),
    (
        "pdf_checks",
        "min_text_contrast = 25",
        "pdf_checks.min_text_contrast must be a number from 1 to 21",
    ),
    (
        "pdf_checks",
        "min_text_contrast = 4.5",
        "Invalid pdf_checks settings: min_text_contrast requires contrast_background_rgb",
    ),
    (
        "pdf_checks",
        "contrast_background_rgb = [1, 2, 0]",
        "Invalid pdf_checks settings: contrast_background_rgb[1] must be a number from 0 to 1",
    ),
    (
        "pdf_checks",
        'required_metadata = ["Foo"]',
        "required_metadata entry 'Foo' must be one of Author, Creator, Keywords, Producer, "
        "Subject, Title",
    ),
    (
        "pdf_checks",
        'expected_metadata = [["Title", "a"], ["Title", "b"]]',
        "expected_metadata lists 'Title' more than once",
    ),
    (
        "pdf_checks",
        'forbidden_annotation_types = ["/Link"]',
        "forbidden_annotation_types entry '/Link' must be a PDF name without a leading slash",
    ),
    (
        "pdf_checks",
        "printable_margins_pt = [1, 2, 3]",
        "pdf_checks.printable_margins_pt requires 4 list items",
    ),
    (
        "pdf_checks",
        "printable_margins_pt = [1, 2, 3, -4]",
        "printable_margins_pt[3] must be a nonnegative number",
    ),
    (
        "pdf_checks",
        "sparse_page_exemptions = [0]",
        "sparse_page_exemptions[0] must be a positive integer",
    ),
    (
        "pdf_checks",
        '[[pdf_checks.section_budgets]]\nname = "a"\nstart_page = 5\nend_page = 2\nmax_pages = 3',
        "Invalid pdf_checks.section_budgets[0] settings: end_page must not be before start_page",
    ),
    (
        "pdf_checks",
        '[[pdf_checks.text_size_regions]]\nname = "a"\npage = 0\nleft_pt = 1\ntop_pt = 1\n'
        "right_pt = 5\nbottom_pt = 5\nmin_text_size_pt = 6",
        "pdf_checks.text_size_regions[0].page must be a positive integer",
    ),
    (
        "pdf_checks",
        '[[pdf_checks.printable_regions]]\nname = "a"\npage = 1\nleft_pt = 10\ntop_pt = 1\n'
        "right_pt = 5\nbottom_pt = 5",
        "Invalid pdf_checks.printable_regions[0] settings: A region needs right_pt greater than "
        "left_pt and bottom_pt greater than top_pt",
    ),
    (
        "rendered_pdf_artwork",
        "raster_dpi = 500",
        "rendered_pdf_artwork.raster_dpi must be an integer from 18 to 144",
    ),
    (
        "included_figure_artwork",
        "background_gray = 300",
        "included_figure_artwork.background_gray must be an integer from 0 to 255",
    ),
    (
        "rendered_pdf_artwork",
        "max_figure_whitespace_ratio = 2",
        "rendered_pdf_artwork.max_figure_whitespace_ratio must be a number from 0 to 1",
    ),
    (
        "rendered_pdf_artwork",
        "detect_printer_marks = true",
        "Invalid rendered_pdf_artwork settings: Printer marks need trim_regions and "
        "printer_mark_max_length_pt",
    ),
    (
        "rendered_pdf_artwork",
        'required_color_space = "gray"',
        "rendered_pdf_artwork.required_color_space must be one of rgb, cmyk",
    ),
    (
        "rendered_pdf_artwork",
        'category_min_dpi = [["photo", 0]]',
        "category_min_dpi[0][1] must be a positive number",
    ),
    (
        "rendered_pdf_artwork",
        'artwork_category = "line"\ncategory_min_dpi = [["photo", 300]]',
        "artwork_category must name a category listed in category_min_dpi",
    ),
    (
        "structure_checks",
        "author_command = 'x y'",
        "author_command must be a command name without the backslash",
    ),
    (
        "structure_checks",
        'required_author_fields = ["author"]',
        "required_author_fields must not list the author_command itself",
    ),
    (
        "structure_checks",
        '[[structure_checks.heading_expectations]]\npage = 0\ntitle = "A"\nnumber = "1"',
        "structure_checks.heading_expectations[0].page must be an integer from 1 to 5000",
    ),
    (
        "build_checks",
        "expected_texlive_year = 1900",
        "build_checks.expected_texlive_year must be an integer from 1996 to 2100",
    ),
    (
        "build_checks",
        "overfull_tolerance_pt = -1",
        "build_checks.overfull_tolerance_pt must be a nonnegative number",
    ),
    (
        "build_checks",
        'forbidden_loaded_packages = ["amsmath"]',
        "forbidden_loaded_packages entry 'amsmath' must be a literal .sty or .cls filename",
    ),
    (
        "build_checks",
        'minimum_package_dates = [["x.sty", "soon"]]',
        "minimum_package_dates[0][1] must be a date written YYYY-MM-DD or YYYY/MM/DD",
    ),
    (
        "build_checks",
        'required_loaded_packages = ["a.sty"]\nforbidden_loaded_packages = ["a.sty"]',
        "A loaded package cannot be both required and forbidden",
    ),
    (
        "online_checks",
        "online_timeout_seconds = 0",
        "online_checks.online_timeout_seconds must be an integer from 1 to 60",
    ),
    (
        "online_checks",
        "online_max_response_bytes = 10",
        "online_checks.online_max_response_bytes must be an integer from 1024 to 4194304",
    ),
    (
        "online_checks",
        "online_provider_interval_seconds = 100",
        "online_checks.online_provider_interval_seconds must be a number from 0.1 to 60",
    ),
    (
        "online_checks",
        'online_contact_email = "nobody"',
        "online_contact_email must be one simple email address",
    ),
    (
        "submission_checks",
        "filename_max_length = 0",
        "submission_checks.filename_max_length must be a positive integer",
    ),
    (
        "submission_checks",
        'identity_term_matching = "fuzzy"',
        "submission_checks.identity_term_matching must be one of word, substring",
    ),
    (
        "submission_checks",
        'allowed_extensions = ["tex"]',
        "allowed_extensions entry 'tex' must be a dot-prefixed final extension such as .tex",
    ),
    (
        "submission_checks",
        'required_deliverables = [["main.pdf", "movie"]]',
        "required_deliverables[0][1] must be one of file, pdf, zip, text",
    ),
    (
        "submission_checks",
        'required_deliverables = [["../main.pdf", "pdf"]]',
        "required_deliverables[0][0] must be a safe path relative to the project, without '..'",
    ),
    (
        "submission_checks",
        'biblatex_bbl_format_version = "x"',
        'biblatex_bbl_format_version must be a version such as "3.3"',
    ),
    (
        "bibliography_checks",
        "fuzzy_duplicate_threshold = 0",
        "bibliography_checks.fuzzy_duplicate_threshold must be a number greater than 0 "
        "and at most 1",
    ),
    (
        "bibliography_checks",
        'doi_entry_types = ["Article"]',
        "doi_entry_types entry 'Article' must be a lowercase entry type or '*'",
    ),
    (
        "bibliography_checks",
        'required_fields = [["article", ["Title"]]]',
        "required_fields[0][1] entry 'Title' must be a lowercase field name or alternatives",
    ),
    (
        "bibliography_transform",
        'remove_fields = ["Title"]',
        "remove_fields entry 'Title' must be a lowercase field name",
    ),
    (
        "bibliography_transform",
        'remove_fields = ["crossref"]',
        "remove_fields cannot remove relationship fields (crossref)",
    ),
    (
        "bibliography_transform",
        'key_renames = [["a b", "c"]]',
        "key_renames[0][0] must be a literal BibTeX key",
    ),
    (
        "formatting_options",
        "blank_lines = 'x'",
        "formatting_options.blank_lines must be one of formatter, preserve, collapse",
    ),
    (
        "formatting_options",
        "exclude = ['/abs']",
        "exclude entries must be glob patterns relative to the project",
    ),
    (
        "source_transforms",
        "comment_policy = 'x'",
        "source_transforms.comment_policy must be one of retain, private, all",
    ),
    (
        "source_transforms",
        "inline_bibliography = 'a.txt'",
        "inline_bibliography must be a project-relative .bbl path",
    ),
    ("workflow", "baseline_runs = 9", "workflow.baseline_runs must be an integer from 1 to 5"),
    (
        "workflow",
        "comparison = { channel_tolerance = 300 }",
        "workflow.comparison.channel_tolerance must be an integer from 0 to 255",
    ),
    (
        "workflow",
        "comparison = { max_changed_pixel_ratio = 2 }",
        "workflow.comparison.max_changed_pixel_ratio must be a number from 0 to 1",
    ),
    (
        "metadata_privacy",
        "image_identity_terms = ['a', 'a']",
        "image_identity_terms lists 'a' more than once",
    ),
    (
        "reporting",
        "suppressions = [{ code = 'TEX001', reason = ' ' }]",
        "reason must be a nonempty explanation",
    ),
    (
        "reporting",
        "suppressions = [{ code = 'NOPE', reason = 'x' }]",
        "code must be an exact registered check code",
    ),
    ("package", "include = ['a', 'a']", "include lists 'a' more than once"),
)


def parse(text: str):
    return parse_settings_text(text + "\n", Path("fledge.toml"))


class SettingsErrorMessageTests(unittest.TestCase):
    def assert_error(self, text: str, names: tuple[str, ...], message: str, *, exact: bool) -> None:
        with self.assertRaises(SettingsError) as raised:
            parse(text)
        if exact:
            self.assertEqual(str(raised.exception), message)
        else:
            self.assertIn(message, str(raised.exception))
        self.assertEqual(raised.exception.settings, names)
        self.assertLessEqual(set(raised.exception.settings), SETTING_NAMES)

    def test_top_level_settings_name_the_key_and_the_allowed_values(self) -> None:
        for text, names, message in TOP_LEVEL_CASES:
            with self.subTest(text=text):
                self.assert_error(text, names, message, exact=True)

    def test_manuscript_and_pdf_constraints_name_their_own_key(self) -> None:
        for text, names, message in FLATTENED_CASES:
            with self.subTest(text=text):
                self.assert_error(text, names, message, exact=False)

    def test_values_in_a_table_name_the_table_and_the_key(self) -> None:
        for table, body, message in TABLE_CASES:
            text = body if body.startswith("[[") else f"[{table}]\n{body}"
            with self.subTest(table=table, body=body):
                self.assert_error(text, (table,), message, exact=False)

    def test_a_misspelled_key_in_a_table_suggests_the_closest_valid_one(self) -> None:
        with self.assertRaises(SettingsError) as raised:
            parse("[pdf_checks]\nraster_dp = 3")
        message = str(raised.exception)
        self.assertIn(
            "Unknown pdf_checks setting(s): raster_dp (did you mean raster_dpi?).", message
        )
        self.assertIn("Valid pdf_checks settings: ", message)
        self.assertIn("raster_dpi", message.split("Valid pdf_checks settings: ")[1])
        self.assertEqual(raised.exception.settings, ("pdf_checks",))

    def test_an_unknown_key_without_a_close_match_lists_the_valid_keys(self) -> None:
        with self.assertRaises(SettingsError) as raised:
            parse("[online_checks]\nfrobnicate = true")
        message = str(raised.exception)
        self.assertTrue(message.startswith("Unknown online_checks setting(s): frobnicate. Valid "))
        self.assertNotIn("did you mean", message)
        self.assertIn("online_timeout_seconds", message)

    def test_a_valid_file_still_loads_with_every_group_set(self) -> None:
        settings = parse(
            "jobs = 3\nabstract_min_words = 0\nmin_image_dpi = 300\n"
            "[pdf_checks]\nraster_dpi = 144\n[rendered_pdf_artwork]\nraster_dpi = 72\n"
        )
        self.assertEqual(settings.pdf_checks.raster_dpi, 144)
        self.assertEqual(settings.rendered_pdf_artwork.raster_dpi, 72)
        self.assertEqual(settings.pdf.min_image_dpi, 300)


class RasterSamplingDefaultsTests(unittest.TestCase):
    def test_the_two_tables_share_limits_but_keep_their_own_default_resolution(self) -> None:
        settings = parse("")
        self.assertEqual(settings.pdf_checks.raster_dpi, 36)
        self.assertEqual(settings.rendered_pdf_artwork.raster_dpi, 72)
        self.assertEqual(settings.included_figure_artwork.raster_dpi, 72)
        for table in ("pdf_checks", "rendered_pdf_artwork", "included_figure_artwork"):
            with self.subTest(table=table):
                self.assertEqual(getattr(settings, table).background_gray, 255)
                self.assertEqual(getattr(settings, table).ink_difference, 5)
                for key, value in (
                    ("raster_dpi", 17),
                    ("background_gray", -1),
                    ("ink_difference", 256),
                ):
                    with self.assertRaises(SettingsError):
                        parse(f"[{table}]\n{key} = {value}")


if __name__ == "__main__":
    unittest.main()
