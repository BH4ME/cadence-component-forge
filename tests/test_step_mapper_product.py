from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jlc2cadence.generators.model3d import write_step_mapping_script


@unittest.skipUnless(os.name == "nt", "Generated PowerShell launcher requires Windows")
class StepMapperProductTests(unittest.TestCase):
    def invoke_launcher(self, product=None):
        with tempfile.TemporaryDirectory(prefix="jlc mapper test ") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            models = root / "3d"
            models.mkdir()
            (models / "cadence_step_package.map").write_text(
                "<STEP-3D-MAPPING><Body /></STEP-3D-MAPPING>", encoding="utf-8")
            replay = scripts / "run_step_package_mapping.scr"
            replay.write_text("", encoding="utf-8")
            generated = write_step_mapping_script([], scripts / "prepare_step_mapping.ps1")
            wrapper = root / "invoke.ps1"
            report = root / "launch.json"
            wrapper.write_text(
                "param([string]$Generated, [string]$Report, [string]$SelectedProduct)\n"
                "$ErrorActionPreference = 'Stop'\n"
                "$global:JlcMapperTestReport = $Report\n"
                "function Start-Process {\n"
                "  param($FilePath, $ArgumentList, $WorkingDirectory)\n"
                "  @{ executable=$FilePath; arguments=$ArgumentList; cwd=$WorkingDirectory } |\n"
                "    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $global:JlcMapperTestReport -Encoding UTF8\n"
                "}\n"
                "if ($SelectedProduct) { & $Generated -OpenMapper -Product $SelectedProduct }\n"
                "else { & $Generated -OpenMapper }\n", encoding="utf-8")
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                       str(wrapper), "-Generated", str(generated), "-Report", str(report)]
            if product is not None:
                command.extend(["-SelectedProduct", product])
            process = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                     timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            launch = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else None
            return process, launch, str(replay)

    def test_explicit_product_reaches_native_launcher(self):
        process, launch, replay = self.invoke_launcher("OrCAD_PCB_Designer")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(launch["arguments"], ["-product", "OrCAD_PCB_Designer", "-s", f'"{replay}"'])

    def test_no_product_preserves_default_selection_and_quotes_replay(self):
        process, launch, replay = self.invoke_launcher()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(launch["arguments"], ["-s", f'"{replay}"'])

    def test_rejects_product_with_command_delimiters(self):
        process, launch, _ = self.invoke_launcher('Product" -s unexpected.scr')
        self.assertNotEqual(process.returncode, 0)
        self.assertIsNone(launch)


if __name__ == "__main__":
    unittest.main()
