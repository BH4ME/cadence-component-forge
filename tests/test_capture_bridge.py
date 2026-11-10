from __future__ import annotations

import base64
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence import capture_bridge as bridge
from jlc2cadence.extractors.easyeda_3d import EasyEDA3DFetchReport
from jlc2cadence.extractors.easyeda_catalog import CatalogError, CatalogPart
from jlc2cadence.ir.schema import Component, Footprint, Graphic, LibraryIR, Model3D, Pad, Pin
from jlc2cadence.validators.cadence_runner import CadenceTools


def sample_library() -> LibraryIR:
    return LibraryIR(
        components=[Component(name="R", ref_prefix="R", footprint="R0603", pins=[
            Pin(number="1", name="1"), Pin(number="2", name="2"),
        ])],
        footprints=[Footprint(name="R0603", pads=[
            Pad(number="1", x=-0.8, y=0, size=(0.8, 0.9)),
            Pad(number="2", x=0.8, y=0, size=(0.8, 0.9)),
        ])],
    )


class PublishFileTests(unittest.TestCase):
    def test_transient_replace_permission_error_retries_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pad"
            target = Path(directory) / "published" / "part.pad"
            source.write_bytes(b"new padstack\x00\xff")
            target.parent.mkdir()
            target.write_bytes(b"old padstack")
            original_replace = Path.replace
            attempts = []

            def replace(path, destination):
                attempts.append(path)
                if len(attempts) < 3:
                    self.assertEqual(target.read_bytes(), b"old padstack")
                    raise PermissionError("temporary Windows sharing violation")
                return original_replace(path, destination)

            with patch.object(Path, "replace", new=replace), patch("time.sleep") as sleep:
                bridge._publish_file(source, target)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_permanent_replace_failure_is_bounded_preserves_target_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pad"
            target = Path(directory) / "published" / "part.pad"
            source.write_bytes(b"replacement")
            target.parent.mkdir()
            target.write_bytes(b"original")
            failure = PermissionError("permanent sharing violation")
            with patch.object(Path, "replace", side_effect=failure) as replace, patch("time.sleep") as sleep:
                with self.assertRaises(PermissionError) as raised:
                    bridge._publish_file(source, target)
            self.assertIs(raised.exception, failure)
            self.assertGreater(replace.call_count, 1)
            self.assertLessEqual(replace.call_count, 10)
            self.assertEqual(sleep.call_count, replace.call_count - 1)
            self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 3)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_other_replace_errors_are_not_retried_and_temp_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pad"
            target = Path(directory) / "published" / "part.pad"
            source.write_bytes(b"replacement")
            failure = OSError("disk failure")
            with patch.object(Path, "replace", side_effect=failure) as replace, patch("time.sleep") as sleep:
                with self.assertRaises(OSError) as raised:
                    bridge._publish_file(source, target)
            self.assertIs(raised.exception, failure)
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_same_process_nested_publish_uses_independent_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pad"
            second = Path(directory) / "second.pad"
            target = Path(directory) / "published" / "part.pad"
            first.write_bytes(b"first complete payload")
            second.write_bytes(b"second complete payload")
            original_replace = Path.replace
            attempts = []

            def replace(path, destination):
                attempts.append(path)
                if len(attempts) == 1:
                    bridge._publish_file(second, target)
                    self.assertEqual(target.read_bytes(), second.read_bytes())
                return original_replace(path, destination)

            with patch.object(Path, "replace", new=replace):
                bridge._publish_file(first, target)
            self.assertEqual(len(set(attempts)), 2)
            self.assertEqual(target.read_bytes(), first.read_bytes())
            self.assertEqual(list(target.parent.iterdir()), [target])

    @unittest.skipUnless(os.name == "nt", "Requires a real Windows file sharing lock")
    def test_windows_read_lock_release_allows_atomic_publish(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pad"
            target = Path(directory) / "published" / "part.pad"
            source.write_bytes(b"replacement\x00\xff")
            target.parent.mkdir()
            target.write_bytes(b"original")
            # Deliberately omit FILE_SHARE_DELETE, as a native reader can do.
            handle = kernel.CreateFileW(str(target), 0x80000000, 0x1, None, 3, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())

            def release(_delay):
                nonlocal handle
                self.assertEqual(target.read_bytes(), b"original")
                self.assertTrue(kernel.CloseHandle(handle))
                handle = None

            try:
                with patch("time.sleep", side_effect=release) as sleep:
                    bridge._publish_file(source, target)
                sleep.assert_called_once()
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(list(target.parent.iterdir()), [target])
            finally:
                if handle is not None:
                    kernel.CloseHandle(handle)


class CaptureBridgeTests(unittest.TestCase):
    def test_search_forwards_pagination_and_reports_catalog_total_and_stock(self) -> None:
        catalog = Mock()
        catalog.last_total = 5
        catalog.search.return_value = [CatalogPart("C2040", "NE555", stock=120)]
        with patch.object(bridge, "_catalog", return_value=catalog):
            result = bridge.search_parts(" NE555 ", Path("library"), page=2, limit=2)
            last = bridge.search_parts("NE555", Path("library"), page=3, limit=2)
        self.assertEqual(result["query"], "NE555")
        self.assertEqual((result["page"], result["page_size"], result["total"]), (2, 2, 5))
        self.assertTrue(result["has_more"])
        self.assertFalse(last["has_more"])
        self.assertEqual(result["results"][0]["stock"], 120)
        self.assertIsNone(result["results"][0]["has_step"])
        self.assertEqual(catalog.search.call_args_list[0].args, ("NE555",))
        self.assertEqual(catalog.search.call_args_list[0].kwargs, {"limit": 2, "page": 2})

    def test_exact_id_search_has_no_duplicate_second_page(self) -> None:
        catalog = Mock()
        catalog.last_total = 1
        catalog.search.return_value = [CatalogPart("C25804", "R0603")]
        with patch.object(bridge, "_catalog", return_value=catalog):
            first = bridge.search_parts("c25804", Path("library"))
            second = bridge.search_parts("c25804", Path("library"), page=2)
        self.assertEqual(first["results"][0]["lcsc_id"], "C25804")
        self.assertEqual((first["page"], first["page_size"], first["total"]), (1, 30, 1))
        self.assertFalse(first["has_more"])
        self.assertEqual(second["results"], [])
        self.assertEqual(second["total"], 1)
        self.assertFalse(second["has_more"])

    def test_invalid_search_pagination_is_rejected_before_catalog_access(self) -> None:
        with patch.object(bridge, "_catalog") as catalog:
            for page, limit in ((0, 30), (-1, 30), (1, 0), (1, 101), (1.5, 30), (True, 30), (1, "30")):
                with self.subTest(page=page, limit=limit), self.assertRaises(ValueError):
                    bridge.search_parts("NE555", Path("library"), page=page, limit=limit)
            catalog.assert_not_called()

    def test_search_worker_forwards_page_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "search_parts", return_value={"ok": True}) as run:
            code = bridge.main([
                "search", "--query", "NE555", "--library-root", directory,
                "--output", str(Path(directory) / "reply.json"), "--page", "4", "--limit", "20",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs, {"page": 4, "limit": 20})

    def test_namespacing_preserves_pin_to_pad_relationship(self) -> None:
        library = sample_library()
        bridge.namespace_part(library, "C25804")
        self.assertEqual(library.components[0].footprint, library.footprints[0].name)
        self.assertEqual(library.components[0].properties["PCB Footprint"], "C25804_R0603")
        self.assertEqual(library.components[0].pin_numbers(), library.footprints[0].pad_numbers())
        first = bridge._name("C25804_" + "LongPackage" * 6 + "A")
        second = bridge._name("C25804_" + "LongPackage" * 6 + "B")
        self.assertLessEqual(len(first), 31)
        self.assertNotEqual(first, second)

    def test_invalid_part_is_rejected_before_network_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "_catalog") as catalog:
            with self.assertRaises(ValueError):
                bridge.import_part("../../C25804", Path(directory) / "library")
            catalog.assert_not_called()
            self.assertFalse((Path(directory) / "library").exists())

    def test_prepare_only_writes_real_capture_xml_and_reports_missing_step(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capture bridge ") as directory:
            catalog = Mock()
            catalog.fetch_part.return_value = sample_library()
            report = EasyEDA3DFetchReport(output_dir=directory, formats=["step"])
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", return_value=report
            ), patch.object(bridge, "run_symbol_build_plan") as run:
                result = bridge.import_part("c25804", Path(directory), prepare_only=True)
            self.assertTrue(result["ok"])
            self.assertTrue(result["prepared_only"])
            self.assertEqual(result["step_status"], "missing")
            self.assertTrue(Path(result["capture_xml"]).is_file())
            self.assertFalse(Path(result["olb_path"]).exists())
            self.assertEqual(list(Path(directory).rglob("*.step")), [])
            self.assertTrue(result["warnings"])
            self.assertEqual(result["step_files"], [])
            self.assertEqual(result["step_viewer_url"], "")
            self.assertEqual(result["footprint_path"], "")
            run.assert_not_called()

    def test_preview_matches_generated_capture_layout_and_saved_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = sample_library()
            library.components[0].pins[0].x = 700
            library.components[0].pins[0].y = 800
            library.components[0].pins[1].side = "right"
            library.components[0].pins.append(Pin(number="3", name="hidden", hidden=True))
            library.footprints[0].pads.append(Pad(number="3", x=0, y=2, size=(0.8, 0.9)))
            library.footprints[0].pads[0].rotation = 45
            library.footprints[0].graphics.append(Graphic(
                kind="line", layer="silkscreen_top", points=[(-1, -2), (3, 4)], width=0.15,
            ))
            catalog = Mock()
            catalog.fetch_part.return_value = library
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", return_value=EasyEDA3DFetchReport(directory, ["step"])
            ):
                result = bridge.import_part("C25804", Path(directory), prepare_only=True)
            symbol = result["preview_data"]["symbol"]
            self.assertEqual([(pin["number"], pin["x"], pin["y"], pin["end_x"], pin["end_y"])
                              for pin in symbol["pins"]], [("1", -10, -30, 0, -30), ("2", 90, -30, 80, -30)])
            self.assertEqual(symbol["graphics"][0]["points"], [[0, 0], [80, 0], [80, -40], [0, -40], [0, 0]])
            self.assertEqual(symbol["source_path"], result["capture_xml"])
            footprint = result["preview_data"]["footprint"]
            saved = json.loads((Path(result["build_dir"]) / "reports" / "library.ir.json").read_text(encoding="utf-8"))
            self.assertEqual(footprint["pads"], saved["footprints"][0]["pads"])
            self.assertEqual(footprint["graphics"], saved["footprints"][0]["graphics"])

    def test_homogeneous_preview_uses_first_units_physical_pin_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = sample_library()
            library.components[0].pins = [
                Pin(number="1", name="IN", unit="A"), Pin(number="2", name="OUT", side="right", unit="A"),
                Pin(number="3", name="IN", unit="B"), Pin(number="4", name="OUT", side="right", unit="B"),
            ]
            path = Path(directory) / "capture"
            bridge.write_capture_outputs(library, path, Path(directory) / "scripts")
            preview = bridge._capture_symbol_preview(path / "library.olb.xml", "R")
            self.assertEqual([pin["number"] for pin in preview["pins"]], ["1", "2"])

    def test_prepared_step_paths_identify_downloaded_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capture model ") as directory:
            catalog = Mock()
            catalog.fetch_part.return_value = sample_library()

            def download(_library, output, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                step = output / "part #1.step"
                step.write_text("downloaded test asset", encoding="utf-8")
                return EasyEDA3DFetchReport(str(output), ["step"], models=[
                    Model3D(file=str(step), key="C25804_R0603", format="step"),
                ])

            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", side_effect=download
            ), patch.object(bridge, "urlopen", side_effect=OSError("Viewer unavailable")):
                result = bridge.import_part("C25804", Path(directory), prepare_only=True,
                                            viewer_base_url="http://127.0.0.1:8123/")
            self.assertEqual(result["step_status"], "available")
            self.assertEqual(result["step_files"], [str(Path(result["build_dir"]) / "3d" / "part #1.step")])
            query = parse_qs(urlsplit(result["step_viewer_url"]).query)
            self.assertEqual(query["dir"], [Path(directory).resolve().as_posix()])
            self.assertEqual(query["file"], [Path(result["step_files"][0]).relative_to(directory).as_posix()])

    def test_cached_results_gain_previews_and_current_viewer_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capture cached ") as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            tools = CadenceTools(allegro=Path("allegro.exe"), allegro_batch=Path("allegro_batch.exe"))

            def download(_library, output, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                step = output / "part #1.step"
                step.write_text("downloaded test asset", encoding="utf-8")
                return EasyEDA3DFetchReport(str(output), ["step"], models=[
                    Model3D(file=str(step), key="C25804_R0603", format="step"),
                ])

            def build(_tools, symbols):
                (symbols / "C25804_R0603.dra").write_bytes(b"test dra")
                (symbols / "c25804_r0603.psm").write_bytes(b"test psm")
                return [{"status": "created_psm"}]

            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", side_effect=download
            ) as fetch, patch.object(bridge.CadenceTools, "discover", return_value=tools), patch.object(
                bridge, "run_symbol_build_plan", side_effect=build
            ) as native_build, patch.object(bridge, "run_step_mapping", return_value={"status": "mapped"}), patch.object(
                bridge, "compile_existing_package_symbols", return_value=[{"status": "created_psm"}]
            ), patch.object(bridge, "urlopen", side_effect=OSError("Viewer unavailable")):
                first = bridge.import_part("C25804", Path(directory))
                manifest = Path(first["build_dir"]) / "capture_browser.json"
                old = json.loads(manifest.read_text(encoding="utf-8"))
                for key in ("preview_data", "step_files", "footprint_path", "step_viewer_url"):
                    old.pop(key)
                manifest.write_text(json.dumps(old), encoding="utf-8")
                xml = ElementTree.parse(first["capture_xml"])
                xml.find("./Package/LibPart/NormalView/SymbolPinScalar/Defn").set("hotptY", "77")
                xml.write(first["capture_xml"], encoding="utf-8")
                second = bridge.import_part("C25804", Path(directory), viewer_base_url="http://localhost:8456/view")
                third = bridge.import_part("C25804", Path(directory))
            self.assertTrue(second["cached"])
            self.assertEqual(second["build_dir"], first["build_dir"])
            self.assertEqual(second["preview_data"]["symbol"]["pins"][0]["y"], -77)
            self.assertEqual(second["step_files"], [str(Path(directory) / "3d" / "part #1.step")])
            self.assertEqual(second["footprint_path"], str(Path(directory) / "allegro" / "symbols" / "C25804_R0603.dra"))
            self.assertTrue(Path(second["footprint_path"]).is_file())
            query = parse_qs(urlsplit(second["step_viewer_url"]).query)
            self.assertEqual(query["file"], ["3d/part #1.step"])
            self.assertEqual(query["dir"], [Path(directory).resolve().as_posix()])
            self.assertEqual(third["step_viewer_url"], "")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(native_build.call_count, 1)

    def test_viewer_rejects_remote_or_non_http_targets_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "_catalog") as catalog:
            for url in ("https://localhost:8000", "http://example.com", "http://localhost.example.com",
                        "file:///tmp/viewer", "http://user@localhost", "http://localhost:invalid"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    bridge.import_part("C25804", Path(directory) / "library", viewer_base_url=url)
            catalog.assert_not_called()
            self.assertFalse((Path(directory) / "library").exists())

    def test_worker_forwards_viewer_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, "import_part", return_value={"ok": True}) as run:
            code = bridge.main([
                "import", "--part", "C25804", "--library-root", directory,
                "--output", str(Path(directory) / "reply.json"), "--viewer-base-url", "http://localhost:8000/",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.kwargs["viewer_base_url"], "http://localhost:8000/")

    def test_failed_allegro_build_does_not_publish_a_usable_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.return_value = sample_library()
            report = EasyEDA3DFetchReport(output_dir=directory, formats=["step"])
            tools = CadenceTools(allegro=Path("allegro.exe"), allegro_batch=Path("allegro_batch.exe"))
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", return_value=report
            ), patch.object(bridge.CadenceTools, "discover", return_value=tools), patch.object(
                bridge, "run_symbol_build_plan", return_value=[{"status": "failed"}]
            ):
                with self.assertRaisesRegex(RuntimeError, "footprint generation failed"):
                    bridge.import_part("C25804", Path(directory))
            self.assertFalse((Path(directory) / "parts" / "C25804" / "latest.json").exists())
            self.assertFalse((Path(directory) / "allegro" / "symbols").exists())

    def test_worker_writes_structured_failure_for_plugin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capture response ") as directory:
            response = Path(directory) / "reply.json"
            with patch.object(bridge, "search_parts", side_effect=RuntimeError("Network unavailable")):
                code = bridge.main([
                    "search", "--query", "NE555", "--library-root", directory,
                    "--output", str(response),
                ])
            self.assertEqual(code, 1)
            payload = json.loads(response.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "Network unavailable")
            self.assertEqual(payload["user_message"], "Network unavailable")
            self.assertEqual(payload["error_code"], "operation_failed")

    def test_explicit_cadence_target_does_not_fall_back_to_another_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "SPB_17.4"
            (existing / "tools" / "bin").mkdir(parents=True)
            (existing / "tools" / "bin" / "Capture.exe").write_bytes(b"fixture")
            with patch("jlc2cadence.validators.cadence_runner.DEFAULT_CADENCE_ROOTS", [existing]):
                self.assertEqual(CadenceTools.discover().capture, existing / "tools" / "bin" / "Capture.exe")
                self.assertIsNone(CadenceTools.discover(Path(directory) / "absent-SPB_17.2").capture)

    def test_two_cadence_versions_cannot_share_a_content_addressed_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            report = EasyEDA3DFetchReport(output_dir=directory, formats=["step"])
            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", return_value=report
            ):
                first = bridge.import_part("C25804", root, cadence_root=Path(directory) / "SPB_17.2", prepare_only=True)
                second = bridge.import_part("C25804", root, cadence_root=Path(directory) / "SPB_17.4", prepare_only=True)
            self.assertNotEqual(first["build_dir"], second["build_dir"])
            self.assertNotEqual(first["cadence_target"], second["cadence_target"])
            self.assertTrue(Path(first["capture_xml"]).is_file())
            self.assertTrue(Path(second["capture_xml"]).is_file())

    def test_import_worker_preserves_details_and_reports_known_source_failures(self) -> None:
        cases = [
            ("no_cad_library", "No EasyEDA CAD library was found for C25804.",
             "\u6ca1\u6709 CAD \u6587\u4ef6\uff1a\u672a\u627e\u5230\u8be5\u5143\u5668\u4ef6\u7684\u7acb\u521b\u539f\u59cb CAD \u5e93\uff0c\u65e0\u6cd5\u5bfc\u5165\u3002"),
            ("unsupported_geometry", "A custom/polygon pad cannot be represented faithfully by the current Cadence generator.",
             "\u7531\u4e8e\u662f\u5f02\u5f62\u7ed3\u6784\uff0c\u5f53\u524d\u65e0\u6cd5\u751f\u6210\u3002"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            response = Path(directory) / "reply.json"
            for error_code, detail, message in cases:
                with self.subTest(error_code=error_code), patch.object(
                    bridge, "import_part", side_effect=CatalogError(detail, error_code=error_code)
                ):
                    code = bridge.main([
                        "import", "--part", "C25804", "--library-root", directory,
                        "--output", str(response),
                    ])
                    payload = json.loads(response.read_text(encoding="utf-8"))
                    self.assertEqual(code, 1)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["error"], detail)
                    self.assertEqual(payload["error_type"], "CatalogError")
                    self.assertEqual(payload["error_code"], error_code)
                    self.assertEqual(payload["user_message"], message)
                    self.assertNotIn("olb_path", payload)

    def test_failure_messages_require_explicit_source_codes(self) -> None:
        for detail in (
            "Catalog request failed: HTTP Error 403: Forbidden",
            "No valid symbol library ID was returned for C25804.",
            "C25804 does not include both a symbol and a footprint.",
            "The symbol electrical type contains an invalid numeric coordinate.",
            "The original footprint RECT record has no unambiguous documented fill/stroke semantics supported by this converter.",
            "original rounded rectangle radii are invalid at Capture precision",
            "Allegro footprint generation failed; inspect create_symbols.results.json",
            "Unsupported source electrical type",
            "Unicode native readback mismatch",
        ):
            with self.subTest(detail=detail):
                payload = bridge.failure_response(CatalogError(detail))
                self.assertEqual(payload["user_message"], detail)
                self.assertEqual(payload["error_code"], "operation_failed")

    def test_import_retries_missing_step_instead_of_reusing_failed_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Mock()
            catalog.fetch_part.side_effect = lambda _part: sample_library()
            report = EasyEDA3DFetchReport(output_dir=directory, formats=["step"])
            tools = CadenceTools(allegro=Path("allegro.exe"), allegro_batch=Path("allegro_batch.exe"))

            def build(_tools, symbols):
                (symbols / "C25804_R0603.dra").write_bytes(b"test dra")
                (symbols / "c25804_r0603.psm").write_bytes(b"test psm")
                return [{"status": "created_psm"}]

            with patch.object(bridge, "_catalog", return_value=catalog), patch.object(
                bridge, "fetch_easyeda_3d_models", return_value=report
            ) as download, patch.object(bridge.CadenceTools, "discover", return_value=tools), patch.object(
                bridge, "run_symbol_build_plan", side_effect=build
            ):
                first = bridge.import_part("C25804", Path(directory))
                second = bridge.import_part("C25804", Path(directory))
            self.assertEqual(first["step_status"], "missing")
            self.assertFalse(second["cached"])
            self.assertEqual(download.call_count, 2)


class CompactStepPreviewTests(unittest.TestCase):
    def glb(self) -> bytes:
        content = b'{"asset":{"version":"2.0"}}'
        content += b" " * (-len(content) % 4)
        return struct.pack("<4sII", b"glTF", 2, 20 + len(content)) + struct.pack("<I4s", len(content), b"JSON") + content

    def setup_step(self, root: Path):
        step = root / "3d" / "part #1 \u6a21\u578b.step"
        step.parent.mkdir(parents=True)
        step.write_bytes(b"ISO-10303-21;\noriginal source geometry\nEND-ISO-10303-21;")
        return step, step.with_name(f".{step.name}.glb")

    def result(self, step: Path):
        return {"ok": True, "step_files": [str(step)], "warnings": []}

    def service(self, step: Path, sidecar: Path, calls: list):
        def respond(request, timeout):
            calls.append((request, timeout))
            if request.get_method() == "GET":
                payload = {"app": "cad-viewer", "dynamicRoot": True, "serverApiVersion": 2}
            else:
                sidecar.write_bytes(self.glb())
                timestamp = step.stat().st_mtime_ns + 1_000_000
                os.utime(sidecar, ns=(timestamp, timestamp))
                payload = {"ok": True, "result": {"ok": True, "stepPath": str(step), "glbPath": str(sidecar)}}
            return io.BytesIO(json.dumps(payload).encode())
        return respond

    def test_current_glb_is_delivered_without_contacting_viewer_or_changing_step(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, sidecar = self.setup_step(root)
            source = step.read_bytes()
            sidecar.write_bytes(self.glb())
            result = self.result(step)
            with patch.object(bridge, "urlopen") as request:
                bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            request.assert_not_called()
            preview = result["preview_3d"]
            self.assertEqual(preview["format"], "glb")
            self.assertEqual(base64.b64decode(preview["data_base64"]), self.glb())
            self.assertEqual(Path(preview["path"]), sidecar)
            self.assertEqual(Path(preview["source_step"]), step)
            self.assertEqual(step.read_bytes(), source)

    def test_missing_or_stale_glb_uses_verified_viewer_and_exact_step_query(self):
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                step, sidecar = self.setup_step(root)
                source = step.read_bytes()
                if stale:
                    sidecar.write_bytes(self.glb())
                    timestamp = step.stat().st_mtime_ns - 1_000_000_000
                    os.utime(sidecar, ns=(timestamp, timestamp))
                calls = []
                result = self.result(step)
                with patch.object(bridge, "urlopen", side_effect=self.service(step, sidecar, calls)):
                    bridge._add_compact_step_preview(result, root, "http://127.0.0.1:8123/view?old=1")
                self.assertEqual([request.get_method() for request, _ in calls], ["GET", "POST"])
                self.assertTrue(all(timeout <= 45 for _, timeout in calls))
                self.assertEqual(calls[0][0].full_url, "http://127.0.0.1:8123/__cad/server")
                query = parse_qs(urlsplit(calls[1][0].full_url).query)
                expected = {"dir": [root.as_posix()], "file": [step.relative_to(root).as_posix()]}
                if stale:
                    expected["force"] = ["1"]
                self.assertEqual(query, expected)
                self.assertEqual(result["preview_3d"]["source_step"], str(step))
                self.assertEqual(step.read_bytes(), source)
                self.assertEqual(result["warnings"], [])

    def test_incompatible_service_is_never_asked_to_convert(self):
        for fields in ({"app": "other"}, {"dynamicRoot": False}, {"serverApiVersion": 1}, {"serverApiVersion": "2"}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                step, _ = self.setup_step(root)
                result = self.result(step)
                metadata = {"app": "cad-viewer", "dynamicRoot": True, "serverApiVersion": 2, **fields}
                with patch.object(bridge, "urlopen", return_value=io.BytesIO(json.dumps(metadata).encode())) as request:
                    bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
                self.assertEqual(request.call_count, 1)
                self.assertEqual(result["preview_3d"], {})
                self.assertTrue(result["warnings"])
                self.assertTrue(result["ok"])

    def test_viewer_failure_is_optional_and_preserves_step(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, _ = self.setup_step(root)
            source = step.read_bytes()
            result = self.result(step)
            with patch.object(bridge, "urlopen", side_effect=OSError("Viewer offline")):
                bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            self.assertEqual(result["preview_3d"], {})
            self.assertTrue(result["ok"])
            self.assertIn("Viewer offline", result["warnings"][0])
            self.assertEqual(step.read_bytes(), source)

    def test_recovered_preview_clears_old_optional_failure_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, sidecar = self.setup_step(root)
            result = self.result(step)
            result["warnings"] = ["Keep this warning", "Compact 3D preview unavailable: prior failure"]
            sidecar.write_bytes(self.glb())
            bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            self.assertTrue(result["preview_3d"])
            self.assertEqual(result["warnings"], ["Keep this warning"])

    def test_success_response_without_real_sidecar_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, _ = self.setup_step(root)
            result = self.result(step)
            responses = [
                io.BytesIO(json.dumps({"app": "cad-viewer", "dynamicRoot": True, "serverApiVersion": 2}).encode()),
                io.BytesIO(b'{"ok": true, "result": {"ok": true}}'),
            ]
            with patch.object(bridge, "urlopen", side_effect=responses):
                bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            self.assertEqual(result["preview_3d"], {})
            self.assertIn("did not produce a current GLB", result["warnings"][0])

    def test_invalid_and_oversized_glb_are_not_delivered(self):
        for data in (b"not a GLB", b"glTF" + b"\0" * 28, b"glTF" + b"x" * 100):
            with self.subTest(data=data[:8]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                step, sidecar = self.setup_step(root)
                sidecar.write_bytes(data)
                result = self.result(step)
                with patch.object(bridge, "MAX_PREVIEW_GLB_BYTES", 64):
                    bridge._add_compact_step_preview(result, root, "")
                self.assertEqual(result["preview_3d"], {})

    def test_step_outside_library_is_not_read_or_sent_to_viewer(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            step, sidecar = self.setup_step(Path(outside))
            sidecar.write_bytes(self.glb())
            result = self.result(step)
            with patch.object(bridge, "urlopen") as request:
                bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            request.assert_not_called()
            self.assertEqual(result["preview_3d"], {})
            self.assertTrue(result["warnings"])

    def test_another_returned_artifact_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            step, sidecar = self.setup_step(root)
            result = self.result(step)
            calls = []
            service = self.service(step, sidecar, calls)

            def respond(request, timeout):
                response = service(request, timeout)
                if request.get_method() == "POST":
                    payload = json.loads(response.read())
                    payload["result"]["glbPath"] = str(root / "unrelated.glb")
                    return io.BytesIO(json.dumps(payload).encode())
                return response

            with patch.object(bridge, "urlopen", side_effect=respond):
                bridge._add_compact_step_preview(result, root, "http://localhost:8123/")
            self.assertEqual(result["preview_3d"], {})
            self.assertIn("another STEP file", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
