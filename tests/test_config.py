from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.config import load_settings
from latexprep.models import PreparationError


class ConfigTests(unittest.TestCase):
    def test_explicit_file_then_command_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('layout = "flat"\njobs = 4\nformat = true\n')
            settings = load_settings(path, {"jobs": 1, "format": False, "main": None})
            self.assertEqual(settings.jobs, 1)
            self.assertEqual(settings.layout, "flat")
            self.assertFalse(settings.format)

    def test_unknown_type_and_contradictory_settings_rejected(self):
        for overrides in [
            {"publisher": "example"},
            {"jobs": True},
            {"jobs": 0},
            {"jobs": 65},
            {"layout": "publisher"},
            {"format": "true"},
            {"engine": "custom.sh"},
            {"memory_mb": 512, "build_memory_mb": 1024},
            {"max_pages": -1},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)

    def test_generic_constraints_load_into_typed_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(
                'expected_document_class = "article"\n'
                'required_class_options = ["review"]\n'
                'forbidden_packages = ["draftwatermark"]\n'
                'required_sections = ["Data Availability"]\n'
                "require_abstract = true\n"
                "abstract_min_words = 100\nabstract_max_words = 250\n"
                "keywords_min_count = 3\nkeywords_max_count = 6\n"
                "expected_page_width_pt = 612\nexpected_page_height_pt = 792.0\n"
                "require_embedded_fonts = true\nmin_image_dpi = 300\n"
            )
            settings = load_settings(path, {"require_abstract": False, "abstract_min_words": 0})
            manuscript = settings.manuscript_options()
            self.assertEqual(manuscript.expected_document_class, "article")
            self.assertEqual(manuscript.required_class_options, ("review",))
            self.assertEqual(manuscript.required_sections, ("Data Availability",))
            self.assertFalse(manuscript.require_abstract)
            self.assertEqual(manuscript.abstract_min_words, 0)
            pdf = settings.pdf_options()
            self.assertEqual((pdf.expected_page_width_pt, pdf.expected_page_height_pt), (612, 792))
            self.assertTrue(pdf.require_embedded_fonts)
            self.assertEqual(pdf.min_image_dpi, 300)

    def test_invalid_constraint_types_and_combinations_are_rejected(self):
        for overrides in (
            {"expected_document_class": ""},
            {"required_class_options": "review"},
            {"required_class_options": ["review", "review"]},
            {"required_sections": [" "]},
            {"forbidden_packages": [False]},
            {"required_class_options": ["review"], "forbidden_class_options": ["review"]},
            {"abstract_min_words": 20, "abstract_max_words": 10},
            {"abstract_min_words": True},
            {"keywords_max_count": -1},
            {"require_abstract": 1},
            {"expected_page_width_pt": 612},
            {"page_size_tolerance_pt": -0.1},
            {"min_image_dpi": float("nan")},
            {"min_image_dpi": float("inf")},
            {"min_image_dpi": True},
            {"forbid_forms": "false"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)


if __name__ == "__main__":
    unittest.main()
