from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "packaging/windows/manage.py"
SPEC = importlib.util.spec_from_file_location("windows_manage", SCRIPT)
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jlc-owned-installer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.root = self.folder / "program"
        self.state = self.folder / "persistent"
        self.library = self.folder / "user library"
        self.cadence = {}
        for version in ("17.2", "17.4"):
            root = self.folder / ("Cadence " + version)
            (root / "tools/bin").mkdir(parents=True)
            (root / "tools/bin/capture.exe").write_bytes(b"MZ-fixture-not-launched")
            (root / "tools/bin/allegro.exe").write_bytes(b"MZ-fixture-not-launched")
            (root / "tools/capture/tclscripts/capAutoLoad").mkdir(parents=True)
            self.cadence[version] = root
        self.verify = mock.patch.object(manage, "verify_payload", side_effect=lambda path: {
            "version": "1.0.0", "release_id": path.name})
        self.verify.start()
        self.addCleanup(self.verify.stop)

    def args(self, release="one", **updates):
        values = dict(install_root=str(self.root), state_root=str(self.state), release_id=release,
                      library_root=str(self.library), update_manifest_url="https://example.test/release.json",
                      cadence172=str(self.cadence["17.2"]), cadence174=str(self.cadence["17.4"]),
                      allow_fixture=True)
        return argparse.Namespace(**{**values, **updates})

    def test_dual_registration_preserves_projects_and_library(self):
        self.library.mkdir()
        library_file = self.library / "component.step"
        library_file.write_text("user geometry")
        project = self.cadence["17.4"] / "existing.opj"
        project.write_text("user project")
        manage.install(self.args())
        config = manage.read_json(self.state / "config.json")
        self.assertEqual(set(config["cadence_roots"]), {"17.2", "17.4"})
        self.assertIn("releases", config["python"])
        for root in self.cadence.values():
            initializer = root / "tools/capture/tclscripts/capAutoLoad" / manage.INIT_NAME
            text = initializer.read_text()
            self.assertTrue(text.startswith(manage.owner_line(self.root) + "\n"))
            self.assertIn("JLC2CAPTURE_CADENCE_ROOT", text)
        manage.uninstall(self.args())
        self.assertEqual(library_file.read_text(), "user geometry")
        self.assertEqual(project.read_text(), "user project")
        self.assertTrue((self.state / "config.json").is_file())

    def test_foreign_initializer_is_never_replaced(self):
        foreign = self.cadence["17.4"] / "tools/capture/tclscripts/capAutoLoad" / manage.INIT_NAME
        foreign.write_text("# another installation\n")
        with self.assertRaisesRegex(ValueError, "owned by another installation"):
            manage.install(self.args())
        self.assertEqual(foreign.read_text(), "# another installation\n")
        self.assertFalse((self.root / "active-release.tcl").exists())

    def test_upgrade_preserves_custom_configuration_and_can_rollback(self):
        manage.install(self.args())
        config_path = self.state / "config.json"
        config = manage.read_json(config_path)
        config.update(library_root=str(self.folder / "custom library"), custom_option=42)
        config_path.write_bytes(manage.json_bytes(config))
        original_pointer = (self.root / "active-release.tcl").read_bytes()
        manage.install(self.args("two"))
        upgraded = manage.read_json(config_path)
        self.assertEqual(upgraded["library_root"], config["library_root"])
        self.assertEqual(upgraded["custom_option"], 42)
        self.assertIn("two", upgraded["python"])
        manage.rollback(self.args())
        self.assertEqual((self.root / "active-release.tcl").read_bytes(), original_pointer)
        self.assertEqual(manage.read_json(config_path), config)

    def test_failed_commit_restores_previous_pointer_config_and_registration(self):
        manage.install(self.args())
        watched = [self.root / "active-release.tcl", self.state / "config.json", self.root / "install-state.json"]
        before = {path: path.read_bytes() for path in watched}
        write = manage.atomic_write

        def fail_once(path, content):
            if path == self.root / "install-state.json" and b'"two"' in content:
                raise OSError("simulated disk failure")
            write(path, content)

        with mock.patch.object(manage, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "disk failure"):
                manage.install(self.args("two"))
        self.assertEqual({path: path.read_bytes() for path in watched}, before)

    def test_repair_of_same_release_preserves_original_rollback(self):
        manage.install(self.args())
        manage.install(self.args("two"))
        transaction_path = self.root / "rollback/two/transaction.json"
        transaction = transaction_path.read_bytes()
        manage.install(self.args("two"))
        self.assertEqual(manage.read_json(self.root / "install-state.json")["previous_release_id"], "one")
        self.assertEqual(transaction_path.read_bytes(), transaction)
        manage.rollback(self.args())
        self.assertEqual(manage.read_json(self.root / "install-state.json")["release_id"], "one")

    def test_modified_initializer_survives_uninstall(self):
        manage.install(self.args())
        initializer = self.cadence["17.4"] / "tools/capture/tclscripts/capAutoLoad" / manage.INIT_NAME
        initializer.write_text("# replaced by another owner\n")
        result = manage.uninstall(self.args())
        self.assertEqual(initializer.read_text(), "# replaced by another owner\n")
        self.assertIn(str(initializer), result["preserved_foreign_initializers"])

    def test_upgrade_without_explicit_state_root_preserves_recorded_custom_path(self):
        manage.install(self.args())
        original = manage.read_json(self.state / "config.json")
        manage.install(self.args("two", state_root=str(self.folder / "wrong default"), preserve_state_root=True))
        self.assertFalse((self.folder / "wrong default/config.json").exists())
        current = manage.read_json(self.state / "config.json")
        self.assertEqual(current["library_root"], original["library_root"])
        self.assertEqual(manage.read_json(self.root / "install-state.json")["state_root"], str(self.state))

    def test_explicit_state_relocation_preserves_existing_configuration(self):
        manage.install(self.args())
        path = self.state / "config.json"
        config = manage.read_json(path)
        config["custom_option"] = "keep"
        path.write_bytes(manage.json_bytes(config))
        new_state = self.folder / "relocated state"
        manage.install(self.args("two", state_root=str(new_state)))
        self.assertEqual(manage.read_json(new_state / "config.json")["custom_option"], "keep")
        self.assertEqual(manage.read_json(path), config)

    def test_upgrade_preserves_recorded_cadence_root_when_detection_default_differs(self):
        manage.install(self.args())
        state = manage.read_json(self.root / "install-state.json")
        manage.install(self.args("two", cadence174=str(self.folder / "wrong detected root"), preserve_cadence174=True))
        self.assertEqual(manage.read_json(self.root / "install-state.json")["registrations"], state["registrations"])

    def test_release_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the owned installation"):
            manage.install(self.args("../../outside"))

    def test_root_requires_both_capture_and_allegro_without_launching(self):
        root = self.cadence["17.2"]
        (root / "tools/bin/allegro.exe").unlink()
        with self.assertRaisesRegex(ValueError, "capture.exe, allegro.exe"):
            manage.capture_root(root, "17.2")


if __name__ == "__main__":
    unittest.main()
