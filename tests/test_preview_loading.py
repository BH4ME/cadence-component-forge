from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_capture_bridge import bridge, sample_library
import test_capture_bridge as fixtures


class DeferredPreviewTests(unittest.TestCase):
    def test_step_cache_checks_original_bytes_and_refreshes_viewer_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            def download(_library, output, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                step = output / "source.step"
                step.write_bytes(b"unchanged supplier STEP")
                return fixtures.EasyEDA3DFetchReport(str(output), ["step"], models=[
                    fixtures.Model3D(file=str(step), key="C25804_R0603", format="step")])
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", side_effect=download
            ) as fetch, patch.object(bridge, "_add_compact_step_preview"):
                first = bridge.import_part("C25804", Path(directory), prepare_only=True, viewer_base_url="http://localhost:1234/")
                cached = bridge.import_part("C25804", Path(directory), prepare_only=True, viewer_base_url="http://localhost:2345/")
                self.assertTrue(cached["cached"])
                self.assertIn("localhost:2345", cached["step_viewer_url"])
                self.assertEqual(fetch.call_count, 1)
                Path(first["step_files"][0]).write_bytes(b"modified file")
                repaired = bridge.import_part("C25804", Path(directory), prepare_only=True)
                self.assertFalse(repaired["cached"])
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(Path(repaired["step_files"][0]).read_bytes(), b"unchanged supplier STEP")

    def test_path_registration_unavailable_is_reported_without_claiming_success(self):
        from jlc2cadence import library_paths
        for status in ("environment_unavailable", "missing_assets"):
            with self.subTest(status=status), patch.object(library_paths, "register_allegro_paths", return_value={"status": status}):
                result = {"ok": True, "warnings": []}
                bridge._register_library_paths(result, Path("library"), None)
                self.assertEqual(result["path_registration"]["status"], status)
                self.assertTrue(result["warnings"])

    def test_2d_preview_does_not_download_or_render_step_and_reuses_generated_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models"
            ) as fetch, patch.object(bridge, "_add_compact_step_preview") as render:
                first = bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                with patch.object(bridge, "write_capture_outputs") as convert:
                    second = bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                    convert.assert_not_called()
            fetch.assert_not_called()
            render.assert_not_called()
            self.assertEqual(first["step_status"], "deferred")
            self.assertEqual(first["step_files"], [])
            self.assertTrue(first["preview_data"]["symbol"]["pins"])
            self.assertTrue(first["preview_data"]["footprint"]["pads"])
            self.assertTrue(second["cached"])
            self.assertEqual(first["preview_data"], second["preview_data"])

    def test_changed_source_and_deleted_output_invalidate_preview_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            with patch.object(bridge, "_catalog", return_value=catalog):
                first = bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                Path(first["capture_xml"]).unlink()
                rebuilt = bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                self.assertFalse(rebuilt["cached"])
                self.assertTrue(Path(rebuilt["capture_xml"]).is_file())
                changed = sample_library()
                changed.footprints[0].pads[0].x -= 0.2
                catalog.fetch_part.side_effect = None
                catalog.fetch_part.return_value = changed
                revised = bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                self.assertNotEqual(first["build_dir"], revised["build_dir"])
                self.assertNotEqual(first["preview_data"]["footprint"]["pads"], revised["preview_data"]["footprint"]["pads"])

    def test_deferred_preview_is_not_reused_for_step_or_native_import(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            with patch.object(bridge, "_catalog", return_value=catalog):
                bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
                with patch.object(bridge, "fetch_easyeda_3d_models", side_effect=RuntimeError("real STEP fetch requested")) as download:
                    for prepare_only in (False, True):
                        with self.assertRaisesRegex(RuntimeError, "real STEP"):
                            bridge.import_part("C25804", Path(directory), prepare_only=prepare_only)
                    self.assertEqual(download.call_count, 2)
            with self.assertRaisesRegex(ValueError, "only supported for previews"):
                bridge.import_part("C25804", Path(directory), defer_step=True)

    def test_low_disk_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge.shutil, "disk_usage", return_value=Mock(free=1024)), patch.object(bridge, "_catalog") as catalog:
            with self.assertRaisesRegex(OSError, "磁盘空间不足"):
                bridge.import_part("C25804", Path(directory), prepare_only=True, defer_step=True)
            catalog.assert_not_called()

    def test_cli_routes_deferred_preview(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "import_part", return_value={"ok": True}) as run:
            self.assertEqual(bridge.main(["import", "--part", "C25804", "--library-root", directory,
                "--output", str(Path(directory) / "response.json"), "--prepare-only", "--defer-step"]), 0)
            self.assertTrue(run.call_args.kwargs["defer_step"])


class OfflineRendererTests(unittest.TestCase):
    def test_offline_viewer_uses_local_renderer_preserving_original_step(self):
        helper = fixtures.CompactStepPreviewTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, sidecar = helper.setup_step(root)
            original = step.read_bytes()
            result = helper.result(step)
            def render(source, library_root):
                self.assertEqual((source, library_root), (step, root))
                sidecar.write_bytes(helper.glb())
            with patch.object(bridge, "urlopen", side_effect=OSError("offline")), patch.object(
                bridge, "_generate_local_step_preview", side_effect=render
            ) as local:
                bridge._add_compact_step_preview(result, root, "http://localhost:4178/")
            local.assert_called_once()
            self.assertEqual(result["preview_3d"]["source_step"], str(step))
            self.assertEqual(result["preview_renderer"], "local_fallback")
            self.assertEqual(result["warnings"], [])
            self.assertEqual(step.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
