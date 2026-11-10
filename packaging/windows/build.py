"""Build a redistributable payload without bundling any Cadence binary."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
PYTHON_VERSION = "3.11.9"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
WEB_ASSETS = {
    "lucide": ["dist/umd/lucide.min.js", "LICENSE"],
    "three": ["build/three.min.js", "examples/js/loaders/GLTFLoader.js",
              "examples/js/controls/OrbitControls.js", "examples/js/renderers/Projector.js",
              "examples/js/renderers/SVGRenderer.js", "LICENSE"],
}
OCCT_NOTICES = {
    "OCCT-7.8.1-LICENSE_LGPL_21.txt": ("LICENSE_LGPL_21.txt", "e237fa56668030e928551ddd60f05df5fe957f75eab874bbd017e085ed722e7c"),
    "OCCT-7.8.1-LGPL_EXCEPTION.txt": ("OCCT_LGPL_EXCEPTION.txt", "04580a884ea6cea294402649ff7b5cbb167d47462d1340a4ed33e550db10a81b"),
}


def fetch(url: str, path: Path) -> None:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=90) as response, path.open("wb") as stream:
            shutil.copyfileobj(response, stream)


def copy_tree(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_file() and "__pycache__" not in relative.parts and " - " not in path.name and path.suffix == ".py":
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def build(destination: Path) -> dict:
    if destination.exists():
        raise ValueError("Build destination must be new: " + str(destination))
    cache = HERE / "tools/downloads"
    runtime_zip = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    fetch(PYTHON_URL, runtime_zip)
    if hashlib.sha256(runtime_zip.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise ValueError("Official Python archive checksum differs from the pinned release")
    stage = destination / "payload"
    runtime = stage / "runtime"
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(runtime_zip) as archive:
        archive.extractall(runtime)
    (runtime / "python311._pth").write_text(
        "python311.zip\n.\nLib/site-packages\n../app/src\nimport site\n", encoding="ascii")
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
                    "--only-binary=:all:", "--python-version", "3.11", "--platform", "win_amd64",
                    "--implementation", "cp", "--abi", "cp311",
                    "--target", str(runtime / "Lib/site-packages"),
                    "easyeda2kicad==1.0.1", "svgelements==1.9.6", "cryptography==50.0.2",
                    "cffi==2.1.1", "pycparser==3.12", "cadquery-ocp-novtk==7.8.1.1.post1"], check=True)
    copy_tree(PROJECT / "src/jlc2cadence", stage / "app/src/jlc2cadence")
    plugin = stage / "app/capture_plugin"
    plugin.mkdir(parents=True)
    for name in ("jlc2capture.tcl", "jlc2capture_dock.tcl", "jlc2capture_updates.tcl", "version.json"):
        shutil.copy2(PROJECT / "capture_plugin" / name, plugin / name)
    web = plugin / "web"
    web.mkdir()
    for name in ("index.html", "panel.css", "panel.js", "step_preview.js"):
        shutil.copy2(PROJECT / "capture_plugin/web" / name, web / name)
    package = json.loads((PROJECT / "capture_plugin/web/package.json").read_text())
    package_lock = json.loads((PROJECT / "capture_plugin/web/package-lock.json").read_text())
    licenses = stage / "licenses"
    licenses.mkdir()
    for name, paths in WEB_ASSETS.items():
        version = package["dependencies"][name]
        archive_path = cache / f"{name}-{version}.tgz"
        fetch(f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz", archive_path)
        expected = package_lock["packages"]["node_modules/" + name]["integrity"]
        algorithm, value = expected.split("-", 1)
        if base64.b64encode(hashlib.new(algorithm, archive_path.read_bytes()).digest()).decode() != value:
            raise ValueError("Web asset checksum differs from package-lock: " + name)
        with tarfile.open(archive_path) as archive:
            for relative in paths:
                member = archive.extractfile("package/" + relative)
                if member is None:
                    raise ValueError("Missing web asset " + relative)
                target = web / "node_modules" / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(member.read())
                if relative == "LICENSE":
                    shutil.copy2(target, licenses / (name + ".txt"))
    shutil.copy2(runtime / "LICENSE.txt", licenses / "Python.txt")
    dependencies = {"Python": PYTHON_VERSION, "easyeda2kicad": "1.0.1", "svgelements": "1.9.6",
                    "cryptography": "50.0.2", "cffi": "2.1.1", "pycparser": "3.12",
                    "cadquery-ocp-novtk": "7.8.1.1.post1",
                    **package["dependencies"]}
    for distribution in (runtime / "Lib/site-packages").glob("*.dist-info"):
        for path in distribution.rglob("*"):
            if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                shutil.copy2(path, licenses / (distribution.name + "-" + path.name))
    ocp_license = runtime / "Lib/site-packages/cadquery_ocp_novtk/LICENSE"
    if not ocp_license.is_file():
        raise ValueError("The OCP runtime wheel is missing its upstream license")
    shutil.copy2(ocp_license, licenses / "cadquery-ocp-novtk-LICENSE.txt")
    for name, (upstream_name, checksum) in OCCT_NOTICES.items():
        cached = cache / name
        fetch("https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/V7_8_1/" + upstream_name, cached)
        if hashlib.sha256(cached.read_bytes()).hexdigest() != checksum:
            raise ValueError("OCCT license notice checksum differs from the pinned upstream tag")
        shutil.copy2(cached, licenses / name)
    (licenses / "NOTICE.txt").write_text(
        "This distribution makes use of facilities provided by Open CASCADE Technology (OCCT).\n"
        "The unmodified OCCT 7.8.1 shared libraries are supplied through cadquery-ocp-novtk.\n"
        "OCCT source: https://github.com/Open-Cascade-SAS/OCCT/tree/V7_8_1\n"
        "OCP bindings source: https://github.com/CadQuery/OCP\n"
        "See the included Apache, LGPL 2.1 and Open CASCADE exception notices.\n",
        encoding="ascii")
    (licenses / "dependencies.json").write_text(json.dumps(dependencies, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(HERE / "manage.py", stage / "manage.py")
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text())["project"]["version"]
    files = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(stage.rglob("*")) if path.is_file()}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    release_id = version + "-" + digest
    manifest = {"schema": 1, "version": version, "release_id": release_id,
                "runtime_features": ["updates", "local-step-preview"],
                "runtime": {"version": PYTHON_VERSION, "url": PYTHON_URL,
                            "sha256": hashlib.sha256(runtime_zip.read_bytes()).hexdigest()},
                "files": files}
    (stage / "payload-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    subprocess.run([str(runtime / "python.exe"), "-B", "-I", "-c",
                    "import ssl,jlc2cadence.capture_bridge,jlc2cadence.updater,cryptography,svgelements,easyeda2kicad,OCP;print('runtime health: OK')"], check=True)
    with zipfile.ZipFile(destination / ("JLC2Cadence-" + release_id + "-payload.zip"), "w", zipfile.ZIP_DEFLATED) as archive:
        for path in stage.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(stage))
    (destination / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.output.resolve())
    print(json.dumps({key: result[key] for key in ("schema", "version", "release_id", "runtime")}, indent=2))
