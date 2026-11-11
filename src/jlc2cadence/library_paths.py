"""Register generated libraries without replacing the user's Cadence settings.

Capture's live INI is deliberately managed by its native Tcl API.  This module
only manages Allegro's user env file; a running PCB Editor must reload/restart
before it observes changes made by another application.
"""

from __future__ import annotations

import codecs
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


BEGIN = "# BEGIN JLC2CAPTURE LIBRARY PATHS v1"
END = "# END JLC2CAPTURE LIBRARY PATHS v1"


def library_directories(root: Path) -> dict[str, Path]:
    root = root.expanduser().resolve()
    return {
        "psmpath": root / "allegro" / "symbols",
        "padpath": root / "allegro" / "padstacks",
        "steppath": root / "3d",
    }


def _path_lines(root: Path) -> list[str]:
    lines = []
    for key, directory in library_directories(root).items():
        value = directory.as_posix()
        if any(char in value for char in ('"', "\n", "\r", "$")):
            raise ValueError("The library path contains a character unsupported by Allegro env files.")
        lines.append(f'set {key} = ( "{value}" ${key} )')
    return lines


def _persisted_environment(name: str) -> str:
    """Cadence installers may set HOME after the current shell was started."""
    if os.name != "nt":
        return ""
    import winreg

    for hive, key in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, kind = winreg.QueryValueEx(handle, name)
            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value:
                return os.path.expandvars(str(value))
        except OSError:
            pass
    return ""


def discover_allegro_env(
    cadence_root: Path | None = None, *, environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Follow Allegro's ALLEGRO_PCBENV > HOME/pcbenv precedence.

    An explicit mapping is also a deterministic way for tests/tools to select
    an isolated user environment, without consulting this machine's registry.
    """
    values = os.environ if environ is None else environ
    for name in ("ALLEGRO_PCBENV", "HOME"):
        value = values.get(name, "")
        if not value and environ is None:
            value = _persisted_environment(name)
        if value:
            directory = Path(os.path.expandvars(value)).expanduser()
            if not directory.is_absolute():
                raise ValueError(f"{name} must be an absolute path to register Allegro libraries.")
            if name == "ALLEGRO_PCBENV" and not directory.is_dir():
                # Allegro ignores a nonexistent override (documented in its
                # global env). Creating it here would silently switch profiles.
                continue
            return directory / "env" if name == "ALLEGRO_PCBENV" else directory / "pcbenv" / "env"
    # The standard Windows installer uses SPB_Data adjacent to SPB_17.x.
    if cadence_root is not None:
        directory = cadence_root.expanduser().resolve().parent / "SPB_Data" / "pcbenv"
        if directory.is_dir():
            return directory / "env"
    return None


def merge_allegro_env(text: str, root: Path) -> str:
    """Replace only our marked block, retaining all unrelated text verbatim.

    The block follows existing assignments, including values saved by Allegro's
    User Preferences dialog, so their values are prepended to rather than lost
    or allowed to override the newly registered library.
    """
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
            raise ValueError("The JLC2Capture block in the Allegro env file is malformed; settings were not changed.")
        del lines[starts[0]:ends[0] + 1]
    original = "".join(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    if not original:
        original = "source $TELENV" + newline
    elif not original.endswith(("\n", "\r")):
        original += newline
    block = [BEGIN, *_path_lines(root), END]
    return original + newline.join(block) + newline


@contextmanager
def _env_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".jlc2capture.lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another library registration is updating the Allegro env file.")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _decode_env(data: bytes) -> tuple[str, str]:
    if data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith(codecs.BOM_UTF16_LE):
        encoding = "utf-16-le"
    elif data.startswith(codecs.BOM_UTF16_BE):
        encoding = "utf-16-be"
    else:
        encoding = "mbcs" if os.name == "nt" else "utf-8"
    return data.decode(encoding), encoding


def register_allegro_paths(
    root: Path, cadence_root: Path | None = None, *, env_file: Path | None = None,
) -> dict[str, object]:
    """Persist one active library, backing up exact original bytes on first edit.

    Explicit env_file is intended for isolated tests and administrative tools.
    Non-Windows conversion hosts do not modify their user environment implicitly.
    Permission and malformed-file errors propagate so callers can surface them.
    """
    directories = library_directories(root)
    result: dict[str, object] = {key: str(value) for key, value in directories.items()}
    result.update({"status": "missing_assets", "changed": False})
    # Do not advertise an empty/unbuilt library as ready for PCB placement.
    symbols = directories["psmpath"]
    if not symbols.is_dir() or not any(
        path.is_file() and path.suffix.lower() == ".dra" and path.stat().st_size > 0
        for path in symbols.iterdir()
    ):
        return result
    if env_file is None:
        if os.name != "nt":
            result["status"] = "unsupported_host"
            return result
        env_file = discover_allegro_env(cadence_root)
    if env_file is None:
        result["status"] = "environment_unavailable"
        return result
    env_file = env_file.expanduser().resolve()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    result["env_file"] = str(env_file)
    with _env_lock(env_file):
        original = env_file.read_bytes() if env_file.exists() else b""
        text, encoding = _decode_env(original)
        replacement = merge_allegro_env(text, root).encode(encoding)
        if replacement != original:
            backup = env_file.with_name(env_file.name + ".jlc2capture.bak")
            if original and not backup.exists():
                # Exclusive creation never overwrites an earlier recovery copy.
                with backup.open("xb") as stream:
                    stream.write(original)
            descriptor, filename = tempfile.mkstemp(prefix=".jlc2capture-env-", dir=env_file.parent)
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(env_file)
            finally:
                temporary.unlink(missing_ok=True)
            result["changed"] = True
            if backup.exists():
                result["backup"] = str(backup)
    result["status"] = "registered"
    result["requires_pcb_editor_restart"] = True
    return result
