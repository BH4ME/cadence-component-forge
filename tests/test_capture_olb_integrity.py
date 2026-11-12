from __future__ import annotations

import tempfile
import tkinter
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / "capture_plugin/jlc2capture.tcl"


class CaptureOlbIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.xml = self.root / "library.olb.xml"
        self.olb = self.root / "library.olb"
        self.xml.write_bytes(b"<Library>original</Library>")
        self.tcl = tkinter.Tcl()
        self.tcl.call("source", str(PLUGIN))
        self.tcl.eval("""
            set ::conversions 0
            proc XMATIC_XML2OLB {xml olb args} {
                incr ::conversions
                set stream [open $xml rb]; set source [read $stream]; close $stream
                set stream [open $olb wb]
                puts -nonewline $stream "native library: $source"
                close $stream
            }
        """)
        self.data = self.tcl.call("dict", "create", "olb_path", str(self.olb), "capture_xml", str(self.xml))

    def build(self):
        return Path(str(self.tcl.call("::JLC2Capture::buildOlb", self.data)))

    def test_unchanged_xml_and_native_bytes_reuse_verified_library(self):
        first = self.build()
        self.assertEqual(first, self.olb)
        self.assertEqual(self.build(), first)
        self.assertEqual(self.tcl.getvar("::conversions"), 1)

    def test_nonempty_corrupted_olb_is_rebuilt_at_new_path_without_overwrite(self):
        original = self.build()
        corrupted = b"X" * original.stat().st_size
        original.write_bytes(corrupted)
        rebuilt = self.build()
        self.assertNotEqual(rebuilt, original)
        self.assertEqual(original.read_bytes(), corrupted)
        self.assertIn(self.xml.read_bytes(), rebuilt.read_bytes())
        self.assertEqual(self.build(), rebuilt)
        self.assertEqual(self.tcl.getvar("::conversions"), 2)

    def test_changed_xml_never_reuses_or_overwrites_previously_loaded_olb(self):
        original = self.build()
        original_bytes = original.read_bytes()
        self.xml.write_bytes(b"<Library>restored source</Library>")
        rebuilt = self.build()
        self.assertNotEqual(rebuilt, original)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertIn(self.xml.read_bytes(), rebuilt.read_bytes())
        self.assertEqual(self.build(), rebuilt)

    def test_legacy_unverified_library_is_preserved_and_regenerated(self):
        self.olb.write_bytes(b"nonempty unverified old library")
        rebuilt = self.build()
        self.assertNotEqual(rebuilt, self.olb)
        self.assertEqual(self.olb.read_bytes(), b"nonempty unverified old library")
        self.assertEqual(self.tcl.getvar("::conversions"), 1)

    def test_failed_native_conversion_cannot_mark_corrupt_library_as_verified(self):
        self.olb.write_bytes(b"corrupt library")
        self.tcl.eval("proc XMATIC_XML2OLB {args} {error native_conversion_failed}")
        with self.assertRaisesRegex(tkinter.TclError, "native_conversion_failed"):
            self.build()
        self.assertEqual(self.olb.read_bytes(), b"corrupt library")

    def test_corrupted_sidecar_is_data_and_recovers_without_executing_text(self):
        original = self.build()
        original_bytes = original.read_bytes()
        Path(str(self.olb) + ".integrity").write_text("[set ::injected 1] broken", encoding="utf-8")
        rebuilt = self.build()
        self.assertNotEqual(rebuilt, original)
        self.assertEqual(self.tcl.call("info", "exists", "::injected"), 0)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(self.build(), rebuilt)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_sidecar_cannot_read_an_external_library(self):
        self.build()
        fingerprint = self.tcl.call("::JLC2Capture::nativeFileFingerprint", str(self.xml))
        malicious = self.tcl.call("dict", "create", "format", 1, "file", "../private.olb", "xml", fingerprint,
                                  "olb", (1, "00000000"))
        self.tcl.call("set", "::maliciousProof", malicious)
        self.tcl.setvar("::metadataPath", str(self.olb) + ".integrity")
        self.tcl.eval("set s [open $::metadataPath w]; puts -nonewline $s $::maliciousProof; close $s; "
                      "rename ::JLC2Capture::nativeFileFingerprint ::JLC2Capture::realFingerprint; "
                      "proc ::JLC2Capture::nativeFileFingerprint {path} {"
                      "if {[file tail $path] eq {private.olb}} {set ::externalRead 1; error external_read}; "
                      "return [realFingerprint $path]}")
        rebuilt = self.build()
        self.assertNotEqual(rebuilt, self.olb)
        self.assertEqual(self.tcl.call("info", "exists", "::externalRead"), 0)

    def test_dock_placement_and_state_use_actual_rebuilt_library_path(self):
        response = self.root / "response.json"
        response.write_text('{"ok": true}', encoding="utf-8")
        replacement = self.root / "library.rebuilt.olb"
        self.tcl.setvar("::rebuiltOlb", str(replacement))
        self.tcl.eval("""
            proc ::JLC2Capture::buildOlb {data} {return $::rebuiltOlb}
            proc ::JLC2Capture::placementOlbPath {path} {return "$path.native"}
            proc ::JLC2Capture::validatePublishedAssets {data} {}
            proc ::JLC2Capture::registerFootprintPath {args} {}
            proc ::JLC2CaptureDock::activateTarget {target} {}
            proc ::JLC2CaptureDock::emit {args} {}
            proc PlaceNew {args} {set ::placed $args}
        """)
        for name, value in {"operation": "place", "operationPart": "C1234", "operationRoot": self.root.as_posix(),
                            "responseFile": str(response), "operationTarget": "test"}.items():
            self.tcl.setvar("::JLC2CaptureDock::" + name, value)
        data = self.tcl.call("dict", "create", "olb_path", str(self.olb), "symbol_name", "PART", "part_id", "C1234",
                             "library_root", str(self.root), "conversion_policy", "source_exact")
        self.tcl.call("::JLC2CaptureDock::finish", data)
        self.assertEqual(str(self.tcl.getvar("::placed")[0]), str(replacement) + ".native")
        self.assertEqual(Path(str(self.tcl.call("dict", "get", self.tcl.getvar("::JLC2CaptureDock::lastImport"), "olb_path"))), replacement)

    def test_tk_import_always_verifies_backend_assets_even_with_in_memory_cache(self):
        self.tcl.setvar("::JLC2Capture::libraryRoot", str(self.root))
        self.tcl.setvar("::JLC2Capture::selected", "C1234")
        self.tcl.eval("""
            proc ::JLC2Capture::cachedImport {key} {return [dict create step_status available]}
            proc ::JLC2Capture::finishImport {data} {set ::bypassedVerification 1}
            proc ::JLC2Capture::start {args} {set ::started $args}
        """)
        self.tcl.call("::JLC2Capture::importPart", 0)
        self.assertEqual(self.tcl.call("info", "exists", "::bypassedVerification"), 0)
        self.assertEqual(self.tcl.getvar("::started"), ("import", ("--part", "C1234")))


if __name__ == "__main__":
    unittest.main()
