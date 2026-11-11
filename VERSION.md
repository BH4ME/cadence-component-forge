# Version Log

## Current Version

`v0.48`

## Rules

- Increment one minor step when a code-change stage exceeds 1000 changed lines.
- Increment one minor step when testing reaches a meaningful stage breakthrough.
- Record the reason and evidence for every version change.

## History

| Version | Date | Reason |
|---|---|---|
| `v0.48` | 2026-11-11 | Separate original 2D preview from on-demand STEP loading, cache prepared previews, recover local 3D rendering when viewer is unavailable, and register Capture/Allegro library paths. Include the Capture menu fix in rebuilt Windows installers. Validation details: docs/releases/v0.48.0.md. |
| `v0.47` | 2026-11-10 | Windows candidate r7 rebuilt after fixing concurrent STEP preview publication on Windows; signed update, installer lifecycle, Capture/Allegro evidence and performance acceptance were renewed. Production feed and Cadence 17.2 native host remain external prerequisites. |
| `v0.46` | 2026-11-09 | Completed the 478-leaf electronic category matrix with resumable isolated native preparation and truthful source/conversion failure records; the generated ledger and Chinese report preserve the verification limits. |
| `v0.45` | 2026-11-09 | Added the official electronic leaf-category conversion matrix, resumable isolated native builds, independent Capture/Allegro readback and evidence validation. Fixed a proven shared STEP-cache partial-write race and Capture's native property-length crash by preserving oversized metadata in hashed sidecar reports. Full matrix results and verification limits are recorded in 独立分类验收记录. |
| `v0.44` | 2026-11-09 | User-authorized native RefDes annotations now use verified original component prefixes with field/raw-value evidence, without changing source geometry or STEP. Native C2053170 U* and C25804 R* readback, STEP mapping and final PSM compilation passed; native pads/graphics equal the v0.43 readback. 141 backend, 61 plugin and 27 panel tests passed. |
| `v0.43` | 2026-11-09 | Catalog imports now convert original symbol/pad/graphic records, retain original STEP bytes and source transforms, record precision and unsupported data, and omit invented geometry. Independent raw-source and native Allegro readback agree within 0.000038 mm. Missing source RefDes prevents PSM compilation and is explicitly reported as incomplete, without drawing a replacement. |
| `v0.42` | 2026-11-09 | Verified actual Capture CEF search, nonblank symbol/footprint/STEP previews, STEP motion and native PlaceNew. Added SVG rendering, wide embedded-view fallback and official MDI reactivation; protected destination restoration across dirty-title changes. Correct numeric-device designator enabled separate native placement/save/reopen in a fresh independent project. Fixed overlapping pin labels, body fill and reference display. Physical mouse commit remains unverified. |
| `v0.41` | 2026-11-08 | Added a Capture CEF dock, page filters, actual symbol/footprint previews and STEP mesh preview. Isolated native Dbo sessions generated and reopened new C25804 and STM32F031 test designs with verified pin/property readback. GUI mouse placement remains pending. |
| `v0.40` | 2026-11-08 | Recovered the later v0.39 conversion backend; added native Capture browser, live catalog provider, placement bridge and independent test project. Native Allegro footprint/STEP mapping smoke passed; live Capture placement remains unverified. |
| `v0.1` | 2026-07-31 | Planning baseline: project plan, findings, and progress files created. |
| `v0.2` | 2026-07-31 | Initial MVP implementation stage: Python CLI, parser, IR, generators, reports, sample fixtures, and tests. |
| `v0.3` | 2026-07-31 | Stage testing breakthrough: all unit tests pass and demo conversion completes with zero validation errors/warnings. |
| `v0.4` | 2026-07-31 | Cadence batch breakthrough: verified `allegro_batch create_sym -p` creates a `.psm` from a real Cadence sample `.dra`, then added generated symbol build plans and batch compile scripts. |
| `v0.5` | 2026-07-31 | Allegro IR-driven build breakthrough: generated per-footprint SKILL/replay scripts from IR and verified Cadence 17.4 creates a real `.pad`, `.dra`, and `.psm` for the synthetic `RES_0603` fixture. |
| `v0.6` | 2026-07-31 | Through-hole/slot padstack breakthrough: parsed slot metadata, generated slot-aware padstacks, and verified Cadence 17.4 creates real `.pad`, `.dra`, and `.psm` for the synthetic `CONN_SLOT` through-hole/slot fixture. |
