from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jlc2cadence import capture_bridge as bridge, step_preview as preview


class StepPreviewLimitCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.step = self.root / "supplier.step"
        self.step.write_bytes(b"ISO-10303-21;\noriginal supplier STEP\nEND-ISO-10303-21;")
        self.limit = 64

    @staticmethod
    def export_too_large(_source, output):
        output.write_bytes(b"oversized GLB" * 128)

    def generate_too_large(self):
        with self.assertRaises(preview.StepPreviewError) as caught:
            preview.generate_step_glb(self.step, self.root, self.limit)
        self.assertEqual(caught.exception.error_code, "step_preview_too_large")

    def test_same_source_renderer_and_limit_reuse_confirmed_too_large_result(self):
        original = self.step.read_bytes()
        with patch.object(preview, "_export_step_glb", side_effect=self.export_too_large) as export:
            self.generate_too_large()
            self.generate_too_large()
        self.assertEqual(export.call_count, 1)
        self.assertEqual(self.step.read_bytes(), original)

    def test_source_change_invalidates_too_large_result(self):
        with patch.object(preview, "_export_step_glb", side_effect=self.export_too_large) as export:
            self.generate_too_large()
            self.step.write_bytes(self.step.read_bytes().replace(b"original", b"modified"))
            self.generate_too_large()
        self.assertEqual(export.call_count, 2)

    def test_changed_converter_renderer_or_limit_retries_conversion(self):
        for setting, value in (("__version__", "future"), ("PREVIEW_RENDERER_REVISION", 999),
                               ("distribution_version", lambda _package: "future-ocp"), ("DEFAULT_MAX_BYTES", 128)):
            with self.subTest(setting=setting):
                self.limit = 64
                with patch.object(preview, "_export_step_glb", side_effect=self.export_too_large) as export:
                    # Start with a fresh result for this key even if another
                    # subcase left a status for the same source file.
                    status = self.step.with_name(f".{self.step.name}.preview-status.json")
                    status.unlink(missing_ok=True)
                    self.generate_too_large()
                    if setting == "DEFAULT_MAX_BYTES":
                        self.limit = value
                        self.generate_too_large()
                    else:
                        with patch.object(preview, setting, value, create=True):
                            self.generate_too_large()
                    self.assertEqual(export.call_count, 2)

    def test_transient_conversion_errors_are_not_cached(self):
        with patch.object(preview, "_export_step_glb", side_effect=OSError("temporary renderer failure")) as export:
            for _ in range(2):
                with self.assertRaises(preview.StepPreviewError) as caught:
                    preview.generate_step_glb(self.step, self.root, self.limit)
                self.assertEqual(caught.exception.error_code, "step_preview_failed")
        self.assertEqual(export.call_count, 2)
        self.assertFalse(self.step.with_name(f".{self.step.name}.preview-status.json").exists())

    def test_source_changed_during_export_does_not_get_a_size_cache_record(self):
        def export(_source, output):
            self.export_too_large(_source, output)
            self.step.write_bytes(b"ISO-10303-21;\nnew supplier geometry")

        with patch.object(preview, "_export_step_glb", side_effect=export):
            self.generate_too_large()
        self.assertFalse(self.step.with_name(f".{self.step.name}.preview-status.json").exists())

    def test_network_and_temporary_fallback_errors_remain_retryable(self):
        with patch.object(bridge, "urlopen", side_effect=OSError("temporary offline")) as network, patch.object(
            bridge, "_generate_local_step_preview", side_effect=ValueError("temporary local failure")
        ) as local:
            for _ in range(2):
                result = {"step_files": [str(self.step)], "warnings": []}
                bridge._add_compact_step_preview(result, self.root, "http://localhost:4178/")
        self.assertEqual(network.call_count, 2)
        self.assertEqual(local.call_count, 2)
        self.assertFalse(self.step.with_name(f".{self.step.name}.preview-status.json").exists())

    def test_local_bridge_skips_worker_for_known_oversized_source(self):
        with patch.object(preview, "_export_step_glb", side_effect=self.export_too_large):
            self.generate_too_large()
        result = {"step_files": [str(self.step)], "warnings": []}
        with patch.object(bridge, "MAX_PREVIEW_GLB_BYTES", self.limit), patch.object(
            bridge.subprocess, "run", side_effect=ValueError("worker should not run")
        ) as worker:
            bridge._add_compact_step_preview(result, self.root, "")
        worker.assert_not_called()
        self.assertEqual(result["preview_3d"], {})
        self.assertIn("exceeds", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
