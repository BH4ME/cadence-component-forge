from __future__ import annotations

import codecs
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jlc2cadence import library_paths as paths


class AllegroPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library with spaces"
        self.symbols = self.root / "allegro" / "symbols"
        self.symbols.mkdir(parents=True)
        (self.symbols / "original.PSM").write_bytes(b"native psm")
        (self.symbols / "original.DRA").write_bytes(b"native dra")
        self.env = Path(self.temporary.name) / "user" / "pcbenv" / "env"

    def test_preserves_existing_assignments_after_preference_section_and_original_backup(self):
        original = (b"source $TELENV\r\n# custom settings\r\nset psmpath = ( . old_lib )\r\n"
                    b"### User Preferences section\r\nset padpath = ( mypads )\r\n"
                    b"set steppath = ( oldstep )\r\n")
        self.env.parent.mkdir(parents=True)
        self.env.write_bytes(original)
        result = paths.register_allegro_paths(self.root, env_file=self.env)
        new = self.env.read_bytes()
        self.assertTrue(new.startswith(original))
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(result["status"], "registered")
        for name, directory in paths.library_directories(self.root).items():
            self.assertIn(f'set {name} = ( "{directory.as_posix()}" ${name} )'.encode(), new)
        # A later import changes neither the file nor its original recovery copy.
        stamp = self.env.stat().st_mtime_ns
        again = paths.register_allegro_paths(self.root, env_file=self.env)
        self.assertFalse(again["changed"])
        self.assertEqual(self.env.stat().st_mtime_ns, stamp)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)

    def test_switching_library_replaces_only_managed_paths(self):
        original = "source $TELENV\nset psmpath = company_library\n"
        first = paths.merge_allegro_env(original, self.root)
        second_root = Path(self.temporary.name) / "second library"
        second = paths.merge_allegro_env(first, second_root)
        self.assertTrue(second.startswith(original))
        self.assertEqual(second.count(paths.BEGIN), 1)
        self.assertNotIn(self.root.as_posix(), second)
        self.assertIn(second_root.as_posix(), second)

    def test_user_settings_added_after_block_are_preserved_and_precede_new_paths(self):
        first = paths.merge_allegro_env("source $TELENV\n", self.root)
        later = first + "set psmpath = custom_new_path\n"
        merged = paths.merge_allegro_env(later, self.root)
        self.assertLess(merged.index("custom_new_path"), merged.index(paths.BEGIN))
        self.assertEqual(merged.count(paths.BEGIN), 1)

    def test_new_env_retains_default_system_source(self):
        result = paths.register_allegro_paths(self.root, env_file=self.env)
        self.assertTrue(result["changed"])
        self.assertTrue(self.env.read_text().startswith("source $TELENV\n"))

    def test_missing_assets_do_not_create_config(self):
        result = paths.register_allegro_paths(self.root / "missing", env_file=self.env)
        self.assertEqual(result["status"], "missing_assets")
        self.assertFalse(self.env.exists())

    def test_empty_drawing_is_not_registered_as_usable_library(self):
        (self.symbols / "original.DRA").write_bytes(b"")
        result = paths.register_allegro_paths(self.root, env_file=self.env)
        self.assertEqual(result["status"], "missing_assets")
        self.assertFalse(self.env.exists())

    def test_malformed_or_duplicate_managed_block_never_changes_settings(self):
        for text in (paths.BEGIN + "\n", paths.END + "\n", paths.BEGIN + "\n" + paths.BEGIN + "\n" + paths.END):
            with self.subTest(text=text):
                self.env.parent.mkdir(parents=True, exist_ok=True)
                self.env.write_text(text, encoding="utf-8")
                original = self.env.read_bytes()
                with self.assertRaisesRegex(ValueError, "malformed"):
                    paths.register_allegro_paths(self.root, env_file=self.env)
                self.assertEqual(self.env.read_bytes(), original)

    def test_rejects_interpreted_path_characters(self):
        for value in ("bad$HOME", 'bad"path', "bad\npath", "bad\rpath"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                paths.merge_allegro_env("source $TELENV\n", Path(self.temporary.name) / value)

    def test_preserves_bom_and_original_encoding(self):
        self.env.parent.mkdir(parents=True)
        for encoding, bom in (("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
                              ("utf-16-be", codecs.BOM_UTF16_BE)):
            with self.subTest(encoding=encoding):
                original = bom + "source $TELENV\r\n# 元器件\r\n".encode(encoding)
                self.env.write_bytes(original)
                paths.register_allegro_paths(self.root, env_file=self.env)
                self.assertTrue(self.env.read_bytes().startswith(original))

    def test_atomic_replace_failure_retains_original(self):
        self.env.parent.mkdir(parents=True)
        original = b"source $TELENV\n# unchanged\n"
        self.env.write_bytes(original)
        with patch.object(Path, "replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                paths.register_allegro_paths(self.root, env_file=self.env)
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(list(self.env.parent.glob(".jlc2capture-env-*")), [])

    def test_explicit_pcbenv_has_priority_over_home(self):
        home = Path(self.temporary.name) / "home"
        custom = Path(self.temporary.name) / "custom_env"
        custom.mkdir()
        actual = paths.discover_allegro_env(environ={"HOME": str(home), "ALLEGRO_PCBENV": str(custom)})
        self.assertEqual(actual, custom / "env")
        self.assertEqual(paths.discover_allegro_env(environ={"HOME": str(home)}), home / "pcbenv" / "env")

    def test_nonexistent_pcbenv_override_follows_native_home_fallback(self):
        home = Path(self.temporary.name) / "home"
        custom = Path(self.temporary.name) / "not_created"
        actual = paths.discover_allegro_env(environ={"HOME": str(home), "ALLEGRO_PCBENV": str(custom)})
        self.assertEqual(actual, home / "pcbenv" / "env")
        self.assertFalse(custom.exists())

    def test_standard_install_fallback_requires_existing_directory(self):
        cadence = Path(self.temporary.name) / "SPB_17.4"
        self.assertIsNone(paths.discover_allegro_env(cadence, environ={}))
        pcbenv = cadence.parent / "SPB_Data" / "pcbenv"
        pcbenv.mkdir(parents=True)
        self.assertEqual(paths.discover_allegro_env(cadence, environ={}), pcbenv / "env")

    def test_relative_env_override_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            paths.discover_allegro_env(environ={"ALLEGRO_PCBENV": "relative"})

    @unittest.skipIf(os.name == "nt", "Covers conversion-only platforms")
    def test_non_windows_import_does_not_change_home(self):
        with patch.object(paths, "discover_allegro_env") as discovery:
            result = paths.register_allegro_paths(self.root)
        self.assertEqual(result["status"], "unsupported_host")
        discovery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
