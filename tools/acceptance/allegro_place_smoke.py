"""Place an original PSM in a new PCB, save/reopen, and compare native pins.

Use a product name supported by the user's installation, as shown by Allegro's
normal product selector. No license files are read or changed, and no fallback
products are tried. All writes are restricted to a newly created output folder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def quoted(value: str | Path) -> str:
    text = str(value).replace("\\", "/")
    if any(character in ('"', "$") or ord(character) < 32 or ord(character) == 127
           for character in text):
        raise ValueError("Paths must not contain quotes, control characters, or dollar signs")
    return '"' + text + '"'


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_psm_path(symbols: Path, footprint: str) -> Path:
    # Resolve the physical filename for consistent native calls and reporting,
    # even when a caller uses a differently cased alias on Windows.
    matches = [path for path in symbols.iterdir()
               if path.is_file() and path.suffix.casefold() == ".psm"
               and path.stem.casefold() == footprint.casefold()]
    if len(matches) != 1:
        raise ValueError("Expected one compiled PSM for footprint: " + footprint)
    return matches[0]


def readback(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    pins = [
        {"number": number, "x": float(x), "y": float(y), "padstack": padstack}
        for number, x, y, padstack in re.findall(
            r'^PIN\t([^\t\r\n]+)\t([-+\d.eE]+)\t([-+\d.eE]+)\t([^\r\n]+)$', text, re.M
        )
    ]
    units = re.findall(r'^UNITS\t([^\t\r\n]+)\t(\d+)$', text, re.M)
    count = re.findall(r'^PIN_COUNT\t(\d+)$', text, re.M)
    step = re.findall(r'^STEP\t(.+)$', text, re.M)
    if (len(units) != 1 or len(count) != 1 or len(step) != 1 or not pins
            or int(count[0]) != len(pins)
            or len(re.findall(r'^PIN\b.*$', text, re.M)) != len(pins)
            or any(not math.isfinite(pin[axis]) for pin in pins for axis in ("x", "y"))):
        raise ValueError("Incomplete native readback: " + str(path))
    return {"units": units[0][0], "accuracy": int(units[0][1]), "pins": pins,
            "step": step[0], "raw": text}


def compare(source: dict, board: dict, expected_pins: int | None) -> dict:
    if source["units"] != "millimeters" or source["accuracy"] != 4:
        raise ValueError("This acceptance runner requires a source DRA in millimeters with accuracy 4")
    if (board["units"], board["accuracy"]) != ("millimeters", 4):
        raise ValueError("The reopened board did not preserve millimeters with accuracy 4")
    key = lambda pin: (pin["number"], pin["x"], pin["y"], pin["padstack"])
    before = sorted(source["pins"], key=key)
    after = sorted(board["pins"], key=key)
    if len(before) != len(after) or (expected_pins is not None and len(before) != expected_pins):
        raise ValueError("Pin count changed or differs from --expected-pins")
    maximum = 0.0
    for original, placed in zip(before, after):
        if (original["number"], original["padstack"]) != (placed["number"], placed["padstack"]):
            raise ValueError("Pin number or padstack changed during placement")
        maximum = max(maximum, abs(original["x"] - (placed["x"] - 25)),
                      abs(original["y"] - (placed["y"] - 25)))
    if maximum > 0.00005:
        raise ValueError(f"Native pin center mismatch: {maximum} mm")
    if source["step"] != board["step"]:
        raise ValueError("Native STEP association changed during placement")
    return {"pin_count": len(before), "max_pin_center_error_mm": maximum,
            "tolerance_mm": 0.00005, "all_pin_numbers_and_padstacks_equal": True,
            "step_mapping_equal": True, "translation_mm": [25, 25], "rotation_degrees": 0}


def run(args: argparse.Namespace) -> dict:
    if os.name != "nt":
        raise ValueError("Native Allegro acceptance requires Windows")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", args.footprint):
        raise ValueError("Footprint must be a generated library name, without a path or extension")
    if not args.product.strip():
        raise ValueError("Specify the product selected in your normal Allegro startup")
    cadence = args.cadence_root.resolve()
    library = args.library_root.resolve()
    root = args.output.resolve()
    exe = cadence / "tools/bin/allegro.exe"
    dra = library / "allegro/symbols" / (args.footprint + ".dra")
    psm = native_psm_path(library / "allegro/symbols", args.footprint)
    for path in (exe, dra, psm):
        quoted(path)
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("Required file is missing or empty: " + str(path))
    if root.exists() or root.is_relative_to(library) or root.is_relative_to(cadence):
        raise ValueError("Output must be a new folder outside the library and Cadence installation")
    quoted(root)
    root.mkdir(parents=True)
    result = {"ok": False, "product": args.product, "library": str(library), "phases": [],
              "requested_footprint": args.footprint, "native_footprint": psm.stem,
              "source_sha256_before": {str(path): sha256(path) for path in (dra, psm)}}
    report = root / "acceptance.json"
    def save_report():
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        environment = os.environ.copy()
        pcbenv = root / "pcbenv"
        pcbenv.mkdir()
        # A process-local profile prevents writing user MRU, display settings,
        # or remembered product selections. System configuration is retained.
        (pcbenv / "env").write_text(
            "source $TELENV\n" + "".join(
                f"set {key} = ( {quoted(library / relative)} ${key} )\n"
                for key, relative in (("psmpath", "allegro/symbols"),
                                      ("padpath", "allegro/padstacks"), ("steppath", "3d"))
            ), encoding="utf-8")
        environment["ALLEGRO_PCBENV"] = str(pcbenv)
        shutil.copy2(dra, root / "source_copy.dra")
        output_pins = (
            'fprintf(out "PIN_COUNT\\t%d\\n" length(pins))\n'
            'foreach(pin pins fprintf(out "PIN\\t%s\\t%.8f\\t%.8f\\t%s\\n" '
            'pin->number xCoord(pin->xy) yCoord(pin->xy) pin->name))\n'
        )
        scripts = {
            "source": (
                'let((out d pins units)\n'
                f'out=outfile({quoted(root / "source.txt")})\n'
                'd=axlDBGetDesign() pins=d->pins units=axlDBGetDesignUnits()\n'
                'fprintf(out "UNITS\\t%s\\t%d\\n" car(units) cadr(units))\n' + output_pins +
                'fprintf(out "STEP\\t%L\\n" axlStepGet(nil \'primary nil))\nclose(out))\n'
            ),
            "place": (
                'let((out placed saved)\n'
                f'out=outfile({quoted(root / "place.txt")})\n'
                'fprintf(out "UNITS_CHANGE=%L\\n" axlDBChangeDesignUnits("millimeters" 4))\n'
                'fprintf(out "UNITS=%L\\n" axlDBGetDesignUnits())\n'
                f'placed=axlDBCreateSymbol(list({quoted(psm.stem)} "package") 25:25 nil 0)\n'
                'fprintf(out "PLACED=%L\\n" placed)\n'
                'fprintf(out "MESSAGES=%L\\n" axlMsgContextGetString(nil))\nwhen(placed\n'
                f'saved=axlSaveDesign(?design {quoted(root / "placed_board.brd")} ?noMru t)\n'
                'fprintf(out "SAVED=%L\\n" saved))\nclose(out))\n'
            ),
            "reopen": (
                'let((out d pins units)\n'
                f'out=outfile({quoted(root / "reopened.txt")})\n'
                'd=axlDBGetDesign() units=axlDBGetDesignUnits()\n'
                'fprintf(out "UNITS\\t%s\\t%d\\n" car(units) cadr(units))\n'
                'fprintf(out "SYMBOL_COUNT\\t%d\\n" length(d->symbols))\n'
                'foreach(symbol d->symbols\n'
                'fprintf(out "NAME\\t%s\\n" symbol->name)\n'
                'pins=symbol->pins\n' + output_pins +
                'fprintf(out "STEP\\t%L\\n" axlStepGet(nil \'primary symbol->definition)))\nclose(out))\n'
            ),
        }
        designs = {"source": "source_copy.dra", "place": "new_board.brd", "reopen": "placed_board.brd"}
        for phase, script in scripts.items():
            il = root / (phase + ".il")
            replay = root / (phase + ".scr")
            il.write_text(script, encoding="utf-8")
            replay.write_text(f"skill load({quoted(il)})\nexit\n", encoding="utf-8")
            command = [str(exe), "-product", args.product, "-nograph", "-p", str(root),
                       str(root / designs[phase]), "-s", str(replay)]
            try:
                completed = subprocess.run(command, cwd=root, env=environment, capture_output=True,
                                           text=True, errors="replace", timeout=args.timeout,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            except subprocess.TimeoutExpired as error:
                decode = lambda value: value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                result["phases"].append({"phase": phase, "command": command, "exit_code": None,
                                         "timed_out": True, "stdout": decode(error.stdout),
                                         "stderr": decode(error.stderr)})
                raise ValueError(f"Allegro {phase} timed out after {args.timeout} seconds; see acceptance.json") from error
            result["phases"].append({"phase": phase, "command": command, "exit_code": completed.returncode,
                                     "stdout": completed.stdout, "stderr": completed.stderr})
            save_report()
            if completed.returncode:
                raise ValueError(f"Allegro {phase} exited with {completed.returncode}; see acceptance.json")
            if phase == "place" and not (root / "placed_board.brd").is_file():
                raise ValueError("The native script did not save a PCB; inspect place.txt and journal")
        source = readback(root / "source.txt")
        board = readback(root / "reopened.txt")
        if "SYMBOL_COUNT\t1\n" not in board["raw"]:
            raise ValueError("Expected exactly one symbol in the reopened PCB")
        name = re.search(r'^NAME\t([^\r\n]+)$', board["raw"], re.M)
        if not name or name[1].casefold() != args.footprint.casefold():
            raise ValueError("The reopened symbol name differs from the requested footprint")
        result["comparison"] = compare(source, board, args.expected_pins)
        result["source_sha256_after"] = {str(path): sha256(path) for path in (dra, psm)}
        if result["source_sha256_before"] != result["source_sha256_after"]:
            raise ValueError("A source library file changed during native acceptance")
        result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
    finally:
        save_report()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadence-root", type=Path, required=True)
    parser.add_argument("--product", required=True, help="Installed product's official CLI name; no automatic fallback")
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--footprint", required=True)
    parser.add_argument("--expected-pins", type=int)
    parser.add_argument("--output", type=Path, required=True, help="A new isolated directory")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 600 or (args.expected_pins is not None and args.expected_pins <= 0):
        parser.error("Timeout must be in (0, 600] and expected pin count must be positive")
    try:
        result = run(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"ok": result["ok"], "error": result.get("error"),
                      "report": str(args.output.resolve() / "acceptance.json")}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
