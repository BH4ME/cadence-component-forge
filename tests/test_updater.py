from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jlc2cadence import updater


def installer_bytes() -> bytes:
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:134] = b"PE\x00\x00\x4c\x01"
    return bytes(data)


class UpdateFixture:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.keys = {"test-independent": base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")}
        self.package = installer_bytes()
        self.requests = []
        self.body = self.package
        self.declared_size = len(self.package)
        self.manifest_status = 200
        self.redirect = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(self.path)
                if self.path == "/manifest.json":
                    body = json.dumps(fixture.envelope).encode("utf-8")
                    self.send_response(fixture.manifest_status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.endswith(".exe"):
                    if fixture.redirect:
                        self.send_response(302)
                        self.send_header("Location", fixture.redirect)
                        self.end_headers()
                    else:
                        self.send_response(200)
                        if fixture.declared_size is not None:
                            self.send_header("Content-Length", str(fixture.declared_size))
                        self.end_headers()
                        self.wfile.write(fixture.body)
                        self.close_connection = True
                else:
                    self.send_error(404)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.manifest_url = self.base + "/manifest.json"
        self.manifest = {
            "schema": 1, "product": "jlc2capture", "channel": "stable", "platform": "windows-x64",
            "version": "0.47.0", "artifact": {"filename": "JLC2Capture-Setup-0.47.0.exe",
                "url": self.base + "/packages/0.47.0/JLC2Capture-Setup-0.47.0.exe",
                "size": len(self.package), "sha256": hashlib.sha256(self.package).hexdigest()},
            "notes": "Independent fixture release", "published_at": "2026-11-09T12:00:00Z",
        }
        self.sign()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def sign(self):
        self.envelope = {"manifest": self.manifest, "key_id": "test-independent", "signature": base64.b64encode(
            self.private.sign(updater.canonical_manifest(self.manifest))).decode("ascii")}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = UpdateFixture()
        self.temporary = tempfile.TemporaryDirectory(prefix="jlc-updater-independent-")
        self.root = Path(self.temporary.name)
        self.options = {"allow_loopback_http": True, "timeout": 2}
        if sys.platform != "win32":
            lock = patch.object(updater, "_installer_read_lock", lambda _path: nullcontext())
            lock.start()
            self.addCleanup(lock.stop)

    def tearDown(self):
        self.fixture.close()
        self.temporary.cleanup()

    def check(self, current="0.46"):
        return updater.check_update(self.fixture.manifest_url, current, self.fixture.keys, **self.options)

    def download(self, current="0.46"):
        return updater.download_update(self.fixture.manifest_url, current, self.fixture.keys, self.root / "cache", **self.options)

    def assert_error(self, code, call):
        with self.assertRaises(updater.UpdateError) as error:
            call()
        self.assertEqual(error.exception.error_code, code)

    def test_strict_numeric_versions_and_legacy_current_version(self):
        self.assertGreater(updater.version_tuple("1.10.0"), updater.version_tuple("1.9.99"))
        self.assertEqual(updater.version_tuple("0.46", installed=True), (0, 46, 0))
        for value in ("v1.0.0", "1.0", "01.0.0", "1.0.0-rc1", "1.0.0+build", "1.0.0.1", " 1.0.0", True):
            with self.subTest(value=value):
                self.assert_error("invalid_version", lambda: updater.version_tuple(value))

    def test_real_loopback_check_download_and_cached_reverification(self):
        checked = self.check()
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["update_available"])
        downloaded = self.download()
        target = Path(downloaded["installer_path"])
        self.assertEqual(target.read_bytes(), self.fixture.package)
        self.assertEqual(downloaded["status"], "downloaded")
        self.assertEqual(list(target.parent.glob("*.download")), [])
        count = sum(path.endswith(".exe") for path in self.fixture.requests)
        self.download()
        self.assertEqual(sum(path.endswith(".exe") for path in self.fixture.requests), count)
        target.write_bytes(b"tampered")
        self.download()
        self.assertEqual(target.read_bytes(), self.fixture.package)

    def test_same_version_is_success_and_downgrade_never_downloads(self):
        checked = self.check("0.47.0")
        self.assertEqual(checked["status"], "up_to_date")
        self.assertFalse(checked["update_available"])
        self.assertTrue(self.download("0.47.0")["ok"])
        self.assert_error("downgrade_rejected", lambda: self.download("1.0.0"))
        self.assertFalse(any(path.endswith(".exe") for path in self.fixture.requests))

    def test_confirmation_version_must_match_newly_verified_manifest(self):
        checked = self.check()
        self.fixture.manifest["version"] = "0.48.0"
        self.fixture.sign()
        for command in (updater.download_update, updater.apply_update):
            with self.subTest(command=command.__name__), patch.object(updater.sys, "platform", "win32"), patch.object(
                    updater.subprocess, "Popen") as launch:
                self.assert_error("release_changed", lambda: command(self.fixture.manifest_url, "0.46", self.fixture.keys,
                    self.root / "cache", expected_version=checked["latest_version"], **self.options))
                launch.assert_not_called()
        self.assertFalse(any(path.endswith(".exe") for path in self.fixture.requests))

    def test_tampered_or_wrong_signatures_never_fetch_package(self):
        self.fixture.envelope["manifest"] = {**self.fixture.manifest, "notes": "tampered release notes"}
        self.assert_error("invalid_signature", self.download)
        self.fixture.sign()
        stranger = Ed25519PrivateKey.generate()
        self.fixture.envelope["signature"] = base64.b64encode(stranger.sign(
            updater.canonical_manifest(self.fixture.manifest))).decode("ascii")
        self.assert_error("invalid_signature", self.download)
        self.assertFalse(any(path.endswith(".exe") for path in self.fixture.requests))
        self.assertFalse((self.root / "cache").exists())

    def test_unknown_key_and_manifest_identity_are_rejected(self):
        self.fixture.envelope["key_id"] = "attacker"
        self.assert_error("untrusted_key", self.check)
        for key, value in (("product", "another-product"), ("platform", "windows-arm64"), ("channel", "preview")):
            self.fixture.manifest = {**self.fixture.manifest, "product": "jlc2capture", "platform": "windows-x64", "channel": "stable", key: value}
            self.fixture.sign()
            self.assert_error("incompatible_release", self.check)

    def test_production_requires_https_and_http_redirect_cannot_escape_loopback(self):
        self.assert_error("insecure_url", lambda: updater.check_update(
            self.fixture.manifest_url, "0.46", self.fixture.keys))
        for url in ("http://example.com/manifest.json", "http://127.0.0.1.evil.invalid/manifest.json"):
            with self.subTest(url=url):
                self.assert_error("insecure_url", lambda: updater.validate_url(url, allow_loopback_http=True))
        self.fixture.redirect = "http://example.invalid/payload.exe"
        self.assert_error("insecure_url", self.download)

    def test_signed_path_injection_and_reserved_windows_names_are_rejected(self):
        for name in ("../payload.exe", "C:\\payload.exe", "a/payload.exe", "setup.exe & calc.exe", "CON.exe", "NUL.exe"):
            with self.subTest(name=name):
                self.fixture.manifest["artifact"]["filename"] = name
                self.fixture.sign()
                self.assert_error("invalid_artifact_name", self.download)
        self.assertFalse((self.root / "cache").exists())

    def test_truncated_modified_oversized_downloads_never_replace_cached_file(self):
        initial = Path(self.download()["installer_path"])
        previous = initial.read_bytes()
        self.fixture.manifest["artifact"]["sha256"] = "f" * 64
        self.fixture.sign()
        for body, declared in ((self.fixture.package[:10], len(previous)),
                               (b"x" * len(previous), len(previous)),
                               (self.fixture.package + b"x", None)):
            with self.subTest(size=len(body), declared=declared):
                self.fixture.body, self.fixture.declared_size = body, declared
                self.assert_error("invalid_download", self.download)
                self.assertEqual(initial.read_bytes(), previous)
                self.assertEqual(list(initial.parent.glob("*.download")), [])

    def test_content_length_and_signed_size_must_be_exact(self):
        self.fixture.declared_size = len(self.fixture.package) - 1
        self.assert_error("invalid_download", self.download)
        self.fixture.manifest["artifact"]["size"] = True
        self.fixture.sign()
        self.assert_error("invalid_manifest", self.check)

    def test_duplicate_json_fields_and_nan_are_rejected(self):
        for payload in (b'{"schema":1,"schema":2}', b'{"size":NaN}'):
            self.assert_error("invalid_manifest", lambda: updater._decode_json(payload))

    def test_empty_trust_config_is_explicit_and_does_not_contact_server(self):
        keys = self.root / "public-keys.json"
        keys.write_text('{"schema":1,"keys":{}}', encoding="utf-8")
        self.assert_error("trusted_keys_unavailable", lambda: updater.load_trusted_keys(keys))
        self.assertEqual(self.fixture.requests, [])

    def test_cli_check_returns_json_protocol_and_writes_atomic_response(self):
        keys = self.root / "public-keys.json"
        keys.write_text(json.dumps({"schema": 1, "keys": self.fixture.keys}), encoding="utf-8")
        response = self.root / "reply.json"
        with patch.object(sys, "stdout", new=io.StringIO()) as stdout:
            code = updater.main(["check", "--manifest-url", self.fixture.manifest_url, "--current-version", "0.46",
                                 "--trusted-keys", str(keys), "--allow-loopback-http", "--output", str(response)])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["user_message"])
        self.assertEqual(json.loads(response.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_apply_launches_verified_exact_installer_without_shell_and_reports_started(self):
        process = Mock(pid=1234)
        with patch.object(updater.sys, "platform", "win32"), patch.object(updater.subprocess, "Popen", return_value=process) as launch:
            result = updater.apply_update(self.fixture.manifest_url, "0.46", self.fixture.keys, self.root / "cache", **self.options)
        self.assertEqual(result["status"], "started")
        self.assertTrue(result["restart_capture_required"])
        self.assertFalse(result["installed"])
        self.assertEqual(launch.call_args.args[0], [result["installer_path"], "/SILENT", "/NORESTART"])
        self.assertFalse(launch.call_args.kwargs["shell"])
        process.wait.assert_not_called()

    def test_apply_wait_observes_exit_and_failure_does_not_claim_installation(self):
        for code in (0, 7):
            with self.subTest(code=code), patch.object(updater.sys, "platform", "win32"), patch.object(
                    updater.subprocess, "Popen", return_value=Mock(pid=1234, wait=Mock(return_value=code))):
                result = updater.apply_update(self.fixture.manifest_url, "0.46", self.fixture.keys,
                                              self.root / "cache", wait=True, **self.options)
                self.assertEqual(result["installer_exit_code"], code)
                self.assertEqual(result["ok"], code == 0)
                self.assertFalse(result["installed"])

    def test_apply_wait_timeout_leaves_installer_running(self):
        process = Mock(pid=1234, wait=Mock(side_effect=subprocess.TimeoutExpired("installer", 1)))
        with patch.object(updater.sys, "platform", "win32"), patch.object(updater.subprocess, "Popen", return_value=process):
            result = updater.apply_update(self.fixture.manifest_url, "0.46", self.fixture.keys,
                                          self.root / "cache", wait=True, wait_timeout=1, **self.options)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "installer_running")
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_signed_non_executable_is_never_launched(self):
        self.fixture.body = b"signed data is not a Windows executable"
        self.fixture.manifest["artifact"].update(size=len(self.fixture.body), sha256=hashlib.sha256(self.fixture.body).hexdigest())
        self.fixture.declared_size = len(self.fixture.body)
        self.fixture.sign()
        with patch.object(updater.sys, "platform", "win32"), patch.object(updater.subprocess, "Popen") as launch:
            self.assert_error("invalid_installer", lambda: updater.apply_update(self.fixture.manifest_url, "0.46",
                self.fixture.keys, self.root / "cache", **self.options))
        launch.assert_not_called()

    def test_apply_rechecks_cached_hash_after_download_before_launch(self):
        target = Path(self.download()["installer_path"])

        @contextmanager
        def tamper(_path):
            target.write_bytes(b"x" * len(self.fixture.package))
            yield

        with patch.object(updater.sys, "platform", "win32"), patch.object(updater, "_installer_read_lock", tamper), patch.object(
                updater.subprocess, "Popen") as launch:
            self.assert_error("invalid_download", lambda: updater.apply_update(self.fixture.manifest_url, "0.46",
                self.fixture.keys, self.root / "cache", **self.options))
        launch.assert_not_called()

    def test_windows_real_lock_prevents_write_during_launch_validation(self):
        if sys.platform != "win32":
            self.skipTest("Requires Windows file sharing semantics")
        target = Path(self.download()["installer_path"])
        with updater._installer_read_lock(target):
            with self.assertRaises(PermissionError):
                target.write_bytes(b"tampered")
        self.assertEqual(target.read_bytes(), self.fixture.package)


class ReleasePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("signed_release_test", ROOT / "packaging/release/sign_release.py")
        cls.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.publisher)

    def test_real_signing_publication_server_and_download(self):
        from tools.update_server.server import make_server

        with tempfile.TemporaryDirectory(prefix="jlc-release-independent-") as temporary:
            root = Path(temporary)
            private, public = root / "private/update.pem", root / "public-keys.json"
            installer = root / "JLC2Capture-Setup-0.47.0.exe"
            installer.write_bytes(installer_bytes())
            with patch.dict("os.environ", {"JLC2CAPTURE_SIGNING_KEY_PASSWORD": ""}):
                self.publisher.keygen(private, public, "temporary-test")
                (root / "served").mkdir()
                server = make_server(root / "served", port=0)
                base = f"http://127.0.0.1:{server.server_port}"
                publication = self.publisher.sign_release(installer, "0.47.0", base, private, "temporary-test",
                                                          root / "served", "Temporary fixture", allow_loopback_http=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = updater.download_update(publication["manifest_url"], "0.46.0", updater.load_trusted_keys(public),
                    root / "cache", allow_loopback_http=True, expected_version="0.47.0", timeout=2)
                self.assertEqual(Path(result["installer_path"]).read_bytes(), installer.read_bytes())
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(list((root / "served").rglob("*.pem")), [])
                self.assertTrue(private.is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_publication_refuses_replacement_downgrade_and_private_keys_in_checkout(self):
        with tempfile.TemporaryDirectory(prefix="jlc-release-independent-") as temporary:
            root = Path(temporary)
            private, public = root / "private.pem", root / "public.json"
            installer = root / "setup.exe"
            installer.write_bytes(installer_bytes())
            with patch.dict("os.environ", {"JLC2CAPTURE_SIGNING_KEY_PASSWORD": ""}):
                self.publisher.keygen(private, public, "test")
                self.publisher.sign_release(installer, "0.47.0", "https://updates.example.invalid", private, "test", root / "public", "")
                original = (root / "public/manifest.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "older"):
                    self.publisher.sign_release(installer, "0.46.0", "https://updates.example.invalid", private, "test", root / "public", "")
                installer.write_bytes(installer.read_bytes() + b"different")
                with self.assertRaisesRegex(ValueError, "immutable"):
                    self.publisher.sign_release(installer, "0.47.0", "https://updates.example.invalid", private, "test", root / "public", "")
                self.assertEqual((root / "public/manifest.json").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "outside"):
                self.publisher.keygen(ROOT / "must-not-create-test-private.pem", public, "test")
            self.assertFalse((ROOT / "must-not-create-test-private.pem").exists())


if __name__ == "__main__":
    unittest.main()
