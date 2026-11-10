from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import gzip
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.cli import main
from jlc2cadence.extractors.easyeda_3d import fetch_easyeda_3d_models, load_model_sources
from jlc2cadence.extractors.easyeda_cache import import_easyeda_cached_3d_models
from jlc2cadence.ir.schema import Component, Footprint, LibraryIR


class _Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class EasyEDA3DDownloadTests(unittest.TestCase):
    def _library(self, model_id: str = "model-123") -> LibraryIR:
        return LibraryIR(
            footprints=[
                Footprint(
                    name="SOT_TEST",
                    properties={
                        "EasyEDA_3D_Model_Id": model_id,
                        "EasyEDA_3D_Model_Title": "SOT TEST",
                    },
                )
            ]
        )

    def test_downloads_step_and_returns_bindable_model(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_test_"))
        try:
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                self._library(),
                out_dir,
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(len(report.models), 1)
            self.assertEqual(report.downloads[0].status, "downloaded")
            self.assertTrue(Path(report.models[0].file).exists())
            self.assertEqual(report.models[0].key, "model123")
            self.assertIn("/qAxj6KHrDKw4blvCG8QJPs7Y/model-123", calls[0])
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_download_uses_easyeda_composite_model_id_part(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_composite_test_"))
        try:
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                self._library("model-123|workspace-456"),
                out_dir,
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(len(calls), 1)
            self.assertIn("/qAxj6KHrDKw4blvCG8QJPs7Y/model-123", calls[0])
            self.assertNotIn("|", calls[0])
            row = report.downloads[0]
            self.assertEqual(row.model_id, "model-123|workspace-456")
            self.assertEqual(row.request_model_id, "model-123")
            self.assertEqual(row.attempted_model_ids, ["model-123"])
            self.assertEqual(Path(row.file).name, "model_123.step")
            self.assertEqual(report.models[0].key, "model123")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_cache_avoids_second_network_request(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_out_"))
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_cache_"))
        try:
            call_count = 0

            def fake_urlopen(_request: object, **_kwargs: object) -> _Response:
                nonlocal call_count
                call_count += 1
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            first = fetch_easyeda_3d_models(
                self._library(),
                out_dir,
                cache_dir=cache_dir,
                urlopen=fake_urlopen,
            )
            self.assertEqual(first.downloads[0].status, "downloaded")
            (out_dir / "model_123.step").unlink()

            second = fetch_easyeda_3d_models(
                self._library(),
                out_dir,
                cache_dir=cache_dir,
                urlopen=lambda *_args, **_kwargs: self.fail("network should not be called"),
            )
            self.assertEqual(second.downloads[0].status, "cached")
            self.assertEqual(call_count, 1)
            self.assertTrue((out_dir / "model_123.step").exists())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_concurrent_fetch_does_not_read_partially_written_cache(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jlc2cadence_3d_concurrent_") as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            payload = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
            writer_started, release_writer = threading.Event(), threading.Event()
            writer_id = []
            original_write = Path.write_bytes

            def paused_cache_write(path: Path, data: bytes) -> int:
                if path.parent != cache or threading.get_ident() != writer_id[0]:
                    return original_write(path, data)
                # Pause after a recognizable STEP header reaches disk.
                split = len(b"ISO-10303-21;\n")
                with path.open("wb") as stream:
                    stream.write(data[:split])
                    stream.flush()
                    writer_started.set()
                    if not release_writer.wait(5):
                        raise AssertionError("Concurrent cache reader did not finish")
                    stream.write(data[split:])
                return len(data)

            def fetch_writer():
                writer_id.append(threading.get_ident())
                return fetch_easyeda_3d_models(
                    self._library(), root / "writer", cache_dir=cache,
                    urlopen=lambda *_args, **_kwargs: _Response(payload),
                )

            with patch.object(Path, "write_bytes", paused_cache_write):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(fetch_writer)
                    try:
                        self.assertTrue(writer_started.wait(5), "Writer never reached cache publication")
                        reader = fetch_easyeda_3d_models(
                            self._library(), root / "reader", cache_dir=cache,
                            urlopen=lambda *_args, **_kwargs: _Response(payload),
                        )
                    finally:
                        release_writer.set()
                    writer = future.result(timeout=10)

            self.assertEqual(reader.status, "complete")
            self.assertEqual(writer.status, "complete")
            self.assertEqual(Path(reader.models[0].file).read_bytes(), payload)
            self.assertEqual(Path(writer.models[0].file).read_bytes(), payload)
            self.assertEqual((cache / "model_123.step").read_bytes(), payload)
            self.assertEqual([path.name for path in cache.iterdir()], ["model_123.step"])

    def test_cache_publication_failure_preserves_destination_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jlc2cadence_3d_publish_failure_") as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            cached = cache / "model_123.step"
            cached.write_bytes(b"invalid old cache")
            original_replace = Path.replace

            def failed_cache_replace(path: Path, target: Path) -> Path:
                if path.parent == cache:
                    raise OSError("cache publish interrupted")
                return original_replace(path, target)

            with patch.object(Path, "replace", failed_cache_replace):
                with self.assertRaisesRegex(OSError, "cache publish interrupted"):
                    fetch_easyeda_3d_models(
                        self._library(), root / "output", cache_dir=cache,
                        urlopen=lambda *_args, **_kwargs: _Response(b"ISO-10303-21;\nEND-ISO-10303-21;\n"),
                    )

            self.assertEqual(cached.read_bytes(), b"invalid old cache")
            self.assertEqual([path.name for path in cache.iterdir()], ["model_123.step"])

    def test_http_404_is_reported_without_creating_a_model(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_404_"))
        try:
            report = fetch_easyeda_3d_models(
                self._library("missing-model"),
                out_dir,
                urlopen=lambda *_args, **_kwargs: _Response(b"not found", 404),
            )
            self.assertEqual(report.status, "failed")
            self.assertEqual(report.downloads[0].status, "not_found")
            self.assertEqual(report.models, [])
            self.assertFalse((out_dir / "missing_model.step").exists())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_custom_model_source_override_recovers_missing_model(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_source_override_"))
        try:
            source_path = out_dir / "model_sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "include_defaults": False,
                        "models": {
                            "missing-model": {
                                "step": "https://mirror.invalid/models/{model_id}.step"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                self._library("missing-model"),
                out_dir / "models",
                model_sources=load_model_sources(source_path),
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(report.downloads[0].source, "custom_model")
            self.assertEqual(
                calls,
                ["https://mirror.invalid/models/missing-model.step"],
            )
            self.assertEqual(
                report.to_dict()["model_sources"]["path"],
                str(source_path.resolve()),
            )
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_custom_template_supports_composite_model_parts(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_source_template_"))
        try:
            source_path = out_dir / "model_sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "include_defaults": False,
                        "templates": {
                            "step": "https://mirror.invalid/{workspace_id}/{model_id_part}.step"
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                self._library("model-123|workspace-456"),
                out_dir / "models",
                model_sources=load_model_sources(source_path),
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(
                calls,
                ["https://mirror.invalid/workspace-456/model-123.step"],
            )
            self.assertEqual(report.downloads[0].source, "custom_template")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_custom_template_can_use_component_properties(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_component_template_"))
        try:
            library = self._library("missing-model")
            library.components.append(
                Component(
                    name="U_SOURCE",
                    footprint="SOT_TEST",
                    properties={"LCSC Part": "C12345"},
                )
            )
            source_path = out_dir / "model_sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "include_defaults": False,
                        "templates": {
                            "step": "https://mirror.invalid/lcsc/{component_lcsc_part}.step"
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                library,
                out_dir / "models",
                model_sources=load_model_sources(source_path),
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(
                calls,
                ["https://mirror.invalid/lcsc/C12345.step"],
            )
            self.assertEqual(report.downloads[0].source, "custom_template")
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_custom_override_can_match_component_property_value(self) -> None:
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_3d_component_override_"))
        try:
            library = self._library("missing-model")
            library.components.append(
                Component(
                    name="U_SOURCE",
                    footprint="SOT_TEST",
                    properties={"Manufacturer Part": "MPN-123"},
                )
            )
            source_path = out_dir / "model_sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "include_defaults": False,
                        "models": {
                            "MPN-123": {
                                "step": "https://mirror.invalid/vendor/{component_manufacturer_part}.step"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                calls.append(getattr(request, "full_url", ""))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            report = fetch_easyeda_3d_models(
                library,
                out_dir / "models",
                model_sources=load_model_sources(source_path),
                urlopen=fake_urlopen,
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(report.downloads[0].source, "custom_model")
            self.assertEqual(
                calls,
                ["https://mirror.invalid/vendor/MPN-123.step"],
            )
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_cli_fetch_3d_writes_report_and_model(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_fetch_cli_"))
        try:
            def fake_urlopen(_request: object, **_kwargs: object) -> _Response:
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            stdout = io.StringIO()
            with patch(
                "jlc2cadence.extractors.easyeda_3d.urllib.request.urlopen",
                fake_urlopen,
            ), contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "fetch-3d",
                        "--input",
                        str(fixture),
                        "--out",
                        str(out_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["model_count"], 1)
            self.assertTrue((out_dir / "model123.step").exists())
            report = json.loads(
                (out_dir / "easyeda_3d_download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status_counts"]["downloaded"], 1)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_convert_fetch_3d_attaches_downloaded_model(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        input_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_convert_fetch_input_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_convert_fetch_out_"))
        try:
            shutil.copytree(fixture, input_dir / "export", dirs_exist_ok=True)
            (input_dir / "export" / "SOT_LEGACY_TEST.step").unlink()
            source_path = input_dir / "model_sources.json"
            source_path.write_text(
                json.dumps(
                    {
                        "include_defaults": False,
                        "models": {
                            "model123": {
                                "step": "https://mirror.invalid/{model_id}.step"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def fake_urlopen(_request: object, **_kwargs: object) -> _Response:
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            with patch(
                "jlc2cadence.extractors.easyeda_3d.urllib.request.urlopen",
                fake_urlopen,
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "convert",
                        "--input",
                        str(input_dir / "export"),
                        "--out",
                        str(out_dir),
                        "--fetch-3d",
                        "--model-sources",
                        str(source_path),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            download = json.loads(
                (out_dir / "3d" / "easyeda_3d_download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(download["status"], "complete")
            self.assertEqual(download["status_counts"]["downloaded"], 1)
            self.assertEqual(download["model_sources"]["path"], str(source_path.resolve()))
            ir = json.loads(
                (out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8")
            )
            model = ir["footprints"][0]["model3d"]
            self.assertEqual(Path(model["source_file"]).name, "model123.step")
            self.assertTrue((out_dir / "3d" / "model123.step").exists())
            step_map = ET.parse(out_dir / "3d" / "cadence_step_package.map").getroot()
            self.assertEqual(
                step_map.find("./Body/MapItem/StepModel").attrib["file"],
                "model123.step",
            )
        finally:
            shutil.rmtree(input_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_batch_fetch_3d_downloads_and_reports_model(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        work_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_batch_fetch_input_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_batch_fetch_out_"))
        try:
            input_dir = work_dir / "export"
            shutil.copytree(fixture, input_dir)
            (input_dir / "SOT_LEGACY_TEST.step").unlink()
            manifest = work_dir / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "samples": [
                            {
                                "name": "legacy_fetch",
                                "input": "export",
                                "fetch_3d": True,
                                "expected": {
                                    "components": 1,
                                    "footprints": 1,
                                    "models": 1,
                                    "errors": 0,
                                    "warnings": 0,
                                },
                            }
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            def fake_urlopen(_request: object, **_kwargs: object) -> _Response:
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            with patch(
                "jlc2cadence.extractors.easyeda_3d.urllib.request.urlopen",
                fake_urlopen,
            ), contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "batch",
                        "--manifest",
                        str(manifest),
                        "--out",
                        str(out_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            summary = json.loads((out_dir / "batch_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["passed"], 1)
            row = summary["samples"][0]
            self.assertEqual(row["fetch_3d_requested"], True)
            self.assertEqual(row["fetch_3d_status"], "complete")
            self.assertEqual(row["fetch_3d_download_count"], 1)
            self.assertEqual(row["fetch_3d_model_count"], 1)
            self.assertEqual(row["fetch_3d_error_count"], 0)
            self.assertEqual(row["models_count"], 1)
            download = json.loads(
                (out_dir / "legacy_fetch" / "3d" / "easyeda_3d_download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(download["status"], "complete")
            step_map = ET.parse(out_dir / "legacy_fetch" / "3d" / "cadence_step_package.map").getroot()
            self.assertEqual(
                step_map.find("./Body/MapItem/StepModel").attrib["file"],
                "model123.step",
            )
            markdown = (out_dir / "batch_summary.md").read_text(encoding="utf-8")
            self.assertIn(
                "| Sample | Status | Output | Audit | Capture | 3D Review | Signoff | Signoff Template | Require 3D | Require Signoff | HTTP 3D | Cache 3D | 3D errors |",
                markdown,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_local_cache_import_extracts_embedded_step_payload(self) -> None:
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_out_"))
        try:
            cached_file = cache_dir / "cache_entry"
            cached_file.write_bytes(
                b"model-123\n"
                b"prefix bytes before payload\n"
                b"ISO-10303-21;\n"
                b"HEADER;\nENDSEC;\n"
                b"DATA;\nENDSEC;\n"
                b"END-ISO-10303-21;\n"
            )
            report = import_easyeda_cached_3d_models(
                self._library("model-123"),
                [cache_dir],
                out_dir,
                formats=["step"],
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(report.matched_model_ids, ["model-123"])
            self.assertEqual(len(report.models), 1)
            model_path = Path(report.models[0].file)
            self.assertEqual(model_path.name, "model_123.step")
            self.assertEqual(
                model_path.read_bytes(),
                b"ISO-10303-21;\n"
                b"HEADER;\nENDSEC;\n"
                b"DATA;\nENDSEC;\n"
                b"END-ISO-10303-21;\n",
            )
            self.assertEqual(report.payloads[0].source_kind, "file")
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_local_cache_import_extracts_embedded_gzip_step_payload(self) -> None:
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_gzip_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_gzip_out_"))
        try:
            model = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
            (cache_dir / "cache_entry").write_bytes(
                b"cache header model-123\n" + gzip.compress(model) + b"\ntrailer"
            )
            report = import_easyeda_cached_3d_models(
                self._library("model-123"),
                [cache_dir],
                out_dir,
                formats=["step"],
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(len(report.models), 1)
            self.assertEqual(report.payloads[0].source_kind, "gzip_embedded")
            self.assertEqual(Path(report.models[0].file).read_bytes(), model)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_local_cache_import_extracts_embedded_zip_step_payload(self) -> None:
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_zip_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_local_cache_zip_out_"))
        try:
            model = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("model.step", model)
            (cache_dir / "cache_entry").write_bytes(
                b"cache header model-123\n" + archive_buffer.getvalue() + b"\ntrailer"
            )
            report = import_easyeda_cached_3d_models(
                self._library("model-123"),
                [cache_dir],
                out_dir,
                formats=["step"],
            )

            self.assertEqual(report.status, "complete")
            self.assertEqual(len(report.models), 1)
            self.assertEqual(report.payloads[0].source_kind, "zip_embedded:model.step")
            self.assertEqual(Path(report.models[0].file).read_bytes(), model)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_cli_scan_cache_3d_writes_import_report(self) -> None:
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_scan_cache_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_scan_cache_out_"))
        try:
            (cache_dir / "entry.bin").write_bytes(
                b"model123\nISO-10303-21;\nEND-ISO-10303-21;\n"
            )
            fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "scan-cache-3d",
                        "--input",
                        str(fixture),
                        "--cache",
                        str(cache_dir),
                        "--out",
                        str(out_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["model_count"], 1)
            report = json.loads(
                (out_dir / "easyeda_3d_cache_import.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["matched_model_ids"], ["model123"])
            self.assertTrue((out_dir / "model123.step").exists())
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_convert_local_model_cache_attaches_extracted_model(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        input_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_convert_cache_input_"))
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_convert_cache_root_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_convert_cache_out_"))
        try:
            shutil.copytree(fixture, input_dir / "export", dirs_exist_ok=True)
            (input_dir / "export" / "SOT_LEGACY_TEST.step").unlink()
            (cache_dir / "cached_model").write_bytes(
                b"model123\nISO-10303-21;\nEND-ISO-10303-21;\n"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "convert",
                        "--input",
                        str(input_dir / "export"),
                        "--out",
                        str(out_dir),
                        "--local-model-cache",
                        str(cache_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            cache_report = json.loads(
                (out_dir / "3d" / "easyeda_3d_cache_import.json").read_text(encoding="utf-8")
            )
            self.assertEqual(cache_report["status"], "complete")
            ir = json.loads(
                (out_dir / "reports" / "library.ir.json").read_text(encoding="utf-8")
            )
            self.assertEqual(Path(ir["footprints"][0]["model3d"]["file"]).name, "model123.step")
            self.assertEqual(Path(ir["footprints"][0]["model3d"]["source_file"]).name, "cached_model")
        finally:
            shutil.rmtree(input_dir, ignore_errors=True)
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_fetch_3d_local_cache_marks_existing_without_network(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_fetch_cache_root_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_fetch_cache_out_"))
        try:
            (cache_dir / "cached_model").write_bytes(
                b"model123\nISO-10303-21;\nEND-ISO-10303-21;\n"
            )
            with patch(
                "jlc2cadence.extractors.easyeda_3d.urllib.request.urlopen",
                side_effect=AssertionError("network should not be called after cache import"),
            ), contextlib.redirect_stdout(io.StringIO()) as redirected:
                code = main(
                    [
                        "fetch-3d",
                        "--input",
                        str(fixture),
                        "--out",
                        str(out_dir),
                        "--local-model-cache",
                        str(cache_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(redirected.getvalue())
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["cache_import_status"], "complete")
            report = json.loads((out_dir / "easyeda_3d_download.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status_counts"]["existing"], 1)
            self.assertTrue((out_dir / "model123.step").exists())
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_batch_uses_manifest_local_model_cache(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_legacy"
        work_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_batch_cache_input_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_batch_cache_out_"))
        try:
            input_dir = work_dir / "export"
            cache_dir = work_dir / "cache"
            shutil.copytree(fixture, input_dir)
            (input_dir / "SOT_LEGACY_TEST.step").unlink()
            cache_dir.mkdir()
            (cache_dir / "cached_model").write_bytes(
                b"model123\nISO-10303-21;\nEND-ISO-10303-21;\n"
            )
            manifest = work_dir / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "samples": [
                            {
                                "name": "legacy_cache",
                                "input": "export",
                                "local_model_cache": ["cache"],
                                "expected": {
                                    "components": 1,
                                    "footprints": 1,
                                    "models": 1,
                                    "cache_3d_models": 1,
                                    "cache_3d_missing": 0,
                                    "errors": 0,
                                    "warnings": 0,
                                },
                            }
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "batch",
                        "--manifest",
                        str(manifest),
                        "--out",
                        str(out_dir),
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            summary = json.loads((out_dir / "batch_summary.json").read_text(encoding="utf-8"))
            row = summary["samples"][0]
            self.assertEqual(row["cache_3d_status"], "complete")
            self.assertEqual(row["cache_3d_model_count"], 1)
            self.assertEqual(row["cache_3d_missing_count"], 0)
            self.assertEqual(row["models_count"], 1)
            self.assertTrue(
                (out_dir / "legacy_cache" / "3d" / "easyeda_3d_cache_import.json").exists()
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
