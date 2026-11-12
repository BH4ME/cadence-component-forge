from __future__ import annotations

import tempfile
import tkinter
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / "capture_plugin/jlc2capture.tcl"


class CapturePlacementPathTests(unittest.TestCase):
    """Emulate a newly compiled OLB registered under Cadence's native key."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tcl = tkinter.Tcl()
        self.tcl.call("source", str(PLUGIN))
        self.tcl.eval("""
            namespace eval ::native {
                variable strings [dict create]
                variable allocated 0
                variable freed 0
                variable states 0
                variable checked 0
                variable fault ""
                variable canonical "NATIVE/REGISTRY/Library.olb"
            }
            proc DboTclHelper_sMakeCString {{value ""}} {
                incr ::native::allocated
                if {$::native::fault eq "allocation" && $::native::allocated == 2} {
                    error allocation_failed
                }
                set pointer _cstring_$::native::allocated
                dict set ::native::strings $pointer $value
                return $pointer
            }
            proc DboTclHelper_sDeleteCString {pointer} {
                if {![dict exists $::native::strings $pointer]} {error invalid_or_double_free}
                dict unset ::native::strings $pointer
                incr ::native::freed
            }
            proc DboLib_sGetNormalizedName {input output flags} {
                set ::native::requested [dict get $::native::strings $input]
                if {$flags ne "0"} {error unexpected_normalization_flags}
                if {$::native::fault eq "normalize"} {error normalization_failed}
                if {$::native::fault eq "null_state"} {return NULL}
                dict set ::native::strings $output $::native::canonical
                if {$::native::fault eq "empty"} {dict set ::native::strings $output ""}
                incr ::native::states
                # A DboState is an object command, never a numeric return code.
                return ::native::state
            }
            proc ::native::state {method} {
                switch -- $method {
                    OK {
                        incr ::native::checked
                        if {$::native::fault eq "state_check"} {error state_check_failed}
                        return [expr {$::native::fault ne "bad_state"}]
                    }
                    -delete {
                        if {$::native::states != 1} {error invalid_or_double_state_free}
                        incr ::native::states -1
                    }
                    default {error "Unexpected DboState method: $method"}
                }
            }
            proc DboTclHelper_sGetConstCharPtr {pointer} {
                if {$::native::checked == 0} {error unchecked_native_state}
                if {$::native::fault eq "read"} {error string_read_failed}
                return [dict get $::native::strings $pointer]
            }
            proc PlaceNew {library symbol flags} {
                if {$library ne $::native::canonical} {error unregistered_library_path}
                set ::placed [list $library $symbol $flags]
            }
        """)

    def assert_released(self):
        self.assertEqual(self.tcl.call("dict", "size", self.tcl.getvar("::native::strings")), 0)
        self.assertEqual(self.tcl.getvar("::native::states"), 0)

    def prepare_import(self, mode):
        xml = self.root / "Library.olb.xml"
        xml.write_bytes(b'<Library alphabeticNumbering="0">source geometry</Library>')
        olb = self.root / "Library.olb"
        response = self.root / "response.json"
        response.write_text('{"ok": true}', encoding="utf-8")
        self.tcl.eval("""
            proc XMATIC_XML2OLB {xml olb args} {
                set stream [open $xml rb]; set source [read $stream]; close $stream
                set stream [open $olb wb]; puts -nonewline $stream $source; close $stream
            }
            proc ::JLC2Capture::validatePublishedAssets {data} {}
            proc ::JLC2Capture::registerFootprintPath {args} {}
            proc ::JLC2Capture::showDetails {args} {}
            proc ::JLC2Capture::showPreviews {args} {}
            proc ::JLC2Capture::setBusy {args} {}
            proc ::JLC2CaptureDock::activateTarget {args} {}
            proc ::JLC2CaptureDock::emit {args} {}
            proc wm {args} {}
        """)
        for namespace in ("::JLC2Capture", "::JLC2CaptureDock"):
            self.tcl.setvar(namespace + "::operationPart", "C1234")
            self.tcl.setvar(namespace + "::operationRoot", self.root.as_posix())
        self.tcl.setvar("::JLC2Capture::placeRequested", 1)
        self.tcl.setvar("::JLC2CaptureDock::operation", "place")
        self.tcl.setvar("::JLC2CaptureDock::operationTarget", "test-page")
        self.tcl.setvar("::JLC2CaptureDock::responseFile", str(response))
        data = self.tcl.call("dict", "create", "part_id", "C1234", "library_root", str(self.root),
                             "olb_path", str(olb), "capture_xml", str(xml), "symbol_name", "SOURCE_PART",
                             "step_status", "available", "conversion_policy", "source_exact")
        command = "::JLC2CaptureDock::finish" if mode == "dock" else "::JLC2Capture::finishImport"
        return command, data, olb, xml

    def test_native_path_is_returned_verbatim_and_resources_are_released(self):
        path = "C:/Parts with spaces/原始符号/Library.olb"
        self.assertEqual(self.tcl.call("::JLC2Capture::placementOlbPath", path),
                         self.tcl.getvar("::native::canonical"))
        self.assertEqual(self.tcl.getvar("::native::requested"), path)
        self.assertEqual(self.tcl.getvar("::native::checked"), 1)
        self.assertEqual(self.tcl.getvar("::native::freed"), 2)
        self.assert_released()

    def test_first_dock_placement_uses_native_library_key(self):
        self.check_first_placement("dock")

    def test_first_tk_placement_uses_native_library_key(self):
        self.check_first_placement("tk")

    def check_first_placement(self, mode):
        command, data, olb, xml = self.prepare_import(mode)
        self.assertFalse(olb.exists())
        source = xml.read_bytes()
        self.tcl.call(command, data)
        self.assertEqual(self.tcl.getvar("::placed"),
                         (self.tcl.getvar("::native::canonical"), "SOURCE_PART", 0))
        self.assertEqual(Path(str(self.tcl.getvar("::native::requested"))), olb)
        self.assertEqual(xml.read_bytes(), source)
        self.assertEqual(olb.read_bytes(), source)
        if mode == "dock":
            stored = self.tcl.getvar("::JLC2CaptureDock::lastImport")
        else:
            key = self.tcl.call("list", self.root.as_posix(), "C1234")
            stored = self.tcl.call("dict", "get", self.tcl.getvar("::JLC2Capture::imported"), key)
        self.assertEqual(Path(str(self.tcl.call("dict", "get", stored, "olb_path"))), olb)
        self.assert_released()

    def test_dock_normalization_failure_prevents_placement(self):
        self.check_failed_placement("dock")

    def test_tk_normalization_failure_prevents_placement(self):
        self.check_failed_placement("tk")

    def check_failed_placement(self, mode):
        command, data, _, _ = self.prepare_import(mode)
        self.tcl.setvar("::native::fault", "bad_state")
        with self.assertRaisesRegex(tkinter.TclError, "OLB path normalization"):
            self.tcl.call(command, data)
        self.assertEqual(self.tcl.call("info", "exists", "::placed"), 0)
        self.assert_released()

    def test_native_failures_release_all_allocated_resources(self):
        for fault, message in (("normalize", "normalization_failed"), ("empty", "OLB path normalization"),
                               ("bad_state", "OLB path normalization"), ("null_state", "OLB path normalization"),
                               ("state_check", "state_check_failed"), ("read", "string_read_failed")):
            with self.subTest(fault=fault):
                self.tcl.setvar("::native::fault", fault)
                with self.assertRaisesRegex(tkinter.TclError, message):
                    self.tcl.call("::JLC2Capture::placementOlbPath", "raw.olb")
                self.assert_released()

    def test_second_allocation_failure_releases_first_string(self):
        self.tcl.setvar("::native::fault", "allocation")
        with self.assertRaisesRegex(tkinter.TclError, "allocation_failed"):
            self.tcl.call("::JLC2Capture::placementOlbPath", "raw.olb")
        self.assertEqual(self.tcl.getvar("::native::freed"), 1)
        self.assert_released()

    def test_missing_native_normalizer_cannot_fall_back_to_raw_path(self):
        self.tcl.call("rename", "DboLib_sGetNormalizedName", "")
        with self.assertRaisesRegex(tkinter.TclError, "DboLib_sGetNormalizedName"):
            self.tcl.call("::JLC2Capture::placementOlbPath", "raw.olb")
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
