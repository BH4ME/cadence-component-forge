from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "capture_plugin/jlc2capture.tcl"


class CaptureMenuCompatibilityTests(unittest.TestCase):
    def test_search_and_update_menus_return_capture_update_boolean(self):
        tcl = tkinter.Tcl()
        tcl.eval(
            "set ::actions {}; set ::menus {}; "
            "proc RegisterAction {name predicate shortcut handler context} {"
            "dict set ::actions $name [list $predicate $handler]}; "
            "proc InsertXMLMenu {definition} {lappend ::menus $definition}; "
            "proc RefreshMenu {} {}"
        )
        tcl.call("source", str(PLUGIN))
        menu_items = []
        for definition in tcl.getvar("::menus"):
            item = definition[3]
            if item[0] == "action":
                menu_items.append(item)
        self.assertEqual({item[1] for item in menu_items},
                         {"Search Components...", "Check for Updates..."})
        for item in menu_items:
            with self.subTest(menu=item[1]):
                predicate, handler = tcl.call("dict", "get", tcl.getvar("::actions"), item[4])
                self.assertTrue(tcl.getboolean(tcl.call(predicate)))
                # Capture's menu Update handler uses the literal true/false
                # result, unlike RegisterAction's numeric shouldProcess gate.
                # This follows Cadence's GUI TCL samples 16 and 17.
                self.assertEqual(tcl.call(handler), "true")
                self.assertEqual(tcl.call(handler, "menu-context"), "true")


class ReleaseDockCompatibilityTests(unittest.TestCase):
    """Capability emulation, not a claim of native Capture 17.2 validation."""

    COMMANDS = (
        "CreateControlBar", "LoadResource", "ShowWidget", "GetWidth", "Execute", "DestroyWidget",
        "CreateWidget", "SetTitle", "MDIActivate", "RegisterCloseTclFn", "DeRegisterCloseTclFn",
    )
    CONSTANTS = ("WDOCK_RIGHT", "WDALLOW_RIGHT", "WF_CEF", "WTYPE_VIEW")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tcl = tkinter.Tcl()
        self.tcl.call("source", str(PLUGIN))
        self.tcl.setvar("::JLC2Capture::libraryRoot", self.temporary.name)
        self.tcl.eval(
            "set ::nativeShows 0; set ::widgetCalls {}; set ::dockWidth 780; set ::nextWidget 10; "
            "proc ::JLC2Capture::configure {} {}; "
            "proc ::JLC2Capture::show {args} {incr ::nativeShows}; "
            "rename load realLoad; proc load {args} {lappend ::widgetCalls loader; error unavailable}; "
            "namespace eval ::orPrm {}"
        )

    def complete_cef(self):
        for index, constant in enumerate(self.CONSTANTS, start=1):
            self.tcl.setvar(f"::orPrm::{constant}", index)
        self.tcl.eval(
            "proc ::orPrm::CreateControlBar {args} {lappend ::widgetCalls dock; return [incr ::nextWidget]}; "
            "proc ::orPrm::LoadResource {id path} {lappend ::widgetCalls [list load $id]}; "
            "proc ::orPrm::ShowWidget {id visible} {lappend ::widgetCalls [list show $id $visible]}; "
            "proc ::orPrm::GetWidth {id} {return $::dockWidth}; "
            "proc ::orPrm::Execute {id script} {}; "
            "proc ::orPrm::DestroyWidget {id} {lappend ::widgetCalls [list destroy $id]}; "
            "proc ::orPrm::CreateWidget {args} {lappend ::widgetCalls view; return [incr ::nextWidget]}; "
            "proc ::orPrm::SetTitle {args} {}; "
            "proc ::orPrm::MDIActivate {id} {lappend ::widgetCalls [list activate $id]}; "
            "proc ::orPrm::RegisterCloseTclFn {id callback} {set ::closeCallback $callback}; "
            "proc ::orPrm::DeRegisterCloseTclFn {id} {lappend ::widgetCalls [list unregister $id]}"
        )

    def assert_fallback(self):
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertEqual(self.tcl.getvar("::nativeShows"), 1)
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widget"), 0)

    def test_no_web_extension_uses_native_search(self):
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 0)
        self.assert_fallback()

    def test_legacy_control_bar_without_cef_is_not_accepted(self):
        self.tcl.eval("proc ::orPrm::CreateControlBar {args} {error should_not_create_legacy_browser}")
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 0)
        self.assert_fallback()

    def test_each_missing_cef_method_uses_native_search(self):
        for method in self.COMMANDS:
            with self.subTest(method=method):
                self.complete_cef()
                self.tcl.call("rename", f"::orPrm::{method}", "")
                self.tcl.setvar("::nativeShows", 0)
                self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 0)
                self.assert_fallback()

    def test_each_missing_cef_constant_uses_native_search(self):
        for constant in self.CONSTANTS:
            with self.subTest(constant=constant):
                self.complete_cef()
                self.tcl.call("unset", f"::orPrm::{constant}")
                self.tcl.setvar("::nativeShows", 0)
                self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 0)
                self.assert_fallback()

    def test_explicit_dock_show_also_falls_back_without_cef(self):
        self.tcl.call("::JLC2CaptureDock::show")
        self.assertEqual(self.tcl.getvar("::nativeShows"), 1)
        self.assertIn("CEF", self.tcl.getvar("::JLC2CaptureDock::fallbackReason"))

    def test_package_loader_can_complete_partial_capabilities(self):
        self.complete_cef()
        self.tcl.call("rename", "::orPrm::Execute", "")
        self.tcl.eval("package ifneeded orPrmWebComp 1.0 {"
                      "proc ::orPrm::Execute {args} {}; package provide orPrmWebComp 1.0}")
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 1)
        self.assertEqual(self.tcl.eval("llength $::widgetCalls"), "0")

    def test_native_loader_requires_complete_cef_contract(self):
        self.complete_cef()
        self.tcl.call("rename", "::orPrm::Execute", "")
        self.tcl.eval("proc load {library package} {set ::loaded [list $library $package]; "
                      "proc ::orPrm::Execute {args} {}}")
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 1)
        self.assertEqual(tuple(self.tcl.getvar("::loaded")), ("orPrmWebCompIE64", "orPrmWebComp"))

    def test_complete_cef_opens_dock_without_loading_again(self):
        self.complete_cef()
        self.assertEqual(self.tcl.call("::JLC2CaptureDock::available"), 1)
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertEqual(int(self.tcl.getvar("::nativeShows")), 0)
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widget"), 11)
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widgetType"), "dock")
        self.assertNotIn("loader", tuple(self.tcl.getvar("::widgetCalls")))

    def test_creation_exception_or_zero_handle_uses_native_search(self):
        for body in ("error {CEF runtime unavailable}", "return 0"):
            with self.subTest(body=body):
                self.complete_cef()
                self.tcl.eval(f"proc ::orPrm::CreateControlBar {{args}} {{{body}}}")
                self.tcl.setvar("::nativeShows", 0)
                self.assert_fallback()
                self.assertTrue(self.tcl.getvar("::JLC2CaptureDock::fallbackReason"))

    def test_load_failure_destroys_partial_browser_then_uses_native_search(self):
        self.complete_cef()
        self.tcl.eval("proc ::orPrm::LoadResource {args} {error {CEF resource loading failed}}")
        self.assert_fallback()
        calls = self.tcl.getvar("::widgetCalls")
        self.assertIn(("destroy", 11), calls)

    def test_narrow_dock_stays_in_right_control_bar_by_default(self):
        self.complete_cef()
        self.tcl.setvar("::dockWidth", 125)
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widgetType"), "dock")
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widget"), 11)
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertIn(("show", 11, 1), self.tcl.getvar("::widgetCalls"))
        self.assertEqual(int(self.tcl.getvar("::nativeShows")), 0)

    def test_explicit_mdi_fallback_preserves_legacy_narrow_view(self):
        self.complete_cef()
        self.tcl.setvar("::dockWidth", 125)
        self.tcl.eval("set ::JLC2Capture::configuration [dict create use_mdi_fallback true]")
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widgetType"), "view")
        self.assertEqual(self.tcl.getvar("::JLC2CaptureDock::widget"), 12)
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertIn(("activate", 12), self.tcl.getvar("::widgetCalls"))
        self.assertEqual(int(self.tcl.getvar("::nativeShows")), 0)

    def test_explicit_mdi_view_creation_failure_cleans_dock_and_falls_back(self):
        self.complete_cef()
        self.tcl.setvar("::dockWidth", 125)
        self.tcl.eval("set ::JLC2Capture::configuration [dict create use_mdi_fallback true]")
        self.tcl.eval("proc ::orPrm::CreateWidget {args} {return 0}")
        self.assert_fallback()
        self.assertIn(("destroy", 11), self.tcl.getvar("::widgetCalls"))

    def test_existing_stale_widget_failure_resets_handle_and_uses_native_search(self):
        self.complete_cef()
        self.tcl.setvar("::JLC2CaptureDock::widget", 99)
        self.tcl.eval("proc ::orPrm::ShowWidget {args} {error {Stale CEF widget}}")
        self.assert_fallback()
        self.assertIn(("destroy", 99), self.tcl.getvar("::widgetCalls"))

    def test_fallback_tolerates_failed_native_widget_cleanup(self):
        self.complete_cef()
        self.tcl.setvar("::JLC2CaptureDock::widget", 99)
        self.tcl.eval("proc ::orPrm::ShowWidget {args} {error {CEF show failed}}; "
                      "proc ::orPrm::DeRegisterCloseTclFn {args} {error {Hook unavailable}}; "
                      "proc ::orPrm::DestroyWidget {args} {error {Handle unavailable}}")
        self.assert_fallback()

    def test_configuration_errors_are_not_mislabeled_as_cef_failures(self):
        self.complete_cef()
        self.tcl.eval("set ::shownError {}; proc ::JLC2Capture::configure {} {error {Missing config}}; "
                      "proc ::JLC2Capture::showError {message} {set ::shownError $message}")
        self.tcl.call("::JLC2Capture::openBrowser")
        self.assertEqual(self.tcl.getvar("::shownError"), "Missing config")
        self.assertEqual(int(self.tcl.getvar("::nativeShows")), 0)


if __name__ == "__main__":
    unittest.main()
