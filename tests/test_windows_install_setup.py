"""Opt-in actual Setup.exe install/upgrade/uninstall in owned fixture folders."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("JLC_WINDOWS_SETUP"),
                     "Set JLC_WINDOWS_SETUP to run the isolated real installer smoke test")
class WindowsSetupSmokeTests(unittest.TestCase):
    def test_actual_setup_supports_each_single_version(self):
        source = Path(os.environ["JLC_WINDOWS_SETUP"]).resolve()
        packaging = Path(__file__).resolve().parents[1] / "packaging/windows"
        info = json.loads((source.parent / "build-info.json").read_text(encoding="utf-8"))
        test_root = Path(os.environ.get("JLC_WINDOWS_TEST_ROOT", str(
            packaging / "test-runs"))).resolve()
        for version in ("17.2", "17.4"):
            with self.subTest(version=version):
                installation_id = "JLC2Cadence.Test." + uuid.uuid4().hex
                run = test_root / ("owned-" + uuid.uuid4().hex)
                install = run / "program"
                state = run / "custom-persistent"
                library = run / "user library"
                library.mkdir(parents=True)
                user_file = library / "original.step"
                user_file.write_bytes(b"original user CAD")
                root = run / ("Cadence " + version)
                (root / "tools/bin").mkdir(parents=True)
                (root / "tools/bin/capture.exe").write_bytes(b"MZ-install-fixture")
                (root / "tools/bin/allegro.exe").write_bytes(b"MZ-install-fixture")
                autoload = root / "tools/capture/tclscripts/capAutoLoad"
                autoload.mkdir(parents=True)
                unused_version = "17.4" if version == "17.2" else "17.2"
                unused_root = run / ("Cadence " + unused_version)
                self.assertFalse(unused_root.exists())
                evidence = {"schema": 1, "version": version, "owned_run": str(run),
                            "installation_id": installation_id, "cadence_launched": False,
                            "unused_root_absent": True, "steps": []}

                def invoke(executable, arguments, expected=0):
                    result = subprocess.run([str(executable), *arguments], capture_output=True,
                                            text=True, timeout=180,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    evidence["steps"].append({"exe": str(executable), "arguments": arguments,
                                              "exit_code": result.returncode})
                    (run / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

                def cleanup_owned_install(install=install, run=run):
                    uninstaller = install / "unins000.exe"
                    if uninstaller.is_file() and install.resolve().is_relative_to(run.resolve()):
                        subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                                       timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
                self.addCleanup(cleanup_owned_install)
                output = run / "fixture-setup"
                invoke(packaging / "tools/inno-6.4.3/ISCC.exe", ["/Q",
                       "/DPayloadDir=" + str(source.parent / "payload"),
                       "/DReleaseId=" + info["release_id"], "/DAppVersion=" + info["version"],
                       "/DOutputDirectory=" + str(output), "/DFixtureRoot=" + str(run),
                       "/DFixtureId=" + installation_id, str(packaging / "setup.iss")])
                setup = output / ("JLC2Cadence-" + info["version"] + "-TEST-ONLY-Setup.exe")
                common = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                          "/DIR=" + str(install), "/STATEROOT=" + str(state),
                          "/LIBRARYROOT=" + str(library)]
                invoke(setup, [*common, "/CADENCE172=", "/CADENCE174=",
                       "/LOG=" + str(run / "no-host-rejected.log")], expected=None)
                no_host_log = (run / "no-host-rejected.log").read_text(encoding="utf-8-sig")
                self.assertTrue(
                    "Select at least one valid Cadence root" in no_host_log or
                    "Select at least one Cadence 17.2 or 17.4 installation" in no_host_log,
                    no_host_log[-2000:],
                )
                self.assertFalse((install / "install-state.json").exists())
                invoke(setup, [*common, "/CADENCE" + version.replace(".", "") + "=" + str(root),
                       "/CADENCE" + unused_version.replace(".", "") + "=",
                       "/LOG=" + str(run / "single-version-install.log")])
                config_path = state / "config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(set(config["cadence_roots"]), {version})
                initializer = autoload / "jlc2capture_init.tcl"
                self.assertTrue(initializer.is_file())
                self.assertFalse(unused_root.exists())
                # The updater supplies no paths; installed single-host selection must survive.
                invoke(setup, ["/SILENT", "/NORESTART", "/LOG=" + str(run / "single-version-repair.log")])
                repaired = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(set(repaired["cadence_roots"]), {version})
                self.assertEqual(Path(repaired["library_root"]), library)
                self.assertEqual(json.loads((install / "install-state.json").read_text())["state_root"], str(state))
                invoke(install / "unins000.exe", ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                       "/LOG=" + str(run / "uninstall.log")])
                self.assertFalse(initializer.exists())
                self.assertFalse(unused_root.exists())
                self.assertEqual(user_file.read_bytes(), b"original user CAD")
                self.assertTrue(config_path.is_file())
                evidence.update(ok=True, installed_versions=[version], unused_root_absent=True,
                                single_version_repair_preserved=True, empty_selection_rejected=True,
                                user_library_preserved=True, configuration_preserved=True)
                (run / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
                print("Single-version installer evidence:", run / "acceptance.json")

    def test_actual_setup_upgrade_failure_and_uninstall_preserve_user_data(self):
        import winreg
        installation_id = "JLC2Cadence.Test." + uuid.uuid4().hex
        key = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + installation_id + "_is1"
        try:
            existing = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass
        else:
            existing.Close()
            self.fail("A per-user JLC2Cadence installation already exists; refusing to affect its registration")
        source_setup = Path(os.environ["JLC_WINDOWS_SETUP"]).resolve()
        source_previous = Path(os.environ.get("JLC_WINDOWS_PREVIOUS_SETUP", str(source_setup))).resolve()
        test_root = Path(os.environ.get("JLC_WINDOWS_TEST_ROOT", str(
            Path(__file__).resolve().parents[1] / "packaging/windows/test-runs"))).resolve()
        run = test_root / ("owned-" + uuid.uuid4().hex)
        install = run / "program"
        state = run / "persistent"
        library = run / "user library"
        library.mkdir(parents=True)
        user_file = library / "original.step"
        user_file.write_bytes(b"original user CAD")
        project = run / "user-project.opj"
        project.write_bytes(b"unrelated user project")
        cadence = []
        for version in ("17.2", "17.4"):
            root = run / ("Cadence " + version)
            (root / "tools/bin").mkdir(parents=True)
            # Explicit /FIXTURETEST mode never starts these non-executable fixtures.
            (root / "tools/bin/capture.exe").write_bytes(b"MZ-install-fixture")
            (root / "tools/bin/allegro.exe").write_bytes(b"MZ-install-fixture")
            (root / "tools/capture/tclscripts/capAutoLoad").mkdir(parents=True)
            cadence.append(root)
        evidence = {"schema": 1, "fixture_cadence_roots": True, "cadence_launched": False,
                    "production_artifact": str(source_setup),
                    "production_sha256": hashlib.sha256(source_setup.read_bytes()).hexdigest(),
                    "owned_run": str(run),
                    "installation_id": installation_id, "steps": []}

        def invoke(executable, arguments, expected=0):
            result = subprocess.run([str(executable), *arguments], capture_output=True, text=True,
                                    timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
            evidence["steps"].append({"exe": str(executable), "exit_code": result.returncode})
            (run / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            if expected is None:
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            else:
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

        packaging = Path(__file__).resolve().parents[1] / "packaging/windows"
        def compile_fixture(source, label):
            build = source.parent
            info = json.loads((build / "build-info.json").read_text(encoding="utf-8"))
            output = run / label
            invoke(packaging / "tools/inno-6.4.3/ISCC.exe", ["/Q", "/DPayloadDir=" + str(build / "payload"),
                    "/DReleaseId=" + info["release_id"], "/DAppVersion=" + info["version"],
                    "/DOutputDirectory=" + str(output), "/DFixtureRoot=" + str(run),
                    "/DFixtureId=" + installation_id, packaging / "setup.iss"])
            return output / ("JLC2Cadence-" + info["version"] + "-TEST-ONLY-Setup.exe")

        setup = compile_fixture(source_setup, "target-fixture")
        previous = setup if source_previous == source_setup else compile_fixture(source_previous, "previous-fixture")
        evidence.update(setup=str(setup), setup_sha256=hashlib.sha256(setup.read_bytes()).hexdigest(), previous_setup=str(previous))
        common = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                  "/DIR=" + str(install), "/STATEROOT=" + str(state), "/LIBRARYROOT=" + str(library),
                  "/CADENCE172=" + str(cadence[0]), "/CADENCE174=" + str(cadence[1])]
        invoke(previous, [*common, "/LOG=" + str(run / "first-install.log")])
        def cleanup_owned_install():
            uninstaller = install / "unins000.exe"
            if uninstaller.is_file() and install.resolve().is_relative_to(run.resolve()):
                subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                               timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(cleanup_owned_install)
        config_path = state / "config.json"
        config = json.loads(config_path.read_text())
        config["custom_user_option"] = "preserved on upgrade"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        old_pointer = (install / "active-release.tcl").read_bytes()
        old_config = config_path.read_bytes()
        old_state = (install / "install-state.json").read_bytes()
        initializers = [root / "tools/capture/tclscripts/capAutoLoad/jlc2capture_init.tcl" for root in cadence]
        original_initializer = initializers[1].read_bytes()
        initializers[1].write_bytes(b"# unrelated owner must survive\n")
        invoke(setup, [*common, "/LOG=" + str(run / "failed-upgrade.log")], expected=None)
        self.assertEqual((install / "active-release.tcl").read_bytes(), old_pointer)
        self.assertEqual(config_path.read_bytes(), old_config)
        self.assertEqual((install / "install-state.json").read_bytes(), old_state)
        self.assertEqual(initializers[1].read_bytes(), b"# unrelated owner must survive\n")
        initializers[1].write_bytes(original_initializer)
        invoke(setup, [*common, "/LOG=" + str(run / "upgrade.log")])
        upgraded = json.loads(config_path.read_text())
        self.assertEqual(upgraded["custom_user_option"], "preserved on upgrade")
        self.assertEqual(Path(upgraded["library_root"]), library)
        self.assertTrue(upgraded["isolate_host_libraries"])
        self.assertEqual(set(upgraded["cadence_roots"]), {"17.2", "17.4"})
        bundled_python = Path(upgraded["python"])
        invoke(bundled_python, ["-B", "-I", "-c", "import ssl,cryptography,svgelements,OCP;import jlc2cadence.updater;print('portable runtime OK')"])
        for index, initializer in enumerate(initializers):
            text = initializer.read_text()
            self.assertIn("JLC2CAPTURE_CADENCE_ROOT", text)
            self.assertIn("JLC2CAPTURE_CADENCE_VERSION", text)
            self.assertIn(("17.2", "17.4")[index], text)
            self.assertNotIn("binary decode", text)
        # Every destructive action is restricted to this unique, owned test installation.
        self.assertTrue(install.resolve().is_relative_to(run.resolve()))
        self.assertEqual(install.name, "program")
        uninstallers = list(install.glob("unins*.exe"))
        self.assertEqual(len(uninstallers), 1)
        invoke(uninstallers[0], ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=" + str(run / "uninstall.log")])
        deadline = time.monotonic() + 30
        while bundled_python.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(bundled_python.exists(), "Uninstaller left its recorded program executable")
        self.assertTrue(all(not path.exists() for path in initializers))
        self.assertEqual(user_file.read_bytes(), b"original user CAD")
        self.assertEqual(project.read_bytes(), b"unrelated user project")
        self.assertEqual(json.loads(config_path.read_text())["custom_user_option"], "preserved on upgrade")
        evidence.update(ok=True, user_library_preserved=True, project_preserved=True,
                        configuration_preserved=True, owned_initializers_removed=True,
                        failed_upgrade_restored_previous_active=True)
        (run / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print("Isolated installer evidence:", run / "acceptance.json")


if __name__ == "__main__":
    unittest.main()
