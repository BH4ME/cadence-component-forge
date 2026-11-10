"""Transactional registration for the self-contained Windows distribution."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


OWNER = "# jlc2cadence windows installer v1"
INIT_NAME = "jlc2capture_init.tcl"
SUPPORTED = ("17.2", "17.4")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def encoded(path: Path) -> str:
    value = base64.b64encode(str(path.resolve()).replace("\\", "/").encode()).decode()
    return "[encoding convertfrom utf-8 [::base64::decode {" + value + "}]]"


def owner_line(install_root: Path) -> str:
    return OWNER + " " + hashlib.sha256(str(install_root.resolve()).casefold().encode()).hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError(f"Path is outside the owned installation: {path}")
    return resolved


def capture_root(root: Path, version: str, allow_fixture: bool = False) -> dict:
    root = root.resolve()
    capture = root / "tools/bin/capture.exe"
    allegro = root / "tools/bin/allegro.exe"
    autoload = root / "tools/capture/tclscripts/capAutoLoad"
    if not capture.is_file() or not allegro.is_file() or not autoload.is_dir():
        raise ValueError(f"Cadence {version} needs capture.exe, allegro.exe and capAutoLoad: {root}")
    for executable in (capture, allegro):
        with executable.open("rb") as stream:
            if stream.read(2) != b"MZ" and not allow_fixture:
                raise ValueError(f"Not a Windows executable: {executable}")
    observed_version = "fixture" if allow_fixture else file_version(capture)
    match = re.search(r"\b(17\.[24])(?:\b|-)", observed_version)
    if not allow_fixture and (not match or match.group(1) != version):
        raise ValueError(f"Capture executable version {observed_version!r} does not match {version}: {capture}")
    # No application is launched during detection or installation.
    return {"version": version, "file_version": observed_version, "root": str(root),
            "capture": str(capture), "allegro": str(allegro)}


def file_version(executable: Path) -> str:
    if sys.platform != "win32":
        raise ValueError("Windows executable version detection requires Windows")
    from ctypes import wintypes
    api = ctypes.windll.version
    ignored = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(str(executable), ctypes.byref(ignored))
    if not size:
        raise ValueError("Executable has no version resource: " + str(executable))
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(executable), 0, size, data):
        raise ValueError("Cannot read executable version: " + str(executable))
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not api.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        raise ValueError("Executable has no version translation: " + str(executable))
    languages = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
    query = f"\\StringFileInfo\\{languages[0]:04x}{languages[1]:04x}\\FileVersion"
    if not api.VerQueryValueW(data, query, ctypes.byref(pointer), ctypes.byref(length)):
        raise ValueError("Executable has no FileVersion string: " + str(executable))
    return ctypes.wstring_at(pointer, length.value).rstrip("\0")


def verify_payload(release: Path) -> dict:
    manifest = read_json(release / "payload-manifest.json")
    if not manifest or manifest.get("schema") != 1:
        raise ValueError("Missing or unsupported payload manifest")
    for relative, expected in manifest["files"].items():
        path = inside(release / relative, release)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Payload integrity check failed: {relative}")
    python = release / "runtime/python.exe"
    imports = "import ssl, jlc2cadence.capture_bridge, svgelements, easyeda2kicad"
    if "updates" in manifest.get("runtime_features", []):
        imports += "; import cryptography, jlc2cadence.updater"
    if "local-step-preview" in manifest.get("runtime_features", []):
        imports += "; import OCP, jlc2cadence.step_preview"
    check = subprocess.run(
        [str(python), "-B", "-I", "-c", imports],
        capture_output=True, text=True, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check.returncode:
        raise ValueError("Bundled runtime health check failed: " + (check.stderr or check.stdout).strip())
    return manifest


def install(args) -> dict:
    root = Path(args.install_root).resolve()
    state_root = Path(args.state_root).resolve()
    release = inside(root / "releases" / args.release_id, root)
    manifest = verify_payload(release)
    if manifest["release_id"] != args.release_id:
        raise ValueError("Release directory and manifest identity differ")
    installed = read_json(root / "install-state.json", {})
    if installed and installed.get("owner") != owner_line(root):
        raise ValueError("Installation state is owned by another program")
    if installed and getattr(args, "preserve_state_root", False):
        state_root = Path(installed["state_root"]).resolve()
    config_path = state_root / "config.json"
    old_config_path = Path(installed.get("state_root", str(state_root))) / "config.json"
    previous_config = {**read_json(old_config_path, {}), **read_json(config_path, {})}
    if not isinstance(previous_config, dict):
        raise ValueError("Existing configuration must be a JSON object")
    roots = dict(previous_config.get("cadence_roots", {}))
    registrations = dict(installed.get("registrations", {}))
    selected = {}
    for version in SUPPORTED:
        value = getattr(args, "cadence" + version.replace(".", ""), None)
        if installed and getattr(args, "preserve_cadence" + version.replace(".", ""), False) and version in registrations:
            value = roots.get(version) or str(Path(registrations[version]).parents[4])
        if value:
            selected[version] = capture_root(Path(value), version, args.allow_fixture)
            roots[version] = selected[version]["root"]
    if not selected and not registrations:
        raise ValueError("Select at least one Cadence 17.2 or 17.4 installation")
    expected_owner = owner_line(root)
    changes: dict[Path, bytes] = {}
    for version, item in selected.items():
        path = Path(item["root"]) / "tools/capture/tclscripts/capAutoLoad" / INIT_NAME
        if path.exists() and not path.read_text(encoding="utf-8").startswith(expected_owner + "\n"):
            raise ValueError(f"Refusing to replace an initializer owned by another installation: {path}")
        changes[path] = (
            expected_owner + "\n"
            "package require base64\n"
            "set ::env(JLC2CAPTURE_CADENCE_ROOT) " + encoded(Path(item["root"])) + "\n"
            "set ::env(JLC2CAPTURE_CADENCE_VERSION) {" + version + "}\n"
            "if {[catch {source -encoding utf-8 " + encoded(root / "bootstrap.tcl") + "} jlcWindowsError]} {\n"
            '    puts "JLC2Cadence initialization failed: $jlcWindowsError"\n}\n'
        ).encode()
        registrations[version] = str(path)
        old_path = installed.get("registrations", {}).get(version)
        if old_path and Path(old_path) != path:
            previous = Path(old_path)
            if previous.exists():
                raise ValueError("Unregister the previous Cadence root before changing its registration: " + old_path)
    config = {**previous_config, "python": str(release / "runtime/python.exe"),
              "cadence_roots": roots, "install_root": str(root),
              "isolate_host_libraries": previous_config.get("isolate_host_libraries", True),
              "library_root": previous_config.get("library_root") or str(Path(args.library_root).resolve()),
              "cadence_root": previous_config.get("cadence_root") or next(iter(roots.values())),
              "update_manifest_url": previous_config.get("update_manifest_url", args.update_manifest_url),
              "update_trusted_keys": previous_config.get("update_trusted_keys", str(root / "installer/update-public-keys.json")),
              "update_cache_dir": previous_config.get("update_cache_dir", ""),
              "viewer_base_url": previous_config.get("viewer_base_url", "")}
    changes[config_path] = json_bytes(config)
    changes[root / "bootstrap.tcl"] = (
        expected_owner + "\npackage require base64\nsource -encoding utf-8 " + encoded(root / "active-release.tcl") + "\n"
    ).encode()
    changes[root / "active-release.tcl"] = (
        expected_owner + "\npackage require base64\nset ::env(JLC2CAPTURE_CONFIG) " + encoded(config_path) + "\n"
        "set ::env(JLC2CAPTURE_PYTHON) " + encoded(release / "runtime/python.exe") + "\n"
        "set ::env(JLC2CAPTURE_TRUSTED_KEYS) " + encoded(root / "installer/update-public-keys.json") + "\n"
        "source -encoding utf-8 " + encoded(release / "app/capture_plugin/jlc2capture.tcl") + "\n"
    ).encode()
    new_state = {"schema": 1, "owner": expected_owner, "release_id": args.release_id,
                 "version": manifest["version"],
                 "previous_release_id": (installed.get("release_id") if installed.get("release_id") != args.release_id
                                         else installed.get("previous_release_id")),
                 "state_root": str(state_root), "registrations": registrations}
    changes[root / "install-state.json"] = json_bytes(new_state)
    old = {path: path.read_bytes() if path.is_file() else None for path in changes}
    backup = root / "rollback" / args.release_id
    if installed and installed.get("release_id") != args.release_id:
        atomic_write(backup / "transaction.json", json_bytes({
            "owner": expected_owner,
            "files": {str(path): base64.b64encode(value).decode() if value is not None else None
                      for path, value in old.items()},
        }))
    committed = []
    try:
        for path, content in changes.items():
            atomic_write(path, content)
            committed.append(path)
    except BaseException:
        for path in reversed(committed):
            if old[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, old[path])
        raise
    return {"ok": True, **new_state, "config_path": str(config_path)}


def uninstall(args) -> dict:
    root = Path(args.install_root).resolve()
    state = read_json(root / "install-state.json", {})
    if not state:
        return {"ok": True, "removed_initializers": [], "registration_absent": True}
    if state.get("owner") != owner_line(root):
        raise ValueError("Refusing uninstall without this installation's owner state")
    removed, preserved = [], []
    for path_string in state.get("registrations", {}).values():
        path = Path(path_string)
        if path.name != INIT_NAME or path.parent.name != "capAutoLoad":
            raise ValueError("Unexpected initializer path in installation state")
        if path.is_file():
            if path.read_text(encoding="utf-8").startswith(owner_line(root) + "\n"):
                path.unlink()
                removed.append(str(path))
            else:
                preserved.append(str(path))
    # Inno removes its recorded program files; configuration and libraries survive.
    return {"ok": True, "removed_initializers": removed, "preserved_foreign_initializers": preserved,
            "preserved_state_root": state["state_root"]}


def rollback(args) -> dict:
    root = Path(args.install_root).resolve()
    state = read_json(root / "install-state.json", {})
    transaction = read_json(root / "rollback" / state["release_id"] / "transaction.json")
    if not transaction or transaction.get("owner") != owner_line(root):
        raise ValueError("No rollback transaction belongs to this installation")
    allowed = {root / "bootstrap.tcl", root / "active-release.tcl", root / "install-state.json",
               Path(state["state_root"]) / "config.json"}
    allowed.update(Path(value) for value in state.get("registrations", {}).values())
    for path_string, value in transaction["files"].items():
        path = Path(path_string)
        if path not in allowed:
            raise ValueError("Rollback contains a path outside the recorded transaction")
        if value is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(value, validate=True))
    return {"ok": True, "release_id": read_json(root / "install-state.json")["release_id"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--install-root", required=True)
    install_parser.add_argument("--release-id", required=True)
    install_parser.add_argument("--state-root", required=True)
    install_parser.add_argument("--library-root", required=True)
    install_parser.add_argument("--update-manifest-url", default="")
    for version in SUPPORTED:
        install_parser.add_argument("--cadence" + version.replace(".", ""))
        install_parser.add_argument("--preserve-cadence" + version.replace(".", ""), action="store_true")
    install_parser.add_argument("--allow-fixture", action="store_true", help=argparse.SUPPRESS)
    install_parser.add_argument("--output")
    install_parser.add_argument("--preserve-state-root", action="store_true")
    for command in ("uninstall", "rollback"):
        commands.add_parser(command).add_argument("--install-root", required=True)
    probe_parser = commands.add_parser("probe")
    probe_parser.add_argument("--root", required=True)
    probe_parser.add_argument("--version", choices=SUPPORTED, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            result = {"ok": True, **capture_root(Path(args.root), args.version)}
        else:
            result = globals()[args.command](args)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
        if getattr(args, "output", None):
            atomic_write(Path(args.output), json_bytes(result))
        print(json.dumps(result, ensure_ascii=True))
        return 1
    if getattr(args, "output", None):
        atomic_write(Path(args.output), json_bytes(result))
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
