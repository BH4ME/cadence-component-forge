"""Isolated HTTP checks; no real release, key, or persistent server is created."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover - optional outside the packaged runtime
    x509 = hashes = serialization = rsa = NameOID = None

from tools.update_server.server import make_server


class ReleaseHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "public"
        self.root.mkdir()
        self.payload = b"isolated test artifact\x00\x01"
        package = self.root / "packages" / "0.47.0" / "fixture.zip"
        package.parent.mkdir(parents=True)
        package.write_bytes(self.payload)
        self.envelope = {
            "manifest": {
                "schema": 1,
                "product": "jlc2capture",
                "channel": "stable",
                "platform": "windows-x64",
                "version": "0.47.0",
                "artifact": {
                    "filename": "fixture.zip",
                    "url": "http://127.0.0.1/packages/0.47.0/fixture.zip",
                    "size": len(self.payload),
                    "sha256": hashlib.sha256(self.payload).hexdigest(),
                },
                "notes": '<script>alert("fixture")</script>\n第二行',
                "published_at": "2026-11-09T00:00:00Z",
            },
            "key_id": '<img src=x onerror="fixture">',
            "signature": "test-fixture-not-a-real-signature",
        }
        self.write_manifest()
        self.server = make_server(self.root, port=0)
        self.server.RequestHandlerClass.log_message = lambda *_args: None
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def write_manifest(self) -> bytes:
        body = json.dumps(self.envelope, ensure_ascii=False).encode("utf-8")
        (self.root / "manifest.json").write_bytes(body)
        return body

    def request(self, target: str, method: str = "GET", headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_home_uses_real_metadata_and_escapes_notes_and_key(self) -> None:
        status, headers, body = self.request("/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("JLC2Capture 0.47.0", text)
        self.assertIn('/packages/0.47.0/fixture.zip', text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;img src=x", text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('<img src=x', text)
        self.assertIn("第二行", text)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_manifest_is_raw_and_never_cached_or_conditional(self) -> None:
        original = (self.root / "manifest.json").read_bytes()
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = self.request(
                    "/manifest.json?fresh=1", method,
                    {"If-Modified-Since": "Thu, 01 Jan 2099 00:00:00 GMT", "If-None-Match": "anything"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(int(headers["Content-Length"]), len(original))
                self.assertEqual(body, original if method == "GET" else b"")

    def test_package_get_and_head(self) -> None:
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = self.request("/packages/0.47.0/fixture.zip", method)
                self.assertEqual(status, 200)
                self.assertEqual(int(headers["Content-Length"]), len(self.payload))
                self.assertIn("attachment", headers["Content-Disposition"])
                self.assertEqual(body, self.payload if method == "GET" else b"")

    def test_home_head_has_no_body_and_matching_length(self) -> None:
        get_status, get_headers, get_body = self.request("/")
        status, headers, body = self.request("/", "HEAD")
        self.assertEqual(status, get_status)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Content-Length"], get_headers["Content-Length"])
        self.assertEqual(int(headers["Content-Length"]), len(get_body))

    def test_forbids_traversal_and_windows_path_forms(self) -> None:
        (Path(self.temporary.name) / "secret.txt").write_text("outside secret", encoding="utf-8")
        for target in (
            "/../secret.txt", "/%2e%2e/secret.txt", "/packages/%2E%2E/%2e%2e/secret.txt",
            "/%2e%2e%5csecret.txt", "/C:/secret.txt", "/manifest.json::$DATA", "/bad%00name", "/%ZZ",
        ):
            with self.subTest(target=target):
                status, _, body = self.request(target)
                self.assertEqual(status, 403)
                self.assertNotIn(b"outside secret", body)

    def test_directories_never_listed(self) -> None:
        for target in ("/packages", "/packages/", "/packages/0.47.0/"):
            with self.subTest(target=target):
                status, _, body = self.request(target)
                self.assertEqual(status, 404)
                self.assertNotIn(b"fixture.zip", body)

    def test_bad_package_digest_disables_download(self) -> None:
        status, _, _ = self.request("/")
        self.assertEqual(status, 200)
        package = self.root / "packages" / "0.47.0" / "fixture.zip"
        package.write_bytes(b"x" * len(self.payload))
        status, _, body = self.request("/")
        self.assertEqual(status, 503)
        self.assertIn("安装包尚未就绪", body.decode("utf-8"))
        self.assertNotIn('class="download"', body.decode("utf-8"))

    def test_missing_or_invalid_manifest_has_no_invented_version(self) -> None:
        for raw in (b"{}", b"not JSON"):
            (self.root / "manifest.json").write_bytes(raw)
            status, _, body = self.request("/")
            self.assertEqual(status, 503)
            self.assertNotIn(b"0.47.0", body)
        (self.root / "manifest.json").unlink()
        status, headers, _ = self.request("/manifest.json")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_manifest_changes_visible_on_next_request(self) -> None:
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"updated fixture notes", body)
        self.envelope["manifest"]["notes"] = "updated fixture notes"
        self.write_manifest()
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"updated fixture notes", body)

    def test_invalid_unicode_manifest_does_not_break_response(self) -> None:
        self.envelope["manifest"]["notes"] = "\ud800"
        (self.root / "manifest.json").write_text(json.dumps(self.envelope), encoding="utf-8")
        status, _, body = self.request("/")
        self.assertEqual(status, 503)
        self.assertIn("暂无可用发布", body.decode("utf-8"))

    def test_manifest_filename_cannot_escape_public_root(self) -> None:
        self.envelope["manifest"]["artifact"]["filename"] = "../../secret.zip"
        self.write_manifest()
        status, _, body = self.request("/")
        self.assertEqual(status, 503)
        self.assertNotIn('class="download"', body.decode("utf-8"))

    def test_symlink_escape_is_denied_when_supported(self) -> None:
        outside = Path(self.temporary.name) / "secret.txt"
        outside.write_text("outside secret", encoding="utf-8")
        try:
            (self.root / "escape.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is unavailable for this Windows account")
        status, _, body = self.request("/escape.txt")
        self.assertEqual(status, 403)
        self.assertNotIn(b"outside secret", body)

    def test_non_loopback_binding_rejected(self) -> None:
        for host in ("0.0.0.0", "192.0.2.1", "example.com", "::"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    make_server(self.root, host=host, port=0)

    @unittest.skipUnless(x509 is not None, "cryptography is unavailable for the TLS fixture")
    def test_optional_https_transport(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256())
        )
        certificate_path = Path(self.temporary.name) / "fixture-cert.pem"
        key_path = Path(self.temporary.name) / "fixture-key.pem"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        server = make_server(self.root, port=0, certfile=certificate_path, keyfile=key_path)
        self.assertTrue(server.tls_enabled)
        server.RequestHandlerClass.log_message = lambda *_args: None
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            context = ssl._create_unverified_context()
            connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=3)
            try:
                connection.request("GET", "/manifest.json")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body, (self.root / "manifest.json").read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
