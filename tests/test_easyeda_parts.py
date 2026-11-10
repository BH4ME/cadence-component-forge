from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jlc2cadence.cli import main
from jlc2cadence.extractors.easyeda_3d import EasyEDA3DFetchReport
from jlc2cadence.extractors.easyeda_parts import (
    LCSC3DResolution,
    LCSC3DResolutionReport,
    resolve_lcsc_3d_models,
)
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


def _metadata_payload(model_id: str = "model-123") -> bytes:
    return json.dumps(
        {
            "success": True,
            "result": {
                "title": "Test component",
                "packageDetail": {
                    "title": "SOT-23",
                    "dataStr": {
                        "head": {
                            "uuid_3d": model_id,
                            "c_para": {"3DModel": "SOT-23"},
                        }
                    },
                },
            },
        }
    ).encode("utf-8")


def _lceda_step_payload(lcsc_id: str = "C12345", step_id: str = "step-model") -> bytes:
    return json.dumps(
        {
            "success": True,
            "code": 0,
            "result": [
                {
                    "uuid": "component-uuid",
                    "puuid": "package-uuid",
                    "number": lcsc_id,
                    "step": step_id,
                }
            ],
        }
    ).encode("utf-8")


class EasyEDAPartResolutionTests(unittest.TestCase):
    def _library(self, *lcsc_ids: str) -> LibraryIR:
        components = [
            Component(
                name=f"U_{index}",
                footprint="SOT_TEST",
                properties={"Supplier Part": value},
            )
            for index, value in enumerate(lcsc_ids, start=1)
        ]
        return LibraryIR(
            components=components,
            footprints=[Footprint(name="SOT_TEST")],
        )

    def test_resolves_missing_3d_id_from_lcsc_metadata(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request: object, **_kwargs: object) -> _Response:
            url = getattr(request, "full_url", "")
            calls.append(url)
            if "/api/components/searchByNumbers" in url:
                return _Response(_lceda_step_payload("C12345", ""))
            return _Response(_metadata_payload())

        report = resolve_lcsc_3d_models(
            self._library("C12345"),
            urlopen=fake_urlopen,
        )

        self.assertEqual(report.status, "complete")
        self.assertEqual(report.resolved_count, 1)
        self.assertEqual(report.assigned_footprints, ["SOT_TEST"])
        self.assertEqual(report.resolutions[0].source, "easyeda_product_components")
        self.assertEqual(
            calls,
            [
                "https://lceda.cn/api/components/searchByNumbers",
                "https://easyeda.com/api/products/C12345/components",
            ],
        )

    def test_resolves_lceda_step_by_number_before_easyeda_metadata(self) -> None:
        calls: list[str] = []

        def fake_urlopen(request: object, **_kwargs: object) -> _Response:
            url = getattr(request, "full_url", "")
            calls.append(url)
            if "/api/components/searchByNumbers" in url:
                return _Response(_lceda_step_payload("C12345", "official-step-id"))
            if "/api/products/C12345/components" in url:
                self.fail("direct LCEDA step should skip EasyEDA metadata")
            self.fail(f"unexpected URL: {url}")

        library = self._library("C12345")
        report = resolve_lcsc_3d_models(library, urlopen=fake_urlopen)

        self.assertEqual(report.status, "complete")
        self.assertEqual(report.resolutions[0].source, "lceda_search_by_numbers")
        self.assertEqual(report.resolutions[0].model_id, "official-step-id")
        self.assertEqual(
            library.footprints[0].properties["EasyEDA_3D_Model_Id"],
            "official-step-id",
        )
        self.assertEqual(
            library.footprints[0].properties["EasyEDA_3D_Resolution_Source"],
            "lceda_search_by_numbers",
        )
        self.assertEqual(
            calls,
            ["https://lceda.cn/api/components/searchByNumbers"],
        )

    def test_lceda_step_by_number_empty_step_keeps_model_unresolved(self) -> None:
        def fake_urlopen(request: object, **_kwargs: object) -> _Response:
            url = getattr(request, "full_url", "")
            if "/api/products/C12345/components" in url:
                return _Response(_metadata_payload(""))
            if "/api/components/searchByNumbers" in url:
                return _Response(_lceda_step_payload("C12345", ""))
            self.fail(f"unexpected URL: {url}")

        library = self._library("C12345")
        report = resolve_lcsc_3d_models(library, urlopen=fake_urlopen)

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.resolutions[0].status, "no_3d_model")
        self.assertIn("step UUID", report.resolutions[0].error)
        self.assertNotIn("EasyEDA_3D_Model_Id", library.footprints[0].properties)

    def test_resolution_writes_and_reuses_metadata_cache(self) -> None:
        cache_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_lcsc_cache_"))
        try:
            call_count = 0

            def fake_urlopen(_request: object, **_kwargs: object) -> _Response:
                nonlocal call_count
                call_count += 1
                return _Response(_metadata_payload("cached-model"))

            first_library = self._library("C12345")
            first = resolve_lcsc_3d_models(
                first_library,
                cache_dir=cache_dir,
                urlopen=fake_urlopen,
            )
            second_library = self._library("C12345")
            second = resolve_lcsc_3d_models(
                second_library,
                cache_dir=cache_dir,
                urlopen=lambda *_args, **_kwargs: self.fail("metadata network should be cached"),
            )

            self.assertEqual(first.status, "complete")
            self.assertEqual(first.resolutions[0].status, "resolved")
            self.assertEqual(second.status, "complete")
            self.assertEqual(second.resolutions[0].status, "cached")
            self.assertEqual(call_count, 2)
            self.assertEqual(
                second_library.footprints[0].properties["EasyEDA_3D_Model_Id"],
                "cached-model",
            )
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_explicit_model_id_skips_lcsc_lookup(self) -> None:
        library = self._library("C12345")
        library.footprints[0].properties["EasyEDA_3D_Model_Id"] = "explicit-model"
        report = resolve_lcsc_3d_models(
            library,
            urlopen=lambda *_args, **_kwargs: self.fail("explicit model should not query LCSC"),
        )

        self.assertEqual(report.status, "no_lcsc_parts")
        self.assertEqual(library.footprints[0].properties["EasyEDA_3D_Model_Id"], "explicit-model")

    def test_multiple_lcsc_models_for_one_footprint_are_not_guessed(self) -> None:
        def fake_urlopen(request: object, **_kwargs: object) -> _Response:
            url = getattr(request, "full_url", "")
            model_id = "model-a" if "C12345" in url else "model-b"
            return _Response(_metadata_payload(model_id))

        library = self._library("C12345", "C67890")
        report = resolve_lcsc_3d_models(library, urlopen=fake_urlopen)

        self.assertEqual(report.status, "partial")
        self.assertEqual(report.ambiguous_footprints, ["SOT_TEST"])
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.to_dict()["error_count"], 1)
        self.assertNotIn("EasyEDA_3D_Model_Id", library.footprints[0].properties)

    def test_fetch_cli_reports_ambiguous_lcsc_error_count(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_v3" / "PRO_DEVICE.epru"
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_lcsc_fetch_ambiguous_out_"))
        try:
            lcsc_report = LCSC3DResolutionReport(
                resolutions=[
                    LCSC3DResolution(
                        lcsc_id="C12345",
                        footprint_names=["SOT_23_3_PRO"],
                        status="resolved",
                        model_id="model-a",
                    ),
                    LCSC3DResolution(
                        lcsc_id="C67890",
                        footprint_names=["SOT_23_3_PRO"],
                        status="resolved",
                        model_id="model-b",
                    ),
                ],
                ambiguous_footprints=["SOT_23_3_PRO"],
            )
            fetch_report = EasyEDA3DFetchReport(
                output_dir=str(out_dir),
                formats=["step"],
            )
            with contextlib.redirect_stdout(io.StringIO()), patch(
                "jlc2cadence.cli.resolve_lcsc_3d_models",
                return_value=lcsc_report,
            ), patch(
                "jlc2cadence.cli.fetch_easyeda_3d_models",
                return_value=fetch_report,
            ):
                code = main(
                    [
                        "fetch-3d",
                        "--input",
                        str(fixture),
                        "--out",
                        str(out_dir),
                        "--resolve-lcsc-3d",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(
                (out_dir / "easyeda_3d_download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "no_model_ids")
            resolution = json.loads(
                (out_dir / "easyeda_lcsc_3d_resolution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(resolution["error_count"], 1)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_fetch_cli_can_resolve_lcsc_before_model_download(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_v3" / "PRO_DEVICE.epru"
        input_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_lcsc_fetch_input_"))
        out_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_lcsc_fetch_out_"))
        try:
            content = fixture.read_text(encoding="utf-8")
            content = content.replace('"3D Model":"model-pro",', "")
            content = content.replace('"3D Model Title":"SOT_23_3_PRO",', "")
            content = content.replace('"3D Model Transform":"118.11,70.866,0,90,0,0,0.1,-0.2,0.3",', "")
            content = content.replace('"Supplier Part":"C000001"', '"Supplier Part":"C12345"')
            (input_dir / "PRO_DEVICE.epru").write_text(content, encoding="utf-8")

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                url = getattr(request, "full_url", "")
                if "/api/products/C12345/components" in url:
                    return _Response(_metadata_payload("model-123"))
                return _Response(b"ISO-10303-21;\nEND-ISO-10303-21;")

            with contextlib.redirect_stdout(io.StringIO()), patch(
                "jlc2cadence.extractors.easyeda_parts.urllib.request.urlopen",
                fake_urlopen,
            ), patch(
                "jlc2cadence.extractors.easyeda_3d.urllib.request.urlopen",
                fake_urlopen,
            ):
                code = main(
                    [
                        "fetch-3d",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(out_dir),
                        "--resolve-lcsc-3d",
                        "--strict",
                    ]
                )

            self.assertEqual(code, 0)
            resolution = json.loads(
                (out_dir / "easyeda_lcsc_3d_resolution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(resolution["status"], "complete")
            self.assertEqual(resolution["resolved_count"], 1)
            download = json.loads(
                (out_dir / "easyeda_3d_download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(download["status"], "complete")
            self.assertTrue((out_dir / "model_123.step").exists())
        finally:
            shutil.rmtree(input_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_manifest_and_batch_propagate_lcsc_resolution(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "easyeda_pro_v3" / "PRO_DEVICE.epru"
        work_dir = Path(tempfile.mkdtemp(prefix="jlc2cadence_lcsc_batch_"))
        out_dir = work_dir / "batch_out"
        try:
            content = fixture.read_text(encoding="utf-8")
            content = content.replace('"3D Model":"model-pro",', "")
            content = content.replace('"3D Model Title":"SOT_23_3_PRO",', "")
            content = content.replace('"3D Model Transform":"118.11,70.866,0,90,0,0,0.1,-0.2,0.3",', "")
            content = content.replace('"Supplier Part":"C000001"', '"Supplier Part":"C12345"')
            input_dir = work_dir / "input"
            input_dir.mkdir()
            (input_dir / "PRO_DEVICE.epru").write_text(content, encoding="utf-8")
            manifest = work_dir / "manifest.json"

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "manifest",
                            "--out",
                            str(manifest),
                            "--name",
                            "lcsc_sample",
                            "--input",
                            str(input_dir),
                            "--resolve-lcsc-3d",
                        ]
                    ),
                    0,
                )

            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            sample = manifest_payload["samples"][0]
            self.assertTrue(sample["resolve_lcsc_3d"])

            def fake_urlopen(request: object, **_kwargs: object) -> _Response:
                return _Response(_metadata_payload("model-123"))

            with patch(
                "jlc2cadence.extractors.easyeda_parts.urllib.request.urlopen",
                fake_urlopen,
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "batch",
                            "--manifest",
                            str(manifest),
                            "--out",
                            str(out_dir),
                            "--strict",
                        ]
                    ),
                    0,
                )

            summary = json.loads((out_dir / "batch_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["resolve_lcsc_3d"])
            row = summary["samples"][0]
            self.assertTrue(row["resolve_lcsc_3d_requested"])
            self.assertEqual(row["lcsc_resolution_status"], "complete")
            self.assertEqual(row["lcsc_resolution_count"], 1)
            self.assertEqual(row["lcsc_resolution_error_count"], 0)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
