from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .step_mapping import validate_step_mapping_output


_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


DEFAULT_CADENCE_ROOTS = [
    Path(r"D:\Cadence_17_4\Cadence\SPB_17.4"),
    Path(r"D:\Cadence_17_2\Cadence\SPB_17.2"),
    Path(r"C:\Cadence_17_4\Cadence\SPB_17.4"),
    Path(r"C:\Cadence_17_2\Cadence\SPB_17.2"),
    Path(r"C:\Cadence\SPB_17.4"),
    Path(r"C:\Cadence\SPB_17.2"),
]


@dataclass
class CadenceTools:
    root: Path | None = None
    allegro: Path | None = None
    allegro_batch: Path | None = None
    capture: Path | None = None
    padstack_editor: Path | None = None
    transolb: Path | None = None
    sym_3d_update: Path | None = None

    @classmethod
    def discover(cls, root_hint: Path | None = None) -> "CadenceTools":
        # A requested host must never silently fall back to another Cadence version.
        roots = [root_hint.expanduser().resolve()] if root_hint else DEFAULT_CADENCE_ROOTS
        for root in roots:
            bin_dir = _bin_dir(root)
            if not bin_dir.exists():
                continue
            tools = cls(
                root=root,
                allegro=_tool(bin_dir, "allegro.exe"),
                allegro_batch=_tool(bin_dir, "allegro_batch.exe"),
                capture=_tool(bin_dir, "Capture.exe"),
                padstack_editor=_tool(bin_dir, "padstack_editor.exe"),
                transolb=_tool(bin_dir, "TransOLB.exe"),
                sym_3d_update=_tool(bin_dir, "sym_3d_update.exe"),
            )
            if any([tools.allegro, tools.capture, tools.allegro_batch]):
                return tools
        return cls()

    def to_dict(self) -> dict[str, str | None]:
        return {
            "root": str(self.root) if self.root else None,
            "allegro": str(self.allegro) if self.allegro else None,
            "allegro_batch": str(self.allegro_batch) if self.allegro_batch else None,
            "capture": str(self.capture) if self.capture else None,
            "padstack_editor": str(self.padstack_editor) if self.padstack_editor else None,
            "transolb": str(self.transolb) if self.transolb else None,
            "sym_3d_update": str(self.sym_3d_update) if self.sym_3d_update else None,
        }


def write_cadence_run_notes(
    cadence_tools: CadenceTools,
    scripts_dir: Path,
    out_dir: Path,
    run_requested: bool,
    run_results: list[dict[str, str | int | None]] | None = None,
    capture_result: dict[str, str | int | None] | None = None,
    step_mapping_result: dict[str, object] | None = None,
) -> Path:
    path = scripts_dir / "cadence_run_notes.md"
    lines = [
        "# Cadence Run Notes",
        "",
        "Detected tools:",
        "",
    ]
    for key, value in cadence_tools.to_dict().items():
        lines.append(f"- `{key}`: `{value or 'not found'}`")
    lines.extend(
        [
            "",
            "Generated helper scripts:",
            "",
            "- `generate_allegro.il`",
            "- `run_generate_allegro.scr`",
            "- `create_symbols.ps1`",
            "- `generate_capture_library.ps1`",
            "- `apply_3d_mapping.il`",
            "",
        ]
    )
    if run_requested:
        lines.append("`--run-cadence` was requested. Generated Allegro replay scripts were used to build `.dra` files, then `.dra` files were passed to `allegro_batch create_sym -p` when possible.")
    else:
        lines.append("Cadence execution was not requested. Review generated files before running them in Cadence 17.4.")
    lines.extend(
        [
            "",
            "Suggested Allegro review command after validating scripts:",
            "",
            "```powershell",
            f"& '{cadence_tools.allegro or 'allegro.exe'}' -nograph -symbol package '<footprint>' -s '<footprint.scr>'",
            "```",
            "",
            "Suggested package-symbol compile command:",
            "",
            "```powershell",
            f"& '{cadence_tools.allegro_batch or 'allegro_batch.exe'}' create_sym -p '<input.dra>' '<output.psm>'",
            "```",
        ]
    )
    if run_results is not None:
        lines.extend(["", "Allegro run results:", ""])
        if run_results:
            for result in run_results:
                lines.append(
                    "- `{footprint}`: {status} (exit={exitCode})".format(
                        footprint=result.get("footprint", "?"),
                        status=result.get("status", "?"),
                        exitCode=result.get("exitCode"),
                    )
                )
        else:
            lines.append("- No `.dra` files were available to compile.")
    if capture_result is not None:
        lines.extend(["", "Capture run result:", ""])
        lines.append(
            "- `{olb}`: {status} (exit={exitCode})".format(
                olb=capture_result.get("olb", "?"),
                status=capture_result.get("status", "?"),
                exitCode=capture_result.get("exitCode"),
            )
        )
    if step_mapping_result is not None:
        lines.extend(["", "STEP mapping result:", ""])
        lines.append(
            "- status={status}, entries={entries}, mapped={mapped}, unmapped={unmapped}".format(
                status=step_mapping_result.get("status", "?"),
                entries=step_mapping_result.get("entry_count", 0),
                mapped=step_mapping_result.get("mapped_entry_count", 0),
                unmapped=step_mapping_result.get("unmapped_entry_count", 0),
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def compile_existing_package_symbols(
    cadence_tools: CadenceTools,
    symbols_dir: Path,
    reuse_statuses: dict[str, str] | None = None,
) -> list[dict[str, str | int | None]]:
    plan_path = symbols_dir / "symbol_build_plan.json"
    if not plan_path.exists():
        return []
    if cadence_tools.allegro_batch is None:
        return [_result("", "", "", "missing_allegro_batch", None)]

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    results: list[dict[str, str | int | None]] = []
    reuse_statuses = reuse_statuses or {}
    for symbol in plan.get("symbols", []):
        footprint = str(symbol.get("footprint", ""))
        dra = symbols_dir / str(symbol.get("dra", ""))
        psm = symbols_dir / str(symbol.get("psm", ""))
        reused_status = reuse_statuses.get(footprint)
        if reused_status:
            result = _result(footprint, str(dra), str(psm), reused_status, 0)
            result["reuse"] = True
            if reused_status == "reused_psm" and not psm.exists():
                result["status"] = "failed_reused_psm"
            elif reused_status == "reused_dra" and not dra.exists():
                result["status"] = "failed_reused_dra"
            results.append(result)
            continue
        if not dra.exists():
            results.append(_result(footprint, str(dra), str(psm), "missing_dra", None))
            continue
        proc = subprocess.run(
            [str(cadence_tools.allegro_batch), "create_sym", "-p", str(dra), str(psm)],
            cwd=str(symbols_dir),
            text=True,
            capture_output=True,
            check=False,
            creationflags=_WINDOWS_NO_WINDOW,
        )
        status = "created_psm" if proc.returncode == 0 and psm.exists() else "failed"
        result = _result(footprint, str(dra), str(psm), status, proc.returncode)
        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
        results.append(result)

    result_path = symbols_dir / "create_symbols.results.json"
    result_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


def run_symbol_build_plan(
    cadence_tools: CadenceTools,
    symbols_dir: Path,
    timeout_seconds: int = 180,
    reuse_statuses: dict[str, str] | None = None,
) -> list[dict[str, str | int | None]]:
    plan_path = symbols_dir / "symbol_build_plan.json"
    if not plan_path.exists():
        return []

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    results: list[dict[str, str | int | None]] = []
    reuse_statuses = reuse_statuses or {}
    for symbol in plan.get("symbols", []):
        footprint = str(symbol.get("footprint", ""))
        dra = symbols_dir / str(symbol.get("dra", ""))
        psm = symbols_dir / str(symbol.get("psm", ""))
        script = symbols_dir / str(symbol.get("allegro_script", ""))
        result = _result(footprint, str(dra), str(psm), "not_run", None)

        reused_status = reuse_statuses.get(footprint)
        if reused_status:
            result["status"] = reused_status
            result["exitCode"] = 0
            result["reuse"] = True
            if reused_status == "reused_psm" and not psm.exists():
                result["status"] = "failed_reused_psm"
            elif reused_status == "reused_dra" and not dra.exists():
                result["status"] = "failed_reused_dra"
            results.append(result)
            continue

        if not script.exists():
            result["status"] = "missing_build_script"
            results.append(result)
            continue

        try:
            result["removedStaleTargets"] = _remove_generated_symbol_targets(dra, psm)
        except OSError as exc:
            result["status"] = "failed_cleanup"
            result["exitCode"] = None
            result["cleanupError"] = str(exc)
            results.append(result)
            continue

        if cadence_tools.allegro is None:
            result["status"] = "missing_allegro"
        else:
            build = _run_process(
                [str(cadence_tools.allegro), "-nograph", "-symbol", "package", footprint, "-s", str(script)],
                cwd=symbols_dir,
                timeout_seconds=timeout_seconds,
            )
            result["buildExitCode"] = build["exitCode"]
            result["exitCode"] = build["exitCode"]
            result["buildStdout"] = build["stdout"]
            result["buildStderr"] = build["stderr"]
            result["buildStatus"] = "created_dra" if build["exitCode"] == 0 and dra.exists() else "failed_dra"
            result["status"] = result["buildStatus"]

        if dra.exists():
            if cadence_tools.allegro_batch is None:
                result["status"] = "created_dra_missing_allegro_batch"
            else:
                compile_proc = _run_process(
                    [str(cadence_tools.allegro_batch), "create_sym", "-p", str(dra), str(psm)],
                    cwd=symbols_dir,
                    timeout_seconds=timeout_seconds,
                )
                result["compileExitCode"] = compile_proc["exitCode"]
                result["exitCode"] = compile_proc["exitCode"]
                result["compileStdout"] = compile_proc["stdout"]
                result["compileStderr"] = compile_proc["stderr"]
                result["status"] = "created_psm" if compile_proc["exitCode"] == 0 and psm.exists() else "failed_psm"

        results.append(result)

    result_path = symbols_dir / "create_symbols.results.json"
    result_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


def run_capture_build(
    cadence_tools: CadenceTools,
    out_dir: Path,
    timeout_seconds: int = 180,
) -> dict[str, str | int | None]:
    capture_dir = out_dir / "capture"
    script = out_dir / "scripts" / "generate_capture_library.ps1"
    olb = capture_dir / "library.olb"
    result: dict[str, str | int | None] = {
        "script": str(script),
        "olb": str(olb),
        "status": "not_run",
        "exitCode": None,
    }
    if not script.exists():
        result["status"] = "missing_capture_script"
        return _write_capture_result(capture_dir, result)
    if cadence_tools.capture is None:
        result["status"] = "missing_capture"
        return _write_capture_result(capture_dir, result)

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-CaptureExe",
        str(cadence_tools.capture),
    ]
    run = _run_process(command, cwd=out_dir, timeout_seconds=timeout_seconds)
    result["exitCode"] = run["exitCode"]
    result["stdout"] = run["stdout"]
    result["stderr"] = run["stderr"]
    result["status"] = "created_olb" if run["exitCode"] == 0 and olb.exists() else "failed_olb"
    return _write_capture_result(capture_dir, result)


def run_step_mapping(
    cadence_tools: CadenceTools,
    out_dir: Path,
    timeout_seconds: int = 180,
) -> dict[str, object]:
    """Apply generated STEP mappings to existing package .dra files through SKILL."""
    out_dir = out_dir.expanduser().resolve()
    model_dir = out_dir / "3d"
    replay = out_dir / "scripts" / "run_apply_3d_mapping.scr"
    skill = out_dir / "scripts" / "apply_3d_mapping.il"
    result_path = model_dir / "apply_3d_mapping.results.json"
    result: dict[str, object] = {
        "tool": str(cadence_tools.allegro) if cadence_tools.allegro else "",
        "replay": str(replay),
        "skill": str(skill),
        "status": "not_run",
        "exitCode": None,
        "entry_count": 0,
        "mapped_entry_count": 0,
        "unmapped_entry_count": 0,
        "stdout": "",
        "stderr": "",
    }

    if not replay.exists() or not skill.exists():
        result["status"] = "missing_step_mapping_script"
        return _write_step_mapping_result(result_path, result)
    if cadence_tools.allegro is None:
        result["status"] = "missing_allegro"
        return _write_step_mapping_result(result_path, result)

    _write_cadence_env(out_dir / "_cadence_pcbenv", model_dir)
    env = os.environ.copy()
    env["ALLEGRO_PCBENV"] = str(out_dir / "_cadence_pcbenv")
    env["STEPPATH"] = _prepend_path_value(env.get("STEPPATH"), model_dir)
    env["STEP_MAPPING_PATH"] = _prepend_path_value(env.get("STEP_MAPPING_PATH"), model_dir)
    env["STEP_FACET_PATH"] = _prepend_path_value(env.get("STEP_FACET_PATH"), model_dir)
    try:
        proc = subprocess.run(
            [
                str(cadence_tools.allegro),
                "-nograph",
                "-symbol",
                "package",
                "JLC2C_3D_MAPPING",
                "-s",
                str(replay),
            ],
            cwd=str(out_dir),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=_WINDOWS_NO_WINDOW,
        )
        result["exitCode"] = proc.returncode
        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
    except subprocess.TimeoutExpired as exc:
        result["status"] = "timeout"
        result["stdout"] = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
        result["stderr"] = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "timeout"
        return _write_step_mapping_result(result_path, result)

    report = validate_step_mapping_output(out_dir, require_mapped=False)
    result["entry_count"] = len(report.entries)
    result["mapped_entry_count"] = report.mapped_entry_count
    result["unmapped_entry_count"] = report.unmapped_entry_count
    if proc.returncode != 0:
        result["status"] = "failed"
    elif not report.entries:
        result["status"] = "no_step_entries"
    elif report.mapped_entry_count == len(report.entries):
        result["status"] = "mapped"
    elif report.mapped_entry_count > 0:
        result["status"] = "partial"
    else:
        result["status"] = "not_mapped"
    return _write_step_mapping_result(result_path, result)


def run_sym_3d_update(
    cadence_tools: CadenceTools,
    out_dir: Path,
    symbols_dir: Path | None = None,
    destination_dir: Path | None = None,
    timeout_seconds: int = 180,
) -> dict[str, str | int | None]:
    out_dir = out_dir.expanduser().resolve()
    model_dir = out_dir / "3d"
    symbols_dir = (symbols_dir or out_dir / "allegro" / "symbols").expanduser().resolve()
    destination_dir = (destination_dir or out_dir / "allegro" / "symbols_3d").expanduser().resolve()
    symbol_list = model_dir / "sym_3d_update_symbols.txt"
    cadence_env_dir = out_dir / "_cadence_pcbenv"
    result_path = model_dir / "sym_3d_update.results.json"
    result: dict[str, str | int | None] = {
        "tool": str(cadence_tools.sym_3d_update) if cadence_tools.sym_3d_update else "",
        "symbol_list": str(symbol_list),
        "symbols_dir": str(symbols_dir),
        "destination_dir": str(destination_dir),
        "model_dir": str(model_dir),
        "cadence_pcbenv": str(cadence_env_dir),
        "log": str(out_dir / "sym_3d_update.log"),
        "status": "not_run",
        "exitCode": None,
        "updated_symbols": 0,
        "failed_symbols": 0,
        "skipped_symbols": 0,
        "stdout": "",
        "stderr": "",
    }

    if cadence_tools.sym_3d_update is None:
        result["status"] = "missing_sym_3d_update"
        return _write_sym_3d_result(result_path, result)
    if not symbol_list.exists():
        result["status"] = "missing_symbol_list"
        return _write_sym_3d_result(result_path, result)
    if not symbols_dir.exists():
        result["status"] = "missing_symbol_dir"
        return _write_sym_3d_result(result_path, result)
    if not model_dir.exists():
        result["status"] = "missing_model_dir"
        return _write_sym_3d_result(result_path, result)

    destination_dir.mkdir(parents=True, exist_ok=True)
    _write_cadence_env(cadence_env_dir, model_dir)
    env = os.environ.copy()
    env["ALLEGRO_PCBENV"] = str(cadence_env_dir)
    env["STEPPATH"] = _prepend_path_value(env.get("STEPPATH"), model_dir)
    env["STEP_MAPPING_PATH"] = _prepend_path_value(env.get("STEP_MAPPING_PATH"), model_dir)
    env["STEP_FACET_PATH"] = _prepend_path_value(env.get("STEP_FACET_PATH"), model_dir)
    command = [
        str(cadence_tools.sym_3d_update),
        "-c",
        str(symbol_list),
        "-s",
        str(symbols_dir),
        "-d",
        str(destination_dir),
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(out_dir),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=_WINDOWS_NO_WINDOW,
        )
        result["exitCode"] = proc.returncode
        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
        combined = f"{proc.stdout}\n{proc.stderr}"
        result["updated_symbols"] = _sym_3d_count(combined, "updated")
        result["failed_symbols"] = _sym_3d_count(combined, "failed")
        result["skipped_symbols"] = _sym_3d_count(combined, "skipped")
        if proc.returncode != 0 or int(result["failed_symbols"] or 0) > 0:
            result["status"] = "failed"
        elif int(result["updated_symbols"] or 0) > 0:
            result["status"] = "updated"
        elif int(result["skipped_symbols"] or 0) > 0:
            result["status"] = "skipped_no_3d_mapping"
        else:
            result["status"] = "no_updates"
    except subprocess.TimeoutExpired as exc:
        result["status"] = "timeout"
        result["stdout"] = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
        result["stderr"] = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "timeout"
    return _write_sym_3d_result(result_path, result)


def _run_process(command: list[str], cwd: Path, timeout_seconds: int) -> dict[str, str | int | None]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            creationflags=_WINDOWS_NO_WINDOW,
        )
        return {
            "exitCode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exitCode": None,
            "stdout": (exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "timeout",
        }


def _remove_generated_symbol_targets(*paths: Path) -> list[str]:
    removed: list[str] = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def _write_capture_result(capture_dir: Path, result: dict[str, str | int | None]) -> dict[str, str | int | None]:
    capture_dir.mkdir(parents=True, exist_ok=True)
    result_path = capture_dir / "capture_build.results.json"
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _write_sym_3d_result(path: Path, result: dict[str, str | int | None]) -> dict[str, str | int | None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _write_step_mapping_result(path: Path, result: dict[str, object]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _write_cadence_env(env_dir: Path, model_dir: Path) -> Path:
    env_dir.mkdir(parents=True, exist_ok=True)
    env_path = env_dir / "env"
    model_text = str(model_dir).replace("\\", "/")
    env_path.write_text(
        "\n".join(
            [
                "# Generated by jlc2cadence for sym_3d_update.",
                "source $TELENV",
                f'set steppath = "{model_text}"',
                f'set step_mapping_path = "{model_text}"',
                f'set step_facet_path = "{model_text}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_path


def _prepend_path_value(existing: str | None, value: Path) -> str:
    value_text = str(value)
    if not existing:
        return value_text
    if value_text.lower() in existing.lower().split(os.pathsep):
        return existing
    return os.pathsep.join([value_text, existing])


def _sym_3d_count(output: str, kind: str) -> int:
    match = re.search(rf"Number of {kind} symbols:\s*(\d+)", output, flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _bin_dir(root: Path) -> Path:
    if root.name.lower() == "bin":
        return root
    candidate = root / "tools" / "bin"
    return candidate if candidate.exists() else root


def _tool(bin_dir: Path, exe: str) -> Path | None:
    path = bin_dir / exe
    return path if path.exists() else None


def _result(
    footprint: str,
    dra: str,
    psm: str,
    status: str,
    exit_code: int | None,
) -> dict[str, str | int | None]:
    return {
        "footprint": footprint,
        "dra": dra,
        "psm": psm,
        "status": status,
        "exitCode": exit_code,
    }
