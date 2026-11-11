# Native PCB placement acceptance

`allegro_place_smoke.py` verifies an existing converted library in an independent PCB using Cadence itself. It reads a copy of the source DRA, places the compiled PSM at `(25, 25)` mm, saves a new board, reopens it in a second PCB session, and compares native pin centers, pin numbers, padstack names, and STEP mapping metadata. Source DRA and PSM hashes must remain unchanged.

Run from the repository root on Windows, with Python 3.10+ and an installed Cadence product:

```powershell
python tools/acceptance/allegro_place_smoke.py `
  --cadence-root 'C:\Cadence\SPB_17.4' `
  --product OrCAD_PCB_Designer `
  --library-root 'D:\MyLibrary' `
  --footprint c11255_res_th_bd3_0_l9_49041e03 `
  --expected-pins 2 `
  --output 'D:\Acceptance\resistor-first-run'
```

Choose the product you normally select when opening PCB Editor. The example product name is specific to that installation; it is not a universal default. Cadence documents `allegro.exe -product help` for available command-line product names. Existing shortcut arguments may also specify `-product`. A legacy `-pcb` shortcut can select a different product than the graphical selector. This runner requires an explicit product and never tries alternate products automatically.

The output directory must not exist and must be outside both the library and Cadence installation. The runner creates a process-local `ALLEGRO_PCBENV` profile there and does not change user preferences or open an existing user PCB. A fresh output directory is required on every run. All source scripts, process output, native readbacks, the saved PCB, and `acceptance.json` remain in that directory for review. Exit status is nonzero if any phase fails, including a native process that exits successfully without saving a PCB.

The generated PSM's actual filename determines the name passed to the native placement API. `acceptance.json` records both the requested and resolved names, including any case normalization. This normalization does not imply that differently cased names always fail in Allegro.

This check requires a millimeter DRA with accuracy 4, the format emitted by the converter. Its coordinate tolerance is 0.00005 mm. It checks all native pins, including source pads retained as extra pins; the expected count is not necessarily the number of electrical contacts in the product description. Matching STEP metadata does not certify the source model's geometry, physical fit, or interactive 3D rendering. Pad shape and graphical primitive checks belong to the separate native geometry validation. The runner does not establish commercial license entitlement or compatibility with every Cadence version.

## Optional STEP mapping UI

The exported `scripts/prepare_step_mapping.ps1` also accepts an explicit product when opening Cadence's mapping UI:

```powershell
.\scripts\prepare_step_mapping.ps1 -OpenMapper -Product OrCAD_PCB_Designer
```

Omitting `-Product` preserves Cadence's usual selection. Automatic package generation uses symbol-editor mode and does not open or place parts in a user's PCB.
