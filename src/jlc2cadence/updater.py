"""Verify signed releases and stage Windows installer updates outside the installation."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
from http.client import HTTPException
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PRODUCT = "jlc2capture"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
RELEASE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.exe", re.IGNORECASE)
INSTALLER_ARGUMENTS = ("/SILENT", "/NORESTART")


class UpdateError(RuntimeError):
    def __init__(self, message: str, error_code: str = "update_error") -> None:
        super().__init__(message)
        self.error_code = error_code


def version_tuple(value: str, *, installed: bool = False) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64:
        raise UpdateError("版本号无效，必须采用 MAJOR.MINOR.PATCH。", "invalid_version")
    if installed and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        value += ".0"
    match = RELEASE_VERSION.fullmatch(value)
    if not match:
        raise UpdateError("版本号无效，必须采用 MAJOR.MINOR.PATCH，且不含前缀或预发布标记。", "invalid_version")
    return tuple(int(item) for item in match.groups())


def canonical_manifest(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UpdateError("更新数据包含重复 JSON 字段。", "invalid_manifest")
        value[key] = item
    return value


def _decode_json(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("更新数据不是有效的 UTF-8 JSON。", "invalid_manifest") from exc


def _decode_base64(value: str, size: int, label: str) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError("Expected base64 text")
        result = base64.b64decode(value, validate=True)
        if len(result) != size:
            raise ValueError("Unexpected decoded size")
        return result
    except (ValueError, TypeError) as exc:
        raise UpdateError(f"{label}无效。", "invalid_signature") from exc


def load_trusted_keys(path: Path) -> dict[str, str]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise UpdateError("无法读取已安装程序配置的更新公钥。", "trusted_keys_unavailable") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("更新公钥配置过大。", "invalid_trusted_keys")
    payload = _decode_json(raw)
    if (not isinstance(payload, dict) or type(payload.get("schema")) is not int
            or payload["schema"] != 1 or not isinstance(payload.get("keys"), dict)):
        raise UpdateError("更新公钥配置格式错误。", "invalid_trusted_keys")
    keys = payload["keys"]
    if not keys:
        raise UpdateError("尚未配置受信任的生产更新公钥。", "trusted_keys_unavailable")
    for key_id, value in keys.items():
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            raise UpdateError("更新公钥标识无效。", "invalid_trusted_keys")
        _decode_base64(value, 32, "更新公钥")
    return keys


def validate_url(url: str, *, allow_loopback_http: bool = False) -> str:
    if not isinstance(url, str) or not 1 <= len(url) <= 4096 or any(ord(char) < 33 for char in url):
        raise UpdateError("更新地址无效。", "invalid_url")
    try:
        parsed = urlsplit(url)
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise UpdateError("更新地址无效。", "invalid_url") from exc
    if not hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise UpdateError("更新地址不允许账号信息或片段。", "invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise UpdateError("更新地址端口无效。", "invalid_url")
    loopback = hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_loopback_http and loopback):
        raise UpdateError("生产更新必须使用 HTTPS；HTTP 仅允许显式启用的本机回环测试。", "insecure_url")
    if "\\" in url:
        raise UpdateError("更新地址不能包含反斜杠。", "invalid_url")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def __init__(self, allow_loopback_http: bool):
        super().__init__()
        self.allow_loopback_http = allow_loopback_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, allow_loopback_http=self.allow_loopback_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float, allow_loopback_http: bool):
    validate_url(url, allow_loopback_http=allow_loopback_http)
    opener = build_opener(_SafeRedirect(allow_loopback_http))
    request = Request(url, headers={"User-Agent": "JLC2Capture-Updater/1", "Accept-Encoding": "identity"})
    response = opener.open(request, timeout=timeout)
    try:
        validate_url(response.geturl(), allow_loopback_http=allow_loopback_http)
    except Exception:
        response.close()
        raise
    return response


def _artifact(artifact: Any, *, allow_loopback_http: bool) -> dict[str, Any]:
    if not isinstance(artifact, dict) or set(artifact) != {"filename", "url", "size", "sha256"}:
        raise UpdateError("安装包描述字段无效。", "invalid_manifest")
    name = artifact["filename"]
    if not isinstance(name, str) or not ARTIFACT_NAME.fullmatch(name):
        raise UpdateError("安装包文件名无效或包含路径。", "invalid_artifact_name")
    device = name.split(".", 1)[0].upper()
    if device in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise UpdateError("安装包文件名是 Windows 保留设备名。", "invalid_artifact_name")
    if type(artifact["size"]) is not int or not 0 < artifact["size"] <= MAX_INSTALLER_BYTES:
        raise UpdateError("安装包大小超出允许范围。", "invalid_manifest")
    if not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
        raise UpdateError("安装包 SHA-256 无效。", "invalid_manifest")
    validate_url(artifact["url"], allow_loopback_http=allow_loopback_http)
    if unquote(urlsplit(artifact["url"]).path.rsplit("/", 1)[-1]) != name:
        raise UpdateError("安装包 URL 文件名与已签名文件名不一致。", "invalid_artifact_name")
    return artifact


def verify_manifest(envelope: Any, trusted_keys: dict[str, str], *, allow_loopback_http: bool = False) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"manifest", "key_id", "signature"}:
        raise UpdateError("更新清单格式错误。", "invalid_manifest")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id) or key_id not in trusted_keys:
        raise UpdateError("更新清单使用了不受信任的签名公钥。", "untrusted_key")
    manifest = envelope["manifest"]
    if not isinstance(manifest, dict):
        raise UpdateError("更新清单内容无效。", "invalid_manifest")
    public = _decode_base64(trusted_keys[key_id], 32, "更新公钥")
    signature = _decode_base64(envelope["signature"], 64, "更新签名")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateError("升级验证组件 cryptography 未安装。", "missing_dependency") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical_manifest(manifest))
    except InvalidSignature as exc:
        raise UpdateError("更新清单签名验证失败，已拒绝下载或执行安装包。", "invalid_signature") from exc
    required = {"schema", "product", "channel", "platform", "version", "artifact", "notes", "published_at"}
    if set(manifest) != required or type(manifest["schema"]) is not int or manifest["schema"] != 1:
        raise UpdateError("更新清单版本或字段不受支持。", "invalid_manifest")
    if manifest["product"] != PRODUCT or manifest["channel"] != "stable" or manifest["platform"] != "windows-x64":
        raise UpdateError("该更新与本产品、稳定通道或 Windows x64 平台不匹配。", "incompatible_release")
    version_tuple(manifest["version"])
    _artifact(manifest["artifact"], allow_loopback_http=allow_loopback_http)
    if not isinstance(manifest["notes"], str) or len(manifest["notes"]) > 16 * 1024:
        raise UpdateError("更新说明格式无效。", "invalid_manifest")
    try:
        published = datetime.fromisoformat(manifest["published_at"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("Timezone required")
    except (AttributeError, TypeError, ValueError) as exc:
        raise UpdateError("更新清单发布时间无效。", "invalid_manifest") from exc
    return manifest


def _fetch(manifest_url: str, trusted_keys: dict[str, str], current_version: str,
           timeout: float, allow_loopback_http: bool, expected_version: str | None = None) -> dict[str, Any]:
    installed = version_tuple(current_version, installed=True)
    if expected_version is not None:
        version_tuple(expected_version)
    try:
        with _open(manifest_url, timeout, allow_loopback_http) as response:
            data = response.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise UpdateError("更新清单超过大小限制。", "invalid_manifest")
        manifest = verify_manifest(_decode_json(data), trusted_keys, allow_loopback_http=allow_loopback_http)
    except (HTTPError, URLError, HTTPException, OSError, TimeoutError) as exc:
        raise UpdateError(f"无法连接更新服务器：{exc}", "network_error") from exc
    if expected_version is not None and manifest["version"] != expected_version:
        raise UpdateError("更新服务器发布的版本已经变化，请重新检查并确认更新版本。", "release_changed")
    if version_tuple(manifest["version"]) < installed:
        raise UpdateError("服务器版本低于当前版本，已拒绝降级。", "downgrade_rejected")
    return manifest


def _result(manifest: dict[str, Any], current_version: str) -> dict[str, Any]:
    available = version_tuple(manifest["version"]) > version_tuple(current_version, installed=True)
    return {"ok": True, "status": "update_available" if available else "up_to_date",
            "user_message": "发现新版本，可下载更新。" if available else "当前已是最新版本。",
            "update_available": available, "current_version": current_version,
            "latest_version": manifest["version"], "notes": manifest["notes"],
            "published_at": manifest["published_at"], "artifact": dict(manifest["artifact"])}


def check_update(manifest_url: str, current_version: str, trusted_keys: dict[str, str], *,
                 timeout: float = 20, allow_loopback_http: bool = False) -> dict[str, Any]:
    return _result(_fetch(manifest_url, trusted_keys, current_version, timeout, allow_loopback_http), current_version)


def _verify_package(path: Path, artifact: dict[str, Any]) -> None:
    try:
        if not path.is_file() or path.stat().st_size != artifact["size"]:
            raise UpdateError("安装包大小不符或下载不完整。", "invalid_download")
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(block)
        if checksum.hexdigest() != artifact["sha256"]:
            raise UpdateError("安装包 SHA-256 校验失败，已拒绝执行。", "invalid_download")
    except OSError as exc:
        raise UpdateError(f"安装包无法读取：{exc}", "download_io_error") from exc


def _download(manifest: dict[str, Any], cache_dir: Path, timeout: float, allow_loopback_http: bool) -> Path:
    artifact = manifest["artifact"]
    cache_root = Path(cache_dir).expanduser().resolve()
    directory = cache_root / manifest["version"]
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(cache_root) or (directory / artifact["filename"]).is_symlink():
        raise UpdateError("安装包缓存路径包含不允许的链接。", "invalid_artifact_name")
    target = directory / artifact["filename"]
    if target.parent.resolve() != directory.resolve():
        raise UpdateError("安装包目标路径无效。", "invalid_artifact_name")
    if target.exists():
        try:
            _verify_package(target, artifact)
            return target
        except UpdateError:
            pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".download", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with _open(artifact["url"], timeout, allow_loopback_http) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isdigit() or int(declared) != artifact["size"]):
                    raise UpdateError("服务器安装包大小与签名清单不符。", "invalid_download")
                total, checksum, started = 0, hashlib.sha256(), time.monotonic()
                while True:
                    block = response.read(min(1024 * 1024, artifact["size"] - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > artifact["size"] or time.monotonic() - started > 1200:
                        raise UpdateError("安装包超过签名大小或下载时限。", "invalid_download")
                    output.write(block)
                    checksum.update(block)
                if total != artifact["size"] or checksum.hexdigest() != artifact["sha256"]:
                    raise UpdateError("安装包不完整或 SHA-256 不符，已拒绝执行。", "invalid_download")
                output.flush()
                os.fsync(output.fileno())
        _verify_package(temporary, artifact)
        temporary.replace(target)
    except (HTTPError, URLError, HTTPException, OSError, TimeoutError) as exc:
        raise UpdateError(f"下载安装包失败，当前安装未改变：{exc}", "download_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def download_update(manifest_url: str, current_version: str, trusted_keys: dict[str, str], cache_dir: Path, *,
                    timeout: float = 30, allow_loopback_http: bool = False,
                    expected_version: str | None = None) -> dict[str, Any]:
    manifest = _fetch(manifest_url, trusted_keys, current_version, timeout, allow_loopback_http, expected_version)
    result = _result(manifest, current_version)
    if not result["update_available"]:
        return result
    installer = _download(manifest, cache_dir, timeout, allow_loopback_http)
    return {**result, "status": "downloaded", "user_message": "更新安装包已下载并通过签名、大小和 SHA-256 验证。",
            "installer_path": str(installer), "version": manifest["version"], "sha256": manifest["artifact"]["sha256"]}


@contextmanager
def _installer_read_lock(path: Path) -> Iterator[None]:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise UpdateError("安装包被占用或无法锁定，已停止启动安装程序。", "installer_locked")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def _verify_windows_installer(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise UpdateError("已签名文件不是有效的 Windows 安装程序。", "invalid_installer")
        offset = int.from_bytes(header[60:64], "little")
        if not 64 <= offset <= min(path.stat().st_size - 6, 16 * 1024 * 1024):
            raise UpdateError("Windows 安装程序头部无效。", "invalid_installer")
        stream.seek(offset)
        pe = stream.read(6)
        if pe[:4] != b"PE\x00\x00" or int.from_bytes(pe[4:], "little") not in {0x14C, 0x8664}:
            raise UpdateError("安装程序不是支持的 Windows x86/x64 可执行文件。", "invalid_installer")


def apply_update(manifest_url: str, current_version: str, trusted_keys: dict[str, str], cache_dir: Path, *,
                 timeout: float = 30, allow_loopback_http: bool = False, wait: bool = False,
                 wait_timeout: float = 900, expected_version: str | None = None) -> dict[str, Any]:
    if sys.platform != "win32":
        raise UpdateError("自动安装仅支持 Windows。", "unsupported_platform")
    manifest = _fetch(manifest_url, trusted_keys, current_version, timeout, allow_loopback_http, expected_version)
    result = _result(manifest, current_version)
    if not result["update_available"]:
        return result
    installer = _download(manifest, cache_dir, timeout, allow_loopback_http)
    with _installer_read_lock(installer):
        _verify_package(installer, manifest["artifact"])
        _verify_windows_installer(installer)
        try:
            process = subprocess.Popen([str(installer), *INSTALLER_ARGUMENTS], cwd=str(installer.parent),
                                       shell=False, close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise UpdateError(f"安装包已验证，但安装程序未能启动：{exc}", "installer_start_failed") from exc
    result.update(status="started", user_message="安装程序已启动，请完成安装向导后重新启动 Capture；现有会话不会被强制关闭。",
                  installer_path=str(installer), installer_pid=process.pid, version=manifest["version"],
                  restart_capture_required=True, installed=False)
    if wait:
        try:
            code = process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            return {**result, "ok": False, "status": "installer_running", "error_code": "installer_wait_timeout",
                    "user_message": "等待安装程序超时；程序仍在运行，未强行终止，请查看安装状态。"}
        result.update(installer_exit_code=code, status="installer_finished" if code == 0 else "installer_failed",
                      ok=code == 0, user_message="安装程序已正常结束，请按需重启 Capture 使用新版本。" if code == 0
                      else f"安装程序返回错误码 {code}，请检查安装日志。")
        if code != 0:
            result["error_code"] = "installer_failed"
    return result


def failure_response(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "status": "error", "user_message": str(exc), "error": str(exc),
            "error_code": getattr(exc, "error_code", "update_error")}


def _write_response(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify signed Windows updates; never replace the current installation directly")
    parser.add_argument("command", choices=("check", "download", "apply"))
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--trusted-keys", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--allow-loopback-http", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=900)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if not 0 < args.timeout <= 120 or not 0 < args.wait_timeout <= 3600:
            raise UpdateError("网络或安装等待时限无效。", "invalid_timeout")
        keys = load_trusted_keys(args.trusted_keys)
        options = {"timeout": args.timeout, "allow_loopback_http": args.allow_loopback_http}
        if args.command == "check":
            result = check_update(args.manifest_url, args.current_version, keys, **options)
        else:
            if not args.cache_dir:
                raise UpdateError("下载或安装更新需要指定独立缓存目录。", "cache_dir_required")
            if args.command == "download":
                result = download_update(args.manifest_url, args.current_version, keys, args.cache_dir,
                                         expected_version=args.expected_version, **options)
            else:
                result = apply_update(args.manifest_url, args.current_version, keys, args.cache_dir,
                                      wait=args.wait, wait_timeout=args.wait_timeout,
                                      expected_version=args.expected_version, **options)
    except (UpdateError, OSError, ValueError, TypeError) as exc:
        result = failure_response(exc)
    if args.output:
        try:
            _write_response(args.output, result)
        except OSError as exc:
            result = failure_response(UpdateError(f"无法写入升级响应文件：{exc}", "response_io_error"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
