from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "allegro_place_smoke_process", ROOT / "tools/acceptance/allegro_place_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PYTHON = getattr(sys, "_base_executable", sys.executable)


def inherited_output_parent(root: Path) -> tuple[list[str], Path]:
    """A parent exits while its child retains inherited stdout/stderr handles."""
    release = root / "release-child"
    child = root / "child.py"
    child.write_text(
        "from pathlib import Path\nimport os, sys, time\n"
        "release = Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 10\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('child output', flush=True)\n"
        "print('child error', file=sys.stderr, flush=True)\n"
        "os.close(1)\nos.close(2)\n"
        "(release.parent / 'child-finished').write_text('finished')\n", encoding="utf-8")
    parent = root / "parent.py"
    parent.write_text(
        "from pathlib import Path\nimport json, os, subprocess, sys\n"
        "root = Path(sys.argv[1])\n"
        "child = subprocess.Popen([sys.executable, str(root / 'child.py'), str(root / 'release-child')], "
        "stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
        "(root / 'identities.json').write_text(json.dumps({'parent_pid': os.getpid(), 'child_pid': child.pid}))\n"
        "print('parent output', flush=True)\n"
        "print('parent error', file=sys.stderr, flush=True)\n", encoding="utf-8")
    return [PYTHON, str(parent), str(root)], release


class AllegroProcessSupervisionTests(unittest.TestCase):
    def test_inherited_pipe_can_timeout_after_parent_exit(self):
        # This is a real subprocess reproduction of the old capture_output path.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, release = inherited_output_parent(root)
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, creationflags=NO_WINDOW)
            try:
                self.assertEqual(process.wait(timeout=5), 0)
                identities = json.loads((root / "identities.json").read_text())
                self.assertEqual(identities["parent_pid"], process.pid)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.15)
                self.assertEqual(process.poll(), 0)
            finally:
                release.write_text("release")
                stdout, stderr = process.communicate(timeout=5)
            self.assertIn("parent output", stdout)
            self.assertIn("child output", stdout)
            self.assertIn("child error", stderr)

    def test_file_logs_finish_with_parent_despite_live_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, release = inherited_output_parent(root)
            try:
                result = smoke.run_phase(command, phase="probe", root=root,
                                         environment=os.environ.copy(), timeout=3)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["status"], "exited")
                self.assertFalse(result["timed_out"])
                identities = json.loads((root / "identities.json").read_text())
                self.assertEqual(result["pid"], identities["parent_pid"])
                self.assertFalse((root / "child-finished").exists())
                self.assertIn("parent output", Path(result["stdout_file"]).read_text())
                self.assertIn("parent error", Path(result["stderr_file"]).read_text())
                recorded = json.loads((root / "probe.process.json").read_text(encoding="utf-8"))
                self.assertEqual(recorded["pid"], result["pid"])
                self.assertEqual(recorded["exit_code"], 0)
            finally:
                release.write_text("release")
                # Let the short-lived child close its own inherited file handles.
                import time
                deadline = time.monotonic() + 5
                while not (root / "child-finished").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

    def test_live_parent_timeout_is_recorded_and_terminated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [PYTHON, "-u", "-c", "import time; print('before timeout'); time.sleep(10)"]
            result = smoke.run_phase(command, phase="timeout", root=root,
                                     environment=os.environ.copy(), timeout=0.2)
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["status"], "timed_out")
            self.assertTrue(result["alive_at_timeout"])
            self.assertTrue(result["termination_requested"])
            self.assertIsNotNone(result["pid"])
            self.assertIsNotNone(result["exit_code"])
            self.assertIn("before timeout", Path(result["stdout_file"]).read_text())

    def test_native_nonzero_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = smoke.run_phase([PYTHON, "-c", "raise SystemExit(7)"],
                                     phase="failure", root=root, environment=os.environ.copy(), timeout=3)
            self.assertEqual(result["exit_code"], 7)
            self.assertFalse(result["timed_out"])

    def test_launch_failure_is_recorded_without_a_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = smoke.run_phase([str(root / "missing-program.exe")], phase="missing",
                                     root=root, environment=os.environ.copy(), timeout=3)
            self.assertEqual(result["status"], "launch_failed")
            self.assertIsNone(result["pid"])
            self.assertIsNone(result["exit_code"])
            self.assertFalse(result["timed_out"])
            self.assertTrue(result["error"])
            self.assertEqual(json.loads((root / "missing.process.json").read_text(encoding="utf-8"))["status"], "launch_failed")


if __name__ == "__main__":
    unittest.main()
