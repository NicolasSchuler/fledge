from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.config import PARENT_MEMORY_MB, load_settings
from latexprep.models import PreparationError


class ParallelConfigTests(unittest.TestCase):
    def test_parallel_defaults_preserve_small_fan_out_and_explicit_job_limits(self):
        settings = load_settings(None, {})
        self.assertEqual(settings.jobs, 2)
        self.assertEqual(settings.build_jobs, 1)
        self.assertEqual(settings.render_jobs, 1)
        self.assertEqual(settings.job_timeout_seconds, 600)
        self.assertEqual(settings.max_temporary_bytes, 2_147_483_648)
        self.assertIsNone(settings.source_date_epoch)
        self.assertGreaterEqual(settings.memory_mb, settings.build_memory_mb + PARENT_MEMORY_MB)

    def test_parallel_file_settings_and_explicit_overrides_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(
                "jobs = 4\nbuild_jobs = 2\nrender_jobs = 3\n"
                "memory_mb = 1024\nbuild_memory_mb = 512\n"
                "job_timeout_seconds = 900\nmax_temporary_bytes = 123456789\n"
                "source_date_epoch = 1700000000\n"
            )
            settings = load_settings(
                path,
                {
                    "render_jobs": 1,
                    "job_timeout_seconds": 240,
                    "max_temporary_bytes": None,
                    "source_date_epoch": 0,
                },
            )
        serialized = settings.to_dict()
        for name, expected in {
            "jobs": 4,
            "build_jobs": 2,
            "render_jobs": 1,
            "memory_mb": 1024,
            "build_memory_mb": 512,
            "job_timeout_seconds": 240,
            "max_temporary_bytes": 123456789,
            "source_date_epoch": 0,
        }.items():
            with self.subTest(setting=name):
                self.assertEqual(serialized[name], expected)

    def test_parallel_capacity_and_limit_fields_require_positive_integer_values(self):
        for name in ("build_jobs", "render_jobs", "job_timeout_seconds", "max_temporary_bytes"):
            for value in (0, -1, True, False, "2", 1.5):
                with (
                    self.subTest(setting=name, value=value),
                    self.assertRaisesRegex(PreparationError, name),
                ):
                    load_settings(None, {name: value})
            with self.subTest(setting=name, value=1):
                self.assertEqual(getattr(load_settings(None, {name: 1}), name), 1)

    def test_build_and_render_capacity_upper_bounds_are_validated(self):
        for name in ("build_jobs", "render_jobs"):
            with self.subTest(setting=name, value=64):
                self.assertEqual(getattr(load_settings(None, {name: 64}), name), 64)
            with (
                self.subTest(setting=name, value=65),
                self.assertRaisesRegex(PreparationError, "at most 64"),
            ):
                load_settings(None, {name: 65})

    def test_build_reservation_leaves_room_for_coordinator_at_exact_boundary(self):
        for build_memory in (128, 1024):
            total = build_memory + PARENT_MEMORY_MB
            with self.subTest(build_memory=build_memory):
                settings = load_settings(
                    None, {"memory_mb": total, "build_memory_mb": build_memory}
                )
                self.assertEqual(settings.memory_mb - settings.build_memory_mb, PARENT_MEMORY_MB)
                with self.assertRaises(PreparationError):
                    load_settings(None, {"memory_mb": total - 1, "build_memory_mb": build_memory})
        with self.assertRaisesRegex(PreparationError, "coordinator"):
            load_settings(None, {"memory_mb": 1024, "build_memory_mb": 1024})
        with self.assertRaisesRegex(PreparationError, "build_memory_mb"):
            load_settings(None, {"memory_mb": 256, "build_memory_mb": 127})

    def test_source_date_epoch_accepts_supported_boundaries_and_rejects_invalid_types(self):
        for value in (None, 0, 253402300799):
            with self.subTest(value=value):
                self.assertEqual(
                    load_settings(None, {"source_date_epoch": value}).source_date_epoch, value
                )
        for value in (-1, 253402300800, True, False, 1.5, "1700000000"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(PreparationError, "source_date_epoch"),
            ):
                load_settings(None, {"source_date_epoch": value})


if __name__ == "__main__":
    unittest.main()
