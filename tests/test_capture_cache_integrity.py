from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from test_capture_bridge import bridge, sample_library, CadenceTools, EasyEDA3DFetchReport, Model3D
from test_easyeda_catalog import component_to_library, resistor_data


class CaptureCacheIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = self.directory / "library"
        self.catalog = Mock()
        self.catalog.fetch_part.side_effect = lambda _: sample_library()
        self.stack.enter_context(patch.object(bridge, "_catalog", return_value=self.catalog))
        self.fetch = self.stack.enter_context(patch.object(bridge, "fetch_easyeda_3d_models", side_effect=self.download))
        tools = CadenceTools(allegro=Path("allegro.exe"), allegro_batch=Path("allegro_batch.exe"))
        self.stack.enter_context(patch.object(bridge.CadenceTools, "discover", return_value=tools))
        self.native = self.stack.enter_context(patch.object(bridge, "run_symbol_build_plan", side_effect=self.build))
        self.stack.enter_context(patch.object(bridge, "run_step_mapping", return_value={"status": "mapped"}))
        self.stack.enter_context(patch.object(bridge, "compile_existing_package_symbols", return_value=[{"status": "created_psm"}]))
        self.stack.enter_context(patch.object(bridge, "_add_compact_step_preview"))

    @staticmethod
    def download(_library, output, **_kwargs):
        output.mkdir(parents=True, exist_ok=True)
        step = output / "original.step"
        step.write_bytes(b"original supplier step")
        return EasyEDA3DFetchReport(str(output), ["step"], models=[
            Model3D(file=str(step), key="C25804_R0603", format="step")])

    @staticmethod
    def build(_tools, symbols):
        (symbols / "C25804_R0603.dra").write_bytes(b"original dra")
        (symbols / "c25804_r0603.psm").write_bytes(b"original psm")
        (symbols.parent / "padstacks" / "original.pad").write_bytes(b"original pad")
        return [{"status": "created_psm"}]

    def run_import(self, **kwargs):
        return bridge.import_part("C25804", self.root, **kwargs)

    def manifest(self, result):
        return Path(result["build_dir"]) / ("preview_browser.json" if result["prepared_only"] else "capture_browser.json")

    def assert_preserved(self, changed):
        backups = list((self.root / ".modified_assets").rglob("*.bak"))
        self.assertTrue(any(path.read_bytes() == changed for path in backups), backups)

    def test_malformed_full_manifest_rebuilds_instead_of_permanent_failure(self):
        first = self.run_import()
        self.manifest(first).write_text("{broken json", encoding="utf-8")
        rebuilt = self.run_import()
        self.assertTrue(rebuilt["ok"])
        self.assertFalse(rebuilt["cached"])
        self.assertEqual(self.native.call_count, 2)
        self.assertTrue(rebuilt["warnings"])

    def test_modified_nonempty_published_assets_rebuild_and_preserve_user_bytes(self):
        for suffix in (".step", ".dra", ".psm", ".pad"):
            with self.subTest(suffix=suffix):
                first = self.run_import()
                asset = next(Path(path) for path in first["published_files"] if Path(path).suffix == suffix)
                original = asset.read_bytes()
                changed = b"modified but nonempty " + suffix.encode()
                asset.write_bytes(changed)
                rebuilt = self.run_import()
                self.assertFalse(rebuilt["cached"])
                self.assertEqual(asset.read_bytes(), original)
                self.assert_preserved(changed)

    def test_modified_capture_and_ir_rebuild_for_imports_and_previews(self):
        for prepare_only in (False, True):
            for relative in ("capture/library.olb.xml", "reports/library.ir.json"):
                with self.subTest(prepare_only=prepare_only, relative=relative):
                    options = {"prepare_only": prepare_only, "defer_step": prepare_only}
                    first = self.run_import(**options)
                    asset = Path(first["build_dir"]) / relative
                    original = asset.read_bytes()
                    changed = original.replace(b'"x": -0.8', b'"x": -9.8') if relative.endswith("json") else original.replace(b'hotptY="30"', b'hotptY="77"')
                    self.assertNotEqual(changed, original)
                    asset.write_bytes(changed)
                    rebuilt = self.run_import(**options)
                    self.assertFalse(rebuilt["cached"])
                    self.assertEqual(asset.read_bytes(), original)
                    self.assertEqual(rebuilt["preview_data"], first["preview_data"])
                    self.assert_preserved(changed)

    def test_manifest_paths_outside_library_are_never_read_or_backed_up(self):
        first = self.run_import()
        external = self.directory / "private.step"
        external.write_bytes(b"unrelated external bytes")
        result = json.loads(self.manifest(first).read_text(encoding="utf-8"))
        result["published_files"].append(str(external))
        result["generated_file_sha256"] = {str(external): "0" * 64}
        self.manifest(first).write_text(json.dumps(result), encoding="utf-8")
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), external.resolve(), "Untrusted manifest caused an external file read")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=open_file):
            rebuilt = self.run_import()
        self.assertFalse(rebuilt["cached"])
        self.assertEqual(external.read_bytes(), b"unrelated external bytes")
        self.assertNotIn(str(external), rebuilt["published_files"])

    def test_source_exact_preview_restores_original_pin_coordinate_after_xml_edit(self):
        def source_library(_part):
            raw = resistor_data()
            # The catalog parser fixture omits pin-label font sizes. Supply
            # explicit original sizes supported by Capture's integer grid.
            raw["dataStr"]["shape"] = [record.replace("~end~~0", "~end~Verdana~7").replace("~start~~0", "~start~Verdana~7")
                                        for record in raw["dataStr"]["shape"]]
            return component_to_library(raw, "C25804")

        self.catalog.fetch_part.side_effect = source_library
        first = self.run_import(prepare_only=True, defer_step=True)
        asset = Path(first["capture_xml"])
        original = asset.read_bytes()
        xml = ElementTree.parse(asset)
        xml.find("./Package/LibPart/NormalView/SymbolPinScalar/Defn").set("hotptY", "77")
        xml.write(asset, encoding="utf-8")
        changed = asset.read_bytes()
        rebuilt = self.run_import(prepare_only=True, defer_step=True)
        self.assertEqual(rebuilt["conversion_policy"], "source_exact")
        self.assertFalse(rebuilt["cached"])
        self.assertEqual(asset.read_bytes(), original)
        self.assertEqual(rebuilt["preview_data"], first["preview_data"])
        self.assert_preserved(changed)

    def test_legacy_manifest_without_integrity_proofs_is_rebuilt(self):
        first = self.run_import()
        result = json.loads(self.manifest(first).read_text(encoding="utf-8"))
        result.pop("generated_file_sha256", None)
        self.manifest(first).write_text(json.dumps(result), encoding="utf-8")
        self.assertFalse(self.run_import()["cached"])


if __name__ == "__main__":
    unittest.main()
