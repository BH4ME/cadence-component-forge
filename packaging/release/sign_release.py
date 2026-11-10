"""Create Ed25519 release metadata; private signing keys stay outside the checkout."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jlc2cadence.updater import (
    KEY_ID, MAX_INSTALLER_BYTES, UpdateError, _verify_windows_installer,
    canonical_manifest, validate_url, verify_manifest, version_tuple,
)


def private_key_location(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Private keys must remain outside the source checkout and distribution directories")
    return path


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def keygen(private_path: Path, public_path: Path, key_id: str) -> dict:
    private_path = private_key_location(private_path)
    public_path = public_path.expanduser().resolve()
    if not KEY_ID.fullmatch(key_id) or private_path.exists() or public_path.exists():
        raise ValueError("Key ID must be valid and both output files must be new")
    key = Ed25519PrivateKey.generate()
    password = os.environ.get("JLC2CAPTURE_SIGNING_KEY_PASSWORD", "").encode("utf-8")
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    private_data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(private_data)
    encoded = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
    _atomic_json(public_path, {"schema": 1, "keys": {key_id: encoded}})
    return {"ok": True, "user_message": "公钥已生成；私钥保留在独立签名环境，请勿分发。",
            "public_keys_file": str(public_path), "key_id": key_id,
            "private_key_encrypted": bool(password)}


def _sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def sign_release(installer: Path, version: str, base_url: str, private_path: Path, key_id: str,
                 output_dir: Path, notes: str, *, allow_loopback_http: bool = False) -> dict:
    version_tuple(version)
    validate_url(base_url, allow_loopback_http=allow_loopback_http)
    if urlsplit(base_url).query or not KEY_ID.fullmatch(key_id):
        raise ValueError("Base URL must not have a query and the key ID must be valid")
    private_path = private_key_location(private_path)
    output_dir = output_dir.expanduser().resolve()
    if private_path.is_relative_to(output_dir):
        raise ValueError("Private keys cannot be inside the public release root")
    installer = installer.expanduser().resolve()
    _verify_windows_installer(installer)
    size = installer.stat().st_size
    if not 0 < size <= MAX_INSTALLER_BYTES:
        raise ValueError("Installer size exceeds the configured update limit")
    password = os.environ.get("JLC2CAPTURE_SIGNING_KEY_PASSWORD", "").encode("utf-8") or None
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The release signing key must be Ed25519")
    digest = _sha256(installer)
    manifest = {"schema": 1, "product": "jlc2capture", "channel": "stable", "platform": "windows-x64",
                "version": version, "artifact": {"filename": installer.name,
                    "url": base_url.rstrip("/") + f"/packages/{version}/{installer.name}",
                    "size": size, "sha256": digest}, "notes": notes,
                "published_at": datetime.now(timezone.utc).isoformat()}
    envelope = {"manifest": manifest, "key_id": key_id,
                "signature": base64.b64encode(key.sign(canonical_manifest(manifest))).decode("ascii")}
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
    verify_manifest(envelope, {key_id: public}, allow_loopback_http=allow_loopback_http)
    latest = output_dir / "manifest.json"
    if latest.is_file():
        previous = json.loads(latest.read_text(encoding="utf-8"))["manifest"]["version"]
        if version_tuple(previous) > version_tuple(version):
            raise ValueError("Refusing to publish a version older than the current release")
    package_dir = output_dir / "packages" / version
    package_dir.mkdir(parents=True, exist_ok=True)
    if not package_dir.resolve().is_relative_to(output_dir):
        raise ValueError("Release package directory cannot escape the public root")
    target = package_dir / installer.name
    if target.exists():
        if target.is_symlink() or target.stat().st_size != size or _sha256(target) != digest:
            raise ValueError("An immutable release artifact already exists with different bytes")
    else:
        descriptor, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=package_dir)
        temporary = Path(name)
        os.close(descriptor)
        try:
            shutil.copyfile(installer, temporary)
            if temporary.stat().st_size != size or _sha256(temporary) != digest:
                raise ValueError("Installer changed during release publication")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    _atomic_json(latest, envelope)
    return {"ok": True, "user_message": "安装包和已签名清单已写入发布目录。",
            "version": version, "manifest": str(latest), "package": str(target), "sha256": digest,
            "manifest_url": base_url.rstrip("/") + "/manifest.json"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate independent signing keys or publish a signed installer release")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("keygen")
    generate.add_argument("--private-key", type=Path, required=True)
    generate.add_argument("--public-keys", type=Path, required=True)
    generate.add_argument("--key-id", required=True)
    sign = subparsers.add_parser("sign")
    sign.add_argument("--installer", type=Path, required=True)
    sign.add_argument("--version", required=True)
    sign.add_argument("--base-url", required=True)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--output-dir", type=Path, required=True)
    sign.add_argument("--notes-file", type=Path)
    sign.add_argument("--allow-loopback-http", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            result = keygen(args.private_key, args.public_keys, args.key_id)
        else:
            notes = args.notes_file.read_text(encoding="utf-8-sig") if args.notes_file else ""
            result = sign_release(args.installer, args.version, args.base_url, args.private_key, args.key_id,
                                  args.output_dir, notes, allow_loopback_http=args.allow_loopback_http)
    except (OSError, ValueError, KeyError, TypeError, UpdateError) as exc:
        result = {"ok": False, "user_message": str(exc), "error": str(exc)}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
