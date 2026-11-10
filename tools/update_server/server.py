"""Loopback-only release server for a separately signed JLC2Capture release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
from typing import Any
from urllib.parse import quote, unquote, urlsplit


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "packaging" / "release" / "public"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    size: int
    sha256: str
    notes: str
    published_at: str
    key_id: str
    channel: str
    platform: str

    @property
    def download_path(self) -> str:
        return f"/packages/{quote(self.version, safe='')}/{quote(self.filename, safe='')}"


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid manifest field: {name}")
    value.encode("utf-8")
    return value


def _safe_segment(value: str) -> bool:
    return (
        value not in {"", ".", ".."}
        and not any(character in value for character in '/\\:')
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and not value.endswith((".", " "))
    )


def read_release(root: Path) -> Release:
    """Read metadata only; clients remain responsible for signature verification."""
    manifest_path = (root / "manifest.json").resolve()
    if not manifest_path.is_relative_to(root):
        raise ValueError("Manifest is outside the public root")
    with manifest_path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds the size limit")
    envelope = json.loads(raw)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("manifest"), dict):
        raise ValueError("Invalid manifest envelope")
    manifest = envelope["manifest"]
    if type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise ValueError("Unsupported manifest schema")
    if manifest.get("product") != "jlc2capture":
        raise ValueError("Unexpected release product")
    if manifest.get("channel") != "stable" or manifest.get("platform") != "windows-x64":
        raise ValueError("Unexpected release channel or platform")
    version = _text(manifest.get("version"), "version")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Invalid release version")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Invalid artifact")
    filename = _text(artifact.get("filename"), "artifact.filename")
    if not _safe_segment(filename):
        raise ValueError("Invalid artifact filename")
    _text(artifact.get("url"), "artifact.url")
    size = artifact.get("size")
    if type(size) is not int or size <= 0:
        raise ValueError("Invalid artifact size")
    digest = _text(artifact.get("sha256"), "artifact.sha256")
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("Invalid artifact SHA256")
    notes = manifest.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("Invalid release notes")
    notes.encode("utf-8")
    _text(envelope.get("signature"), "signature")
    return Release(
        version=version,
        filename=filename,
        size=size,
        sha256=digest.lower(),
        notes=notes,
        published_at=_text(manifest.get("published_at"), "published_at"),
        key_id=_text(envelope.get("key_id"), "key_id"),
        channel=manifest["channel"],
        platform=manifest["platform"],
    )


class ReleaseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path,
                 certfile: str | Path | None = None,
                 keyfile: str | Path | None = None):
        self.root = root
        self._verified_artifacts: dict[Path, tuple[int, int, int, str]] = {}
        super().__init__(address, ReleaseHandler)
        if (certfile is None) != (keyfile is None):
            self.server_close()
            raise ValueError("certfile and keyfile must be supplied together")
        self.tls_enabled = certfile is not None
        if self.tls_enabled:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def artifact_ready(self, release: Release) -> bool:
        path = (self.root / "packages" / release.version / release.filename).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        try:
            stat = path.stat()
            if stat.st_size != release.size:
                return False
            identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._verified_artifacts.get(path)
            if cached is not None and cached[:3] == identity:
                return cached[3] == release.sha256
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            self._verified_artifacts[path] = (*identity, actual)
            return actual == release.sha256
        except OSError:
            return False


class ReleaseHandler(BaseHTTPRequestHandler):
    server: ReleaseServer
    server_version = "JLC2CaptureRelease/1"

    def do_GET(self) -> None:
        try:
            self._respond(head_only=False)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self) -> None:
        try:
            self._respond(head_only=True)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _headers(self, status: int, content_type: str, size: int, *, package: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if package else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

    def _error(self, status: int, *, head_only: bool) -> None:
        message = HTTPStatus(status).phrase
        body = f"{status} {message}\n".encode("utf-8")
        self._headers(status, "text/plain; charset=utf-8", len(body))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _request_path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            raise ValueError("Invalid request target")
        if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
            raise ValueError("Invalid percent encoding")
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
        if any(not _safe_segment(segment) for segment in decoded.split("/") if segment):
            raise ValueError("Invalid path segment")
        return decoded

    def _respond(self, *, head_only: bool) -> None:
        try:
            request_path = self._request_path()
        except (ValueError, UnicodeError):
            self._error(HTTPStatus.FORBIDDEN, head_only=head_only)
            return
        if request_path in {"/", "/index.html"}:
            body, status = self._page()
            self._headers(status, "text/html; charset=utf-8", len(body))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return
        try:
            path = (self.server.root / request_path.lstrip("/")).resolve()
            if not path.is_relative_to(self.server.root):
                self._error(HTTPStatus.FORBIDDEN, head_only=head_only)
                return
            if not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, head_only=head_only)
                return
            stream = path.open("rb")
        except (OSError, ValueError):
            self._error(HTTPStatus.NOT_FOUND, head_only=head_only)
            return
        with stream:
            size = os.fstat(stream.fileno()).st_size
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            package = request_path.startswith("/packages/")
            self._headers(HTTPStatus.OK, content_type, size, package=package)
            if package:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name, safe='')}")
            self.end_headers()
            if not head_only:
                shutil.copyfileobj(stream, self.wfile)

    def _page(self) -> tuple[bytes, int]:
        escape = html.escape
        try:
            release = read_release(self.server.root)
            ready = self.server.artifact_ready(release)
            download = (
                f'<a class="download" href="{escape(release.download_path, quote=True)}" download>下载安装包</a>'
                if ready else '<p class="unavailable">安装包尚未就绪</p>'
            )
            content = f"""
<h1>JLC2Capture {escape(release.version)}</h1>
<p class="meta">{escape(release.channel)} · {escape(release.platform)} · {escape(release.published_at)}</p>
{download}
<dl><dt>安装包</dt><dd>{escape(release.filename)}</dd>
<dt>文件大小</dt><dd>{release.size:,} 字节</dd>
<dt>SHA256</dt><dd><code>{escape(release.sha256)}</code></dd>
<dt>签名 key ID</dt><dd><code>{escape(release.key_id)}</code></dd></dl>
<h2>更新说明</h2><pre>{escape(release.notes) if release.notes else '暂无更新说明'}</pre>
<p><a href="/manifest.json">发布清单</a></p>"""
            status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
        except (OSError, ValueError, TypeError, UnicodeError):
            content = "<h1>JLC2Capture</h1><p>暂无可用发布</p>"
            status = HTTPStatus.SERVICE_UNAVAILABLE
        document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>JLC2Capture 下载</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f7;color:#202629;font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif;letter-spacing:0}}
main{{max-width:860px;margin:0 auto;padding:40px 24px}}h1{{font-size:28px;margin:0 0 8px;overflow-wrap:anywhere}}
h2{{font-size:20px;margin:32px 0 12px}}.meta{{color:#586167;margin:0 0 24px}}a{{color:#075b9a}}.download{{display:inline-block;background:#075b9a;color:white;padding:10px 20px;border-radius:4px;text-decoration:none}}
dl{{display:grid;grid-template-columns:110px minmax(0,1fr);gap:10px 16px;margin:28px 0}}dt{{color:#586167}}dd{{margin:0;overflow-wrap:anywhere}}code{{font-size:14px}}pre{{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;border-top:1px solid #cfd5d8;padding-top:16px}}.unavailable{{color:#a13628}}
@media(max-width:520px){{main{{padding:24px 16px}}h1{{font-size:24px}}dl{{grid-template-columns:1fr;gap:4px}}dd{{margin-bottom:10px}}}}
</style></head><body><main>{content}</main></body></html>"""
        return document.encode("utf-8"), status


def make_server(root: str | Path, host: str = "127.0.0.1", port: int = 8766,
                certfile: str | Path | None = None,
                keyfile: str | Path | None = None) -> ReleaseServer:
    resolved_root = Path(root).resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"Public root does not exist: {resolved_root}")
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Host must be a loopback IP address or localhost") from error
    if not address.is_loopback:
        raise ValueError("This server only binds to a loopback address")
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    if address.version == 6:
        class IPv6ReleaseServer(ReleaseServer):
            address_family = socket.AF_INET6
        return IPv6ReleaseServer((host, port), resolved_root, certfile, keyfile)
    return ReleaseServer((host, port), resolved_root, certfile, keyfile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a signed JLC2Capture release on loopback.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--certfile", type=Path, help="Optional TLS certificate PEM; must be paired with --keyfile")
    parser.add_argument("--keyfile", type=Path, help="Optional TLS private-key PEM; must be paired with --certfile")
    arguments = parser.parse_args(argv)
    try:
        server = make_server(arguments.root, arguments.host, arguments.port,
                             arguments.certfile, arguments.keyfile)
    except (ValueError, OSError, ssl.SSLError) as error:
        parser.error(str(error))
    with server:
        print(f"Release root: {server.root}", flush=True)
        display_host = f"[{arguments.host}]" if ":" in arguments.host else arguments.host
        scheme = "https" if server.tls_enabled else "http"
        print(f"Listening on {scheme}://{display_host}:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
