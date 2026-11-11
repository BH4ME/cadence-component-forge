from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time
import tkinter
import unittest


PLUGIN = Path(__file__).resolve().parents[1] / "capture_plugin/jlc2capture.tcl"


class CaptureWorkerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tcl = tkinter.Tcl()
        self.tcl.call("source", str(PLUGIN))
        self.tcl.setvar("::JLC2Capture::libraryRoot", str(self.root))

    def test_latest_selection_replaces_queue_and_preserves_request_id(self):
        self.tcl.setvar("::JLC2CaptureDock::channel", "active-worker")
        self.tcl.setvar("::JLC2CaptureDock::operation", "preview3d")
        for part, request_id in (("C1234", 7), ("C5678", 8)):
            result = self.tcl.call("::JLC2CaptureDock::start", "preview",
                                   ("--part", part, "--prepare-only", "--defer-step"), request_id)
            self.assertEqual(result, "queued")
        queued = self.tcl.getvar("::JLC2CaptureDock::queuedRequest")
        self.assertEqual(queued[1][1], "C5678")
        self.assertEqual(queued[2], 8)
        self.tcl.eval("rename ::JLC2CaptureDock::start ::JLC2CaptureDock::realStart; "
                      "proc ::JLC2CaptureDock::start {args} {set ::dispatched $args}")
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::startQueued"), 1)
        self.assertEqual(self.tcl.getvar("::dispatched"), queued)
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::startQueued"), 0)

    def test_import_and_place_cannot_be_replaced_or_queued(self):
        self.tcl.setvar("::JLC2CaptureDock::channel", "active-worker")
        for active in ("import", "place"):
            self.tcl.setvar("::JLC2CaptureDock::operation", active)
            self.assertEqual(self.tcl.call("::JLC2CaptureDock::start", "preview",
                                          ("--part", "C1234"), 2), "busy")
        self.tcl.setvar("::JLC2CaptureDock::operation", "preview")
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::start", "import",
                                      ("--part", "C1234"), 2), "busy")
        self.assertEqual(str(self.tcl.getvar("::JLC2CaptureDock::queuedRequest")), "")

    def test_three_d_preview_never_builds_capture_library(self):
        response = self.root / "response.json"
        response.write_text('{"ok": true}', encoding="utf-8")
        for name, value in {"operation": "preview3d", "operationPart": "C1234",
                            "operationRoot": self.root.as_posix(), "operationId": 3,
                            "responseFile": str(response)}.items():
            self.tcl.setvar("::JLC2CaptureDock::" + name, value)
        self.tcl.eval("proc ::JLC2Capture::buildOlb {data} {error must_not_build}; "
                      "proc ::JLC2CaptureDock::emit {args} {set ::emitted $args}")
        data = self.tcl.call("dict", "create", "part_id", "C1234", "library_root",
                             str(self.root), "conversion_policy", "source_exact")
        self.tcl.call("::JLC2CaptureDock::finish", data)
        emitted = self.tcl.getvar("::emitted")
        self.assertEqual(emitted[0], "preview3d")
        self.assertEqual(emitted[2], 3)

    def test_capture_path_uses_native_append_api_and_checks_imported_files(self):
        directory = self.root / "allegro/symbols"
        directory.mkdir(parents=True)
        self.tcl.eval("set ::registered {}; proc capSetFootPrintPathToCaptureINI {directory} "
                      "{lappend ::registered $directory}")
        self.assertEqual(self.tcl.call("::JLC2Capture::registerFootprintPath"), 0)
        (directory / "test.dra").write_bytes(b"native-dra")
        with self.assertRaisesRegex(tkinter.TclError, "missing"):
            self.tcl.call("::JLC2Capture::registerFootprintPath", str(self.root), "TEST")
        (directory / "test.psm").write_bytes(b"native-psm")
        self.assertEqual(self.tcl.call("::JLC2Capture::registerFootprintPath", str(self.root), "TEST"), 1)
        self.assertEqual(tuple(map(str, self.tcl.getvar("::registered"))), (directory.as_posix(),))
        with self.assertRaisesRegex(tkinter.TclError, "Invalid footprint"):
            self.tcl.call("::JLC2Capture::registerFootprintPath", str(self.root), "../test")

    def test_only_plugin_created_tk_root_is_hidden(self):
        self.tcl.eval("package provide Tk 8.6; set ::withdrawn {}; "
                      "proc wm {args} {lappend ::withdrawn $args}")
        self.tcl.call("::JLC2Capture::requireTk")
        self.assertEqual(self.tcl.getvar("::withdrawn"), (("withdraw", "."),))
        self.tcl.eval("set ::withdrawn {}; proc winfo {args} {return 1}")
        self.tcl.call("::JLC2Capture::requireTk")
        self.assertEqual(self.tcl.getvar("::withdrawn"), "")

    def test_bare_python_is_resolved_to_windowless_runtime(self):
        runtime = self.root / "runtime with spaces"
        runtime.mkdir()
        (runtime / "python.exe").write_bytes(b"console")
        (runtime / "pythonw.exe").write_bytes(b"gui")
        self.tcl.setvar("::tcl_platform(platform)", "windows")
        self.tcl.setvar("::JLC2Capture::python", "python")
        self.tcl.setvar("::resolvedPython", str(runtime / "python.exe"))
        self.tcl.eval("proc auto_execok {name} {return [list $::resolvedPython]}")
        self.assertEqual(str(self.tcl.call("::JLC2Capture::workerPython")),
                         (runtime / "pythonw.exe").as_posix())
        (runtime / "pythonw.exe").unlink()
        with self.assertRaisesRegex(tkinter.TclError, "windowless runtime"):
            self.tcl.call("::JLC2Capture::workerPython")

    def test_background_registration_is_separate_and_reused_after_success(self):
        symbols = self.root / "allegro/symbols"
        symbols.mkdir(parents=True)
        (symbols / "test.dra").write_bytes(b"dra")
        package = self.root / "src/jlc2cadence"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "capture_bridge.py").write_text(
            "import sys,json\nfrom pathlib import Path\n"
            "output=Path(sys.argv[sys.argv.index('--output')+1])\n"
            "output.write_text(json.dumps({'ok':True,'arguments':sys.argv[1:],"
            "'path_registration':{'status':'registered'}}),encoding='utf-8')\n", encoding="utf-8")
        self.tcl.setvar("::JLC2Capture::projectRoot", str(self.root))
        self.tcl.setvar("::JLC2Capture::python", sys.executable)
        self.tcl.setvar("::JLC2CaptureDock::channel", "independent-import-worker")
        self.tcl.eval("proc ::JLC2Capture::readJson {path} {"
                      "if {![file isfile $path]} {error missing_response}; "
                      "return [dict create ok 1 path_registration [dict create status registered]]}")
        self.tcl.call("::JLC2Capture::registerLibraryPaths")
        deadline = time.monotonic() + 10
        while str(self.tcl.getvar("::JLC2Capture::pathChannel")) and time.monotonic() < deadline:
            self.tcl.eval("update")
            time.sleep(.01)
        self.assertEqual(str(self.tcl.getvar("::JLC2Capture::pathChannel")), "")
        self.assertEqual(str(self.tcl.getvar("::JLC2Capture::pathWarning")), "")
        response = Path(str(self.tcl.getvar("::JLC2Capture::pathResponse")))
        data = json.loads(response.read_text(encoding="utf-8"))
        self.assertEqual(data["arguments"][0], "register-paths")
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::channel"), "independent-import-worker")
        self.tcl.call("::JLC2Capture::registerLibraryPaths")
        self.assertEqual(str(self.tcl.getvar("::JLC2Capture::pathChannel")), "")
        self.assertEqual(Path(str(self.tcl.getvar("::JLC2Capture::pathResponse"))), response)

    def test_empty_published_pad_is_rejected_before_placement(self):
        pad = self.root / "test.pad"
        pad.write_bytes(b"")
        data = self.tcl.call("dict", "create", "published_files", (str(pad),))
        with self.assertRaisesRegex(tkinter.TclError, "missing or empty"):
            self.tcl.call("::JLC2Capture::validatePublishedAssets", data)
        pad.write_bytes(b"native-pad")
        self.tcl.call("::JLC2Capture::validatePublishedAssets", data)


if __name__ == "__main__":
    unittest.main()
