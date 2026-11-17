# warehouse-models

3D assets for the digital-twin app, written in [sand3d](https://github.com/Sandflow-ai/sand3d).
One file per model in `models/`. On every push to `main`, GitHub Actions builds them all and publishes
them to Supabase Storage under `warehouse/<hash>/`, then registers the version in the `model_sets` table.

## Add a model
Create `models/<name>.py` with `build()` returning a sand3d root. That's it: it is built as `<name>.glb`
and appears in `layout.json`'s asset list. (Use `build_json(assets)` for non-mesh data.)

## Local
    python3.13 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python tools/build.py                 # -> dist/
    python tools/build.py --only robot    # one model

## One-time setup
1. Run `supabase/schema.sql` in the Supabase SQL editor.
2. GitHub repo -> Settings -> Secrets and variables -> Actions: add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
   (service_role key, Project Settings -> API). Never use this key in the browser.
# Python Asset Generation Guide (sand3d) — Warehouse Digital Twin

> **Audience:** an LLM (or engineer) implementing the **Python / sand3d** half of a WebGPU warehouse digital twin.
> **Your job:** generate the 3D assets, the layout/instance data and the publishing pipeline.
> **Not your job:** React, Three.js, the telemetry server, point clouds, the Node mesh optimizer (other agents own those — see §12).
> Read §3 (contracts) before writing any code. Everything in this document marked **VERIFIED** was run and observed; anything marked **TODO-VERIFY** is an assumption you must check and report.

---

## 0. TL;DR

1. Start from the existing repo (`warehouse-models.zip`, layout in §2). Keep everything that works.
2. Work through **WP1 → WP8** (§6) in order. Each work package has acceptance criteria; do not start the next until the current one passes `tools/validate.py` in CI.
3. Output of the pipeline (`dist/`, §7) is uploaded to Supabase Storage under a content-hash folder and registered in the `model_sets` table. **The frontend editor already reads this** — do not break the contracts in §3.
4. New this round: **LOD variants that are instancing-ready**, an **extended static asset catalog**, a **scalable layout generator (≥100,000 static instances)** emitting **binary instance buffers + spatial chunks + a robot lane graph**, a **manifest**, and a **validation harness**.
5. Finish with the report in §13.

---

## 1. Context — what the whole system is

```
┌────────────────────────── YOU ──────────────────────────┐        ┌──────── OTHER AGENTS ────────┐
│ Python + sand3d                                         │        │                              │
│  models/*.py ──► dist/*.glb (articulated, for editor)   │        │ Node optimizer (Draco/KTX2/  │
│              ──► dist/lod/*.glb (single-mesh LODs)      │──────► │  meshopt) reads dist/        │
│  layout gen  ──► dist/layout.json, instances/*.bin,     │        │                              │
│                  chunks.json (lanes, fleet)             │        │ Fleet renderer (WebGPU,      │
│  manifest    ──► dist/manifest.json                     │──────► │  2000 robots in 3 draw calls)│
│  GitHub Action ► Supabase Storage + model_sets row      │        │ Editor (React/Three, exists) │
└─────────────────────────────────────────────────────────┘        │ Telemetry server + worker    │
                                                                   └──────────────────────────────┘
```

The end product is a browser digital twin of a robotic warehouse: **≈2,000 dynamic robots** driven by **40k telemetry msgs/s**, **100k+ static assets** (racks, totes, columns…), 3-tier LOD (high < 15 m, medium 15–50 m, 12-triangle box > 50 m), `InstancedMesh` rendering, and a LiDAR point-cloud layer. The renderer can only hit those numbers if the assets you produce follow strict rules: **one mesh + one material per LOD** for anything instanced, exact bounding boxes, stable names, and data in compact binary form. That is why this guide is strict.

Two consumers of your output exist today:

| Consumer | Uses | Status |
|---|---|---|
| **Editor** (React + Three.js, `/work` page) | articulated `*.glb` + `layout.json` (v1 shape) | **Exists, in use. Must keep working.** |
| **Fleet renderer** (new) | `lod/*.glb`, `instances/*.bin`, `chunks.json`, `manifest.json`, lane graph | Being built by another agent against the specs in this guide. |

---

## 2. Current state of the repo (starting point)

```
warehouse-models/
├─ models/            one file per model; each defines build() -> sand3d root   (rack, tote, robot, conveyor, station)
│                     layout.py defines build_json(assets) -> dict              (layout.json)
├─ lib/common.py      mat(ob, kind, color, **kw) helper + palette constants
├─ lib/supabase_io.py upload_file / register_version / version_exists (urllib only, retries)
├─ tools/build.py     discovers models/*.py, builds into dist/, content-hashes, uploads, registers
├─ supabase/schema.sql   storage bucket `models` (public) + table `model_sets` (RLS: public read, service-role write)
├─ .github/workflows/models.yml   Python 3.13; build on push/PR; publish on main
└─ requirements.txt   sand3d>=1.6
```

**Runner convention (existing):** a file in `models/` exposing `build()` becomes `dist/<file>.glb`; one exposing `build_json(assets)` becomes `dist/<file>.json`. The output set is hashed (sha256, first 8 hex) and uploaded to `warehouse/<hash>/…`; then one row is inserted into `model_sets(name, version, base_path, files)` **last**, so the app never sees a half-uploaded set. Identical code → identical hash (**VERIFIED**: two consecutive builds gave the same hash).

### 2.1 Baseline measurements of the current assets (VERIFIED with `tools/inspect_glb.py`)

| Asset | Size X × Y(up) × Z (m) | Triangles | Mesh nodes | Materials | Joints |
|---|---|---|---|---|---|
| `rack`     | 1.20 × 3.00 × 0.90 | 228   | 19 | 3 | – |
| `tote`     | 0.60 × 0.30 × 0.40 | 160   | 1  | 1 | – |
| `robot`    | 0.74 × 0.18 × 0.58 | **7,968** | 12 | 9 | `wheel_l_spin`, `wheel_r_spin` (continuous), `lift_slide` (prismatic 0–0.3 m) |
| `conveyor` | 2.01 × 0.735 × 0.68 | 600   | 10 | 5 | – (mesh `belt`) |
| `station`  | 1.00 × 1.474 × 0.70 | 968   | 10 | 7 | – (lamp meshes `tower_*`) |

Per-mesh breakdown of the robot (`--meshes`) shows where the robot's triangles go — **this is the first optimisation target**:

```
lidar_ring  3024 tris   caster0 1980   caster1 1980   wheel_l 252   wheel_r 252   lidar 232   chassis 188   (+5 boxes × 12)
```
`lidar_ring` (a torus) and the two caster **spheres** are ≈88 % of the triangles for almost no visual value. Replacing them (flat disc / short cylinder / box) takes LOD0 under budget immediately (budgets in §7.2).

### 2.2 Current `layout.json` (v1) shape — must stay readable by the editor

```jsonc
{
  "meta": { "units": "metres", "axes": "glTF (Y up, Z toward viewer)", "origin": "asset floor-centre",
            "bounds": { "x": [-8.25, 8.75], "z": [-3.0, 10.0] } },
  "assets":  { "rack": "rack.glb", "tote": "tote.glb", "robot": "robot.glb", "conveyor": "conveyor.glb", "station": "station.glb" },
  "joints":  { "robot": ["wheel_l_spin","wheel_r_spin","lift_slide"], "station": ["tower_red","tower_amber","tower_green"] },
  "instances": {
    "rack":     [ { "id": "R0-0", "position": [-4.55, 0, 0], "rotationY": 0 }, … ],        // rotationY is DEGREES
    "tote":     [ { "id": "T-R0-0-0-1", "rack": "R0-0", "level": 0, "position": [x, y, z] }, … ],
    "conveyor": [ { "id": "CV-1", "position": [7.35, 0, 3.75], "rotationY": 90, "beltSpeedMps": 0.4 } ],
    "station":  [ { "id": "ST-1", "position": [-7.55, 0, -1.4], "rotationY": 90 } ]
  },
  "robots": [ { "id": "AMR-1", "start": [-5.75, 0, 1.25], "loop": [[-5.75,1.25],[5.75,1.25],[5.75,-1.0],[-5.75,-1.0]], "speedMps": 1.2 }, … ],
  "telemetry_example": { … }
}
```
(Demo numbers: 4 rows × 8 bays × 5 levels × 2 totes with a deterministic fill pattern; row pitch 2.5 m, bay pitch 1.3 m.)

---

## 3. Contracts — DO NOT BREAK (the frontend depends on every line)

### 3.1 Units, axes, origin, orientation
- Author in **millimetres** (`s3d.units("mm")`); the exporter writes **metres** into glTF. (**VERIFIED**: 600 mm tote → 0.6 m.)
- Exported coordinates are **glTF**: **X right, Y up, Z toward the viewer**. sand3d's authoring frame is Blender-like **Z-up** with the front at **−Y**; the exporter maps Blender **Z→glTF Y** and Blender **−Y→glTF +Z**. So *sand3d +Y becomes glTF −Z.* (**VERIFIED** by joint positions: a wheel authored at sand3d `y=-290` lands at glTF `z=+0.29`.)
- **Origin = floor-centre** of the footprint (x/z centred, y = 0 at the floor). Check with `bounds_min.y ≈ 0` and the bbox centred on x/z.
- **Forward = glTF +X** for anything that drives (robot lidar is at +X). Yaw is rotation about **+Y**, radians in binary data / degrees in JSON, positive = counter-clockwise seen from above.

### 3.2 Node and joint names the frontend looks up by name (`getObjectByName`)

| Asset file | Required node names |
|---|---|
| `robot.glb` | joint nodes `wheel_l_spin`, `wheel_r_spin` (continuous), `lift_slide` (prismatic, 0–0.30 m); mesh node `lidar`, `status_strip`; `wheel_*` joints must keep the prefix `wheel` (runtime keys off it) |
| `station.glb` | `tower_red`, `tower_amber`, `tower_green` — **three separate emissive meshes** (telemetry switches each lamp's emissive intensity) |
| `conveyor.glb` | `belt` (the mesh a belt shader/texture scrolls on) |
| `rack.glb`, `tote.glb` | `rack`, `tote` (root names) |

Never rename these. Adding nodes is fine; removing/renaming is a breaking change.

### 3.3 How sand3d writes joints into glTF (**VERIFIED**)
Each joint becomes a **named empty node** whose `extras` hold:
```json
{ "s3d_joint": { "name": "lift_slide", "type": "prismatic", "value": 0.0, "axis": [0,1,0], "lower": 0.0, "upper": 0.3 } }
```
Rules the frontend relies on:
1. The **moving part is a child** of the joint node. Rotating/translating the joint node moves it.
2. `axis` is expressed in the **joint node's local space**, and the node can carry its own **rest rotation** (e.g. `wheel_l_spin` has a 90° rest rotation about X). Therefore motion is applied *relative to the rest pose*. **Do not "normalise" these rotations away** — you would silently break wheel animation.
3. Joint-node translations and the prismatic limits are in **different units**: node-local values are in millimetres under a root node scaled by **0.001** (**VERIFIED**: robot root `scale = [0.001, 0.001, 0.001]`, `wheel_l_spin` translation `z = 250`), while `lower/upper` are metres. The frontend divides by the parent's world scale. Do not change the root-scale convention.
4. **TODO-VERIFY:** for **revolute** joints (`range=(deg_lo, deg_hi)` in `s3d.joint`) check whether `lower/upper` in `extras` are **radians or degrees** (no current asset has a limited revolute joint; the editor assumes radians). Build a tiny throw-away model with a revolute joint, inspect it, and report the finding in §13.

### 3.4 Labels
Object **labels** (ids in `layout.json` such as `R0-3`, `CV-1`, `AMR-2`) are referenced by user-written behaviour scripts. **No spaces**, stable across builds, deterministic. Keep the existing id schemes (`R<row>-<bay>`, `T-R<row>-<bay>-<level>-<slot>`, `CV-n`, `ST-n`, `AMR-n`).

### 3.5 The Supabase `model_sets.files` rule (important)
The editor's palette lists **every entry of `files` whose value ends in `.glb`** as a placeable model. Therefore:
- `files` must contain **only**: the existing articulated models (`rack`, `tote`, `robot`, `conveyor`, `station`, + any *new* palette-worthy asset), `layout` (`layout.json`) and `manifest` (`manifest.json`).
- **LOD files, instance buffers and chunk files must NOT appear in `files`.** They are uploaded to the same `base_path` (in sub-folders) and are discovered via `manifest.json`. This keeps the editor unchanged.

### 3.6 Determinism
No `random`, `time`, `uuid`, dict-order or filesystem-order dependence. Use `random.Random(seed)` with a fixed seed for any procedural fill. Same inputs ⇒ byte-identical outputs ⇒ same set hash. CI must verify this (WP1).

### 3.7 Backwards compatibility of `layout.json`
The `demo` preset must reproduce the **v1 shape and the same ids/positions** as today (golden-file test, WP5). v2 additions are **additive fields only**.

---

## 4. sand3d crash course (what you need; read the official `docs/api.md` for the rest)

Repo: <https://github.com/Sandflow-ai/sand3d> · `pip install sand3d` · **requires Python 3.11 or 3.13 (3.12 is excluded)** · depends on Blender's `bpy` (large wheel).

**Core idea:** code builds solids; makers take a name and return the solid; edits return it. Edges/faces are addressed by names (`top bottom side rim seam start end cut`, tool names, `plate.top`). Units: `s3d.units("mm")` first.

Calls used in this repo (all **VERIFIED** to work):

| Purpose | Call |
|---|---|
| Primitives | `s3d.box(name, w, d, h)`, `cylinder(name, diameter, height)`, `sphere(name, diameter)`, `cone(name, d1, h, d2=…)`, `torus(name, major_d, minor_d)`, `tube(name, outer_d, inner_d, h)` |
| Place | `s3d.at(ob, x=, y=, z=, rx=, ry=, rz=, on=frame, anchor=)` — **solids start at z=0 (bottom) and are centred in x/y**; `z` sets the bottom |
| Edit | `s3d.fillet(ob, r, edges="top"/"side")`, `s3d.shell(ob, thickness, open="top"/"bottom")`, `s3d.scale(ob, k)` |
| Group | `s3d.group(name, *parts)`, `s3d.assembly(name, *parts, surface=Frame)` |
| Frames | `s3d.bbox(ob, "+Z")` / `"+Y"` / `"-Y"` / `"+Y-Z"` (centre of that side/edge), `s3d.Frame((x,y,z))`, `s3d.ring(n, radius)` (frames around a circle) |
| Joint | `s3d.joint(name, child, on_frame, kind="continuous"/"prismatic"/"revolute", axis="+Y", range=(lo,hi))` — the frame is read on the **parent**, never on the child |
| Material | `s3d.material(ob, kind, color=, emission=)`; kinds seen: `plastic painted_metal brushed_steel steel chrome rubber glossy_plastic walnut ceramic fabric velvet concrete light` (`light` + `emission=` makes an emissive mesh) |
| Scene | `s3d.empty_scene()` (call **before every export**), `s3d.export(root, "path.glb")` |
| Joins | `s3d.join(...)` (boolean union), `s3d.merge(...)` (no boolean) — **TODO-VERIFY** which yields a single mesh/primitive/material when exported (needed for LODs, WP2) |

**Patterns that worked**
- A cylinder is built along **Z**. To lay it along **Y**: `at(cyl, rx=90, y=Y0)` extends toward **−Y** from `Y0`; `rx=-90` extends toward **+Y**. Always confirm with a preview render.
- Pivot for a side-mounted wheel: `bbox(chassis, "+Y")` is the middle of the +Y side — set the wheel's centre height equal to the chassis mid-height so the pivot lies on the wheel axis.
- Open boxes: `shell(box(...), t, open="top")`.
- Booleans (`cut`, `join`) are slower; avoid inside loops. Plain `merge`/`group` is cheap.

**Workflow rule:** after every geometry change run `tools/inspect_glb.py` (numbers) *and* `tools/preview_glb.py` (eyes). Geometry bugs (a part 1 m off, a flipped axis) are invisible in code review.

---

## 5. Environment & tooling

### 5.1 Setup (Linux/macOS; Windows works too)
```bash
python3.13 -m venv .venv && source .venv/bin/activate     # NOT 3.12
pip install -r requirements.txt                           # first install pulls bpy (large, minutes)
python tools/build.py --out dist                          # sanity: builds all models
```
- If your sandbox limits a command to ~5 minutes, run installs/builds in the background (`nohup … &`) and poll the log.
- Harmless log lines to ignore: `Draco is not available…`, `MeshOptimizer is not available…`, `EGL Error … EGL_BAD_MATCH` on headless machines.
- Shell note: do not rely on `{a,b}` brace expansion in `mkdir` (some environments run `/bin/sh`). Create directories one by one.

### 5.2 The two verification tools (full, tested source in Appendices B and C)
- **`tools/inspect_glb.py`** (stdlib only): node tree, joints (with axes/limits), meshes, materials, **triangle counts** (`--meshes` for per-mesh), **world-space bounding box**, `--json` for machines. **VERIFIED** on all five current assets.
- **`tools/preview_glb.py`**: renders several GLBs side by side to a PNG with Blender's workbench engine (headless). **VERIFIED** (tote | robot | rack).

You will turn `inspect_glb.py` into the engine of `tools/validate.py` (WP1).

---

## 6. Work packages

### WP1 — Verification harness (do this first)
**Goal:** make every later change safe and measurable.

Deliver:
1. `tools/inspect_glb.py`, `tools/preview_glb.py` (Appendices), plus `tools/validate.py` that runs the rules in §10 over `dist/` and exits non-zero on failure with a readable table.
2. `tools/budgets.py`: a single dict of per-asset budgets (triangle caps per LOD, max materials, expected size ±tolerance, required node/joint names). Validation reads it; models never hard-code budgets.
3. `tests/` (pytest): (a) determinism — build twice, compare hashes; (b) golden test for the `demo` layout; (c) `validate` unit tests using the real GLBs.
4. CI: add `pytest` and `python tools/validate.py dist` steps **before** publishing; upload `dist/` as an artifact on every run.

Acceptance: introducing a deliberate break (rename `lift_slide`, exceed a triangle budget) makes CI fail with a clear message.

### WP2 — LOD variants for the five existing assets
**Goal:** every asset gets three instancing-ready LODs.

**Convention:** a model module may additionally define
```python
PROXY_MM = (w, d, h)            # exact bounding-box size in mm (footprint centred on x/y, bottom at z=0)
def build_lod(lod: int): ...    # lod in (0, 1) -> sand3d root, SINGLE mesh, rest pose, no joints
```
`tools/build.py` then writes `dist/lod/<name>.lod0.glb`, `.lod1.glb` and **auto-generates `.lod2.glb`** from `PROXY_MM` via a helper `lib/lod.py: proxy_box(name, w, d, h, color)` (a plain box → **exactly 12 triangles**). Keep the existing `build()` untouched — it stays the articulated editor asset.

**Instancing-ready rules for LOD files** (each one becomes a single `InstancedMesh` → one draw call):
- **Exactly one mesh node, one primitive.** Material count per §7 table (robot: **1**).
- **No joints, no animation, rest pose** (robot: lift at 0, wheels at 0). No extras except `{"wh": {"asset": "<name>", "lod": N}}`.
- Origin/orientation per §3.1. **Bounding box identical to the articulated asset within ±2 %** (so swapping LODs never pops).
- Node name `"<asset>_lod<N>"`. Normals present. No textures.
- Colour variation is done at runtime with per-instance colour, so use **one neutral light material** (e.g. `painted_metal`, `#e8e6f5` for robots) — do **not** bake multiple colours.
- Triangle budgets: §7.

How to get one mesh: first try `s3d.merge(...)`/`s3d.join(...)` and **inspect the exported file** (`primitives == 1`, `materials_used == 1`). If sand3d cannot export that, fall back to `bpy` (already installed as sand3d's dependency): import the generated model, `bpy.ops.object.join()`, assign one material, export. Report which route you used (§13).

**Worked example — robot LOD0** (from the §2.1 numbers): drop `lidar_ring`, replace the two caster spheres by short cylinders or boxes, keep chassis/wheels/deck/lidar as merged simple solids → roughly 700–1,200 triangles (cap 1,500). **LOD1** (cap 300): chassis + deck + two box wheels, no lidar detail ≈ 100–200 triangles.

Acceptance: `validate.py` passes the LOD rules for all five assets; `preview_glb.py lods.png dist/lod/robot.lod{0,1,2}.glb` shows visually consistent silhouettes (attach the PNG to your report).

### WP3 — Fleet robot variants
**Goal:** the fleet renderer needs a few robot *types* sharing one pipeline.

- Add `robot_amr` (current robot, WP2 applied), `robot_forklift` (longer, ~1.2 × 0.9 m, taller), `robot_shuttle` (low, flat, rack-shuttle). Each: articulated `*.glb` (joints as in §3.2 where they make sense; wheels named `wheel_*`) **and** `lod/*.lod0/1/2.glb`.
- Each exposes `PROXY_MM` and a `CARRY_HEIGHT_M` constant recorded into `manifest.json` (the simulator uses 0.156 m for `robot`).
- Do not add them to `files` unless they should appear in the editor palette (they should: they have articulated `.glb`s).

### WP4 — Extended static catalog (for the 100k static instances)
Create assets **with LODs** (articulated `.glb` only if palette-worthy; otherwise LOD files only):

| Asset | Approx. size (m) | lod0 tris | Notes |
|---|---|---|---|
| `pallet` | 1.2 × 0.15 × 0.8 | ≤ 300 | wooden slats (boxes) |
| `column` | 0.4 × 8.0 × 0.4 | ≤ 60 | structural, square section |
| `fence` | 2.0 × 1.2 × 0.1 | ≤ 200 | safety fence panel, yellow |
| `wall_panel` | 3.0 × 6.0 × 0.15 | ≤ 24 | cladding, flat |
| `floor_tile` | 2.0 × 0.02 × 2.0 | ≤ 12 | markings via 2 materials max; marker-line variant optional |
| `light_fixture` | 1.2 × 0.1 × 0.3 | ≤ 80 | emissive strip (`light` material) |
| `charging_station` | 0.6 × 1.0 × 0.6 | ≤ 300 | one emissive indicator mesh named `charge_led` |
| `rack_endcap` | 0.1 × 3.0 × 0.9 | ≤ 60 | optional, for aisle ends |

Rules: same contracts as §3; each has `PROXY_MM`; budgets live in `tools/budgets.py`; one preview image per asset in your report.

### WP5 — Scalable layout generator
**Goal:** a parametric, deterministic generator replacing the hard-coded `layout.py`, with presets and compact outputs.

CLI: `python tools/gen_layout.py --preset demo|medium|large [--seed 1] [--robots N] --out dist`
(also callable from `build.py`).

| Preset | Racks | Robots | Static instances | Output |
|---|---|---|---|---|
| `demo` | 32 (4 rows × 8 bays) | 3 | 248 (32 racks + 214 totes + conveyor + station) | **v1-compatible JSON only** — must equal today's ids/positions (golden test) |
| `medium` | ≈ 400 | 200 | ≈ 25k | JSON for named things + binary for bulk |
| `large` | ≥ 5,000 | **2,000** | **≥ 100,000** across ≥ 6 asset types | binary + chunks |

Example composition for `large` (tune to reach ≥ 100k, keep deterministic): ~6k racks, ~55–65k totes (≈ 70–80 % fill, seeded), columns on a 6 m grid, floor tiles over the whole floor, light fixtures per bay, fences around robot-free zones, pallets in the receiving area.

Rules: aisles ≥ 1.6 m, bay pitch 1.3 m, row pitch 2.5 m (as today); racks never overlap; every robot start lies on a lane node.

Outputs: `layout.json` (v2, §8), `instances/<asset>.bin`, `chunks.json`, **lane graph + fleet** (inside `layout.json`). Also add `tools/layout_report.py` printing counts per asset, bounds, bytes and chunk statistics.

Acceptance: §10 layout rules pass; `large` generates in < 60 s on a laptop-class CPU and total instance data < 10 MB.

### WP6 — Manifest + publishing
- Write `dist/manifest.json` (§9) describing every asset, its LOD files, size, triangle counts, joints, materials, and where instance/chunk data lives.
- Update `tools/build.py`: **walk `dist/` recursively** for hashing and upload (preserving relative paths such as `lod/robot.lod0.glb`); register `files` **only** per §3.5. Keep `upload → register last` ordering and `version_exists` idempotency.
- `Cache-Control: max-age=31536000, immutable` (paths are content-hashed).
- Add `--only <asset>` for fast local iteration (build + validate that asset; never uploads).

### WP7 — Handoff to the Node optimizer (interface only)
A different agent runs `@gltf-transform` (Draco/meshopt/KTX2). You provide a stable interface:
- **Input:** `dist/**/*.glb` exactly as produced. **Output expected:** `dist-opt/` with the same relative paths.
- **Constraints you must document and test for in `validate.py`:** articulated assets (anything with `s3d_joint` extras) **must not** be run through `flatten`/`join`/`prune` of named nodes — it would delete joint nodes. LOD files (single mesh) may be fully optimised.
- Add a CI job skeleton (Appendix E) with the optimizer step marked `TODO(other-agent)`; keep the pipeline green without it.

### WP8 — Documentation
`README.md` at repo root: setup, how to add a model (with the LOD convention), budgets, contracts (copy §3), commands, troubleshooting. Update `docs/`-style notes if you add tools. Keep it short and accurate.

---

## 7. Specs

### 7.1 `dist/` layout
```
dist/
├─ rack.glb tote.glb robot.glb conveyor.glb station.glb  (+ new palette assets)   # articulated / full detail
├─ layout.json                                                                    # v1-compatible + v2 fields
├─ manifest.json
├─ chunks.json                                                                    # large/medium presets
├─ lod/<asset>.lod0.glb  <asset>.lod1.glb  <asset>.lod2.glb
└─ instances/<asset>.bin                                                          # large/medium presets
```

### 7.2 LOD budgets (enforced by `tools/budgets.py`)

| Asset | LOD0 tris (cap) | LOD1 tris (cap) | LOD2 | Materials LOD0 / LOD1 / LOD2 |
|---|---|---|---|---|
| robot (all types) | 1,500 | 300 | **12** | **1 / 1 / 1** |
| rack | 300 | 120 | 12 | ≤3 / ≤2 / 1 |
| tote | 200 | 60 | 12 | 1 / 1 / 1 |
| conveyor | 700 | 100 | 12 | ≤2 / 1 / 1 |
| station | 1,000 | 100 | 12 | ≤2 / 1 / 1 |
| WP4 assets | per table in WP4 | ≤ 35 % of LOD0 | 12 | ≤2 / 1 / 1 |

(The articulated `robot.glb` is *not* subject to these caps, but WP2 should still remove the 3,000-triangle torus and the 2,000-triangle caster spheres from it — report the new total.)

LOD2 colour = the asset's dominant colour; it is a flat box exactly matching the LOD0 bounding box.

### 7.3 Size/budget limits
`lod*.glb` ≤ 150 KB each (robot LOD0 ≤ 300 KB); `layout.json` ≤ 3 MB for any preset (bulk goes to `.bin`); total `dist/` for `large` ≤ 15 MB before Draco.

---

## 8. Layout v2 specification

### 8.1 `layout.json` additions (all optional for the `demo` preset except `format`)
```jsonc
{
  "format": 2,
  "meta": { "units": "metres", "axes": "glTF (Y up, Z toward viewer)", "yaw": "radians about +Y, CCW from above (binary); degrees in JSON instances",
            "tileSize": 32, "bounds": { "x": [..], "z": [..] }, "preset": "large", "seed": 1 },
  "assets": { "rack": { "glb": "rack.glb", "lods": ["lod/rack.lod0.glb","lod/rack.lod1.glb","lod/rack.lod2.glb"],
                        "instances": "instances/rack.bin", "count": 6144 }, … },
  "instances": { /* v1 lists, ONLY for small named things (demo: everything; large: conveyors, stations) */ },
  "lanes": {
    "nodes": [ { "id": 0, "x": -40.5, "z": 1.25 }, … ],
    "edges": [ { "a": 0, "b": 1, "oneWay": false }, … ]           // aisle centre-lines + cross aisles; graph must be connected
  },
  "fleet": { "count": 2000, "idPrefix": "AMR-", "idPad": 4, "type": "robot_amr",
             "startNodes": [ 12, 47, … ],                          // length == count, lane node indices, deterministic
             "speedMps": 1.2 },
  "robots": [ /* v1 entries; demo preset only */ ]
}
```

### 8.2 Binary instance file `instances/<asset>.bin` (little-endian)
```
offset 0   : 4 bytes  ASCII "WHI1"
offset 4   : uint32   count
offset 8   : uint32   strideFloats   (= 5)
offset 12  : uint32   reserved (0)
offset 16  : count × strideFloats × float32 :  x, y, z, yawRadians, variant
```
- `variant` is a small integer stored as float (colour/model variant index, 0 = default).
- Records are **sorted by tile** (`cx = floor(x / tileSize)`, `cz = floor(z / tileSize)`, then by `cz`, `cx`, then original order) so each tile's instances are a **contiguous range**.
- Coordinates in metres, glTF axes. Record index is the instance's numeric id (string ids exist only for the few named objects in `instances`).

### 8.3 `chunks.json`
```jsonc
{ "tileSize": 32,
  "assets": { "rack": [ { "cx": -2, "cz": 0, "start": 0, "count": 96, "min": [x,y,z], "max": [x,y,z] }, … ], … } }
```
`start` is a **record index** into that asset's `.bin`; `min/max` are the world AABB of the tile's instances (including asset size) for frustum culling.

---

## 9. `manifest.json` specification
```jsonc
{
  "format": 1,
  "set": "warehouse",
  "generator": { "sand3d": "<version>", "python": "3.13" },      // versions only — NO timestamps, hostnames or paths (the manifest is hashed)
  "assets": {
    "robot": {
      "glb": "robot.glb",
      "lods": { "0": "lod/robot.lod0.glb", "1": "lod/robot.lod1.glb", "2": "lod/robot.lod2.glb" },
      "sizeM": [0.74, 0.18, 0.58],
      "triangles": { "articulated": 800, "0": 760, "1": 160, "2": 12 },
      "materials": { "0": 1, "1": 1, "2": 1 },
      "joints": [ { "name": "wheel_l_spin", "type": "continuous" }, … ],
      "origin": "floor-centre", "forward": "+X",
      "carryHeightM": 0.156
    }, …
  },
  "layout": "layout.json",
  "instances": { "rack": "instances/rack.bin", … },
  "chunks": "chunks.json"
}
```
Everything in the manifest must be **measured from the generated files** (use `inspect_glb.inspect()`), never typed by hand. **Do not put timestamps or hostnames in any file that is hashed** (breaks determinism).

---

## 10. Validation rules (`tools/validate.py`)

| ID | Rule |
|---|---|
| V01 | Every expected file exists; every `.glb` parses as glTF 2.0 binary |
| V02 | **Units sanity:** bbox size within ±15 % of `budgets.py` expected size (catches mm/m mistakes) |
| V03 | **Origin:** `min Y` within 1 mm of 0; bbox centre on X/Z within 1 % of the footprint centre (unless the asset declares an offset) |
| V04 | Required node names present (§3.2); required joints present with the right `type` |
| V05 | No NaN/inf in accessor min/max |
| V06 | **LOD files:** exactly 1 mesh node, 1 primitive, no joints, extras only `wh` |
| V07 | Triangle count ≤ cap (§7.2); LOD2 == 12 |
| V08 | Material count ≤ cap (§7.2) |
| V09 | LOD bbox equals articulated bbox ±2 % (all three axes), LOD2 exactly the LOD0 bbox ±1 mm |
| V10 | File-size limits (§7.3) |
| V11 | Determinism: second build ⇒ identical hashes |
| V12 | `layout.json`: ids unique; all instance `asset`s exist in the manifest; positions inside `meta.bounds`; demo preset equals the golden v1 |
| V13 | Binary files: magic/header/size consistent (`16 + count·stride·4 == size`); tiles contiguous; every record inside its tile; counts equal `assets[*].count` |
| V14 | `chunks.json`: ranges cover each `.bin` exactly once; AABBs contain their instances |
| V15 | Lane graph connected; every `fleet.startNodes[i]` is a valid node; no rack overlaps (AABB test) |
| V16 | `manifest.json` numbers match the files (re-measure) |
| V17 | Articulated assets keep **named nodes** with `s3d_joint` extras (guards the Node optimizer hand-off) |

---

## 11. Pitfalls & lessons (collected while building the existing pipeline)

1. **Python 3.12 is unsupported** — `pip install sand3d` fails with "no matching distribution". Use 3.13 (or 3.11).
2. The `bpy` install and first run are slow; cache pip in CI (`cache: pip`) and run long commands in the background with polling.
3. Call `s3d.empty_scene()` **between** exports and set `s3d.units("mm")` again; otherwise objects accumulate and names collide.
4. Cylinders/cones are built along **Z** — rotating them onto another axis changes the extension direction (§4). Verify with a render, not by reasoning.
5. Joint frames are read on the **parent**; using a frame from the child fails. The pivot's height comes from the frame, so size the wheel to match.
6. The exporter puts rest rotations on joint nodes (§3.3). Resist "cleaning" them.
7. Spheres and tori are triangle-hungry (`caster` = 1,980 tris each; `lidar_ring` = 3,024). Prefer boxes/cylinders for small details and always run `--meshes`.
8. Never use unseeded randomness; never iterate sets/dicts in a way that depends on hash order; sort before writing.
9. Don't hand-type numbers into the manifest/budgets — measure them.
10. A change that looks harmless in code (an extra `at(..., z=...)`) can move a part 1 m; the **bounding-box check (V02/V03/V09)** exists to catch exactly this.
11. Emissive "lights" are meshes with the `light` material; the telemetry layer toggles their emissive intensity, so keep each lamp a separate mesh with a stable name (station).
12. Headless Blender prints EGL warnings — harmless.

---

## 12. Out of scope & coordination points

You do **not** build: the WebGPU fleet renderer, Web Worker/WebSocket telemetry, interpolation, ECS, spatial hash, point cloud, KTX2/Draco compression, Node mock server, React UI.

Things other agents will rely on / you must tell them (put in the README):
1. **Frontend `meshAssets()`** lists every `.glb` in `files` as a palette item → obey §3.5 (LODs only via the manifest).
2. **Node optimizer** must not flatten articulated assets (V17).
3. **Fleet renderer** reads `manifest.json` → `lods`, `sizeM`, `carryHeightM`; `chunks.json` + `instances/*.bin` for static data; `layout.json.lanes/fleet` for robot spawn.
4. **Telemetry server** uses `lanes` to generate plausible robot paths.
5. **Open question to answer in your report:** revolute joint limit units in glTF extras (§3.3 TODO-VERIFY).

---

## 13. Definition of done & report template

Done = CI green (pytest + `validate.py` + build), `dist/` for `demo`, `medium` and `large` generated, all V01–V17 passing, README updated.

Return a report with:
```
## Result
- Work packages completed: WP1 … WP8 (✔/partial/✘ + one line each)
## Measurements (from inspect_glb, not estimated)
| asset | lod0 tris | lod1 | lod2 | materials | size m | articulated tris (before → after) |
## Generated data
| preset | static instances | assets | bin MB | json MB | chunks | gen time |
## Decisions & findings
- Single-mesh route used (merge/join/bpy) and why
- Revolute joint extras unit (radians/degrees) — evidence
- Anything in this guide that was wrong or ambiguous
## Preview images (lods.png, catalog.png)
## Known limitations / follow-ups for other agents
```

---

## Appendix A — Current `models/robot.py` (style reference, abridged)

```python
"""AMR with wheel/lift joints -> robot.glb"""
import sand3d as s3d
from lib.common import mat, VIOLET, CYAN, PINK, AMBER, RED, GREEN

def build():
    """Autonomous mobile robot: two drive wheels, casters, lifting deck, lidar."""
    chassis = mat(s3d.box("chassis", 700, 500, 100), "painted_metal", "#e8e6f5")
    s3d.fillet(chassis, 18, edges="side")
    skirt = mat(s3d.at(s3d.box("skirt", 740, 540, 14), z=0), "rubber", "#0a0912")
    deck = mat(s3d.at(s3d.box("deck", 640, 440, 40), z=100), "painted_metal", VIOLET)
    # wheels: cylinders along Y; the joint pivot is the middle of the chassis side, wheel centre height 50
    wheel_l = mat(s3d.at(s3d.cylinder("wheel_l", 100, 40), x=0, y=-290, z=50, rx=-90), "rubber", "#0a0912")
    wheel_r = mat(s3d.at(s3d.cylinder("wheel_r", 100, 40), x=0, y=290, z=50, rx=90), "rubber", "#0a0912")
    casters = [mat(s3d.at(s3d.sphere(f"caster{i}", 40), x=sx * 290, y=0), "steel", "#8a86a8") for i, sx in enumerate((-1, 1))]
    lidar = mat(s3d.at(s3d.cylinder("lidar", 70, 40), x=280, y=0, z=140), "glossy_plastic", "#0a0912")
    lidar_glow = mat(s3d.at(s3d.torus("lidar_ring", 70, 8), x=280, y=0, z=170), "light", CYAN, emission=4.0)   # 3,024 tris!
    platform = mat(s3d.at(s3d.box("platform_plate", 520, 380, 16), z=140), "brushed_steel", "#9a96b8")
    marker = mat(s3d.at(s3d.box("platform_marker", 80, 80, 4), z=156), "light", PINK, emission=2.5)
    lift = s3d.group("lift", platform, marker)
    eyes = mat(s3d.at(s3d.box("status_strip", 8, 300, 12), x=352, z=60), "light", GREEN, emission=3.0)
    s3d.joint("wheel_l_spin", wheel_l, s3d.bbox(chassis, "-Y"), kind="continuous", axis="+Y")
    s3d.joint("wheel_r_spin", wheel_r, s3d.bbox(chassis, "+Y"), kind="continuous", axis="+Y")
    s3d.joint("lift_slide", lift, s3d.bbox(deck, "+Z"), kind="prismatic", axis="+Z", range=(0, 300))
    return s3d.assembly("robot", chassis, skirt, deck, wheel_l, wheel_r, *casters, lidar, lidar_glow, lift, eyes)
```

## Appendix B — `tools/inspect_glb.py` (tested)

```python
#!/usr/bin/env python3
"""Inspect a .glb without any dependency: node tree, joints, meshes, materials, triangles, world bounds.

  python tools/inspect_glb.py dist/robot.glb            # human-readable report
  python tools/inspect_glb.py dist/robot.glb --meshes   # + triangles per mesh node (find what blows a budget)
  python tools/inspect_glb.py dist/robot.glb --json     # machine-readable (used by tools/validate.py)
"""
import json, struct, sys, math

def load(path):
    b = open(path, "rb").read()
    magic, version, _length = struct.unpack("<III", b[:12])
    if magic != 0x46546C67 or version != 2:
        raise SystemExit(f"{path}: not a glTF 2.0 binary")
    clen, ctype = struct.unpack("<II", b[12:20])
    assert ctype == 0x4E4F534A, "first chunk must be JSON"
    return json.loads(b[20:20 + clen])

def mat4_mul(a, b):  # column-major 4x4
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]

def node_matrix(n):
    if "matrix" in n:
        return list(n["matrix"])
    tx, ty, tz = n.get("translation", [0, 0, 0])
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    sx, sy, sz = n.get("scale", [1, 1, 1])
    r = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
         2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
         2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]
    return [r[0] * sx, r[1] * sx, r[2] * sx, 0, r[3] * sy, r[4] * sy, r[5] * sy, 0,
            r[6] * sz, r[7] * sz, r[8] * sz, 0, tx, ty, tz, 1]

def apply(m, p):
    return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
            m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]]

def inspect(path):
    g = load(path)
    nodes, meshes, acc = g.get("nodes", []), g.get("meshes", []), g.get("accessors", [])
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    roots = [i for i in range(len(nodes)) if i not in parent]

    def tris(mesh):
        t = 0
        for p in mesh["primitives"]:
            if p.get("mode", 4) != 4:
                continue
            t += (acc[p["indices"]]["count"] if "indices" in p else acc[p["attributes"]["POSITION"]]["count"]) // 3
        return t

    lo, hi = [math.inf] * 3, [-math.inf] * 3
    mesh_nodes, joints, materials_used, tri_total, prim_total, per_mesh = [], [], set(), 0, 0, []

    def walk(i, pm):
        nonlocal tri_total, prim_total
        n = nodes[i]
        m = mat4_mul(pm, node_matrix(n))
        j = (n.get("extras") or {}).get("s3d_joint")
        if j:
            joints.append(j)
        if "mesh" in n:
            mesh = meshes[n["mesh"]]
            mesh_nodes.append(n.get("name", f"node{i}"))
            per_mesh.append({"node": n.get("name", f"node{i}"), "triangles": tris(mesh),
                             "materials": sorted({p.get("material", -1) for p in mesh["primitives"]})})
            tri_total += tris(mesh)
            prim_total += len(mesh["primitives"])
            for p in mesh["primitives"]:
                if "material" in p:
                    materials_used.add(p["material"])
                a = acc[p["attributes"]["POSITION"]]
                mn, mx = a.get("min"), a.get("max")
                if mn and mx:
                    for cx in (mn[0], mx[0]):
                        for cy in (mn[1], mx[1]):
                            for cz in (mn[2], mx[2]):
                                q = apply(m, [cx, cy, cz])
                                for k in range(3):
                                    lo[k] = min(lo[k], q[k]); hi[k] = max(hi[k], q[k])
        for c in n.get("children", []):
            walk(c, m)

    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    for r in roots:
        walk(r, ident)
    size = [hi[k] - lo[k] for k in range(3)] if mesh_nodes else [0, 0, 0]
    return {
        "file": path, "nodes": len(nodes), "mesh_nodes": len(mesh_nodes), "primitives": prim_total,
        "materials_used": len(materials_used), "triangles": tri_total,
        "bounds_min": [round(v, 4) for v in lo], "bounds_max": [round(v, 4) for v in hi],
        "size_m": [round(v, 4) for v in size],           # X, Y(up), Z
        "joints": [{"name": j["name"], "type": j["type"], "axis": j.get("axis"),
                    "lower": j.get("lower"), "upper": j.get("upper")} for j in joints],
        "meshes": per_mesh,
        "node_names": [n.get("name", "") for n in nodes],
        "extensions_used": g.get("extensionsUsed", []),
    }

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    for p in args:
        r = inspect(p)
        if "--json" in sys.argv:
            print(json.dumps(r))
            continue
        print(f"{r['file']}\n  nodes={r['nodes']} mesh_nodes={r['mesh_nodes']} primitives={r['primitives']} "
              f"materials={r['materials_used']} triangles={r['triangles']}")
        print(f"  size (m) X={r['size_m'][0]} Y(up)={r['size_m'][1]} Z={r['size_m'][2]}  "
              f"min={r['bounds_min']} max={r['bounds_max']}")
        if "--meshes" in sys.argv:
            for m in sorted(r["meshes"], key=lambda m: -m["triangles"]):
                print(f"  mesh  {m['node']:<22} {m['triangles']:>6} tris  materials={m['materials']}")
        for j in r["joints"]:
            print(f"  joint {j['name']:<16} {j['type']:<10} axis={j['axis']} lower={j['lower']} upper={j['upper']}")
```

## Appendix C — `tools/preview_glb.py` (tested)

```python
#!/usr/bin/env python3
"""Render GLB files side by side to a PNG (headless, Blender workbench engine — needs `bpy`, which sand3d installs).

  python tools/preview_glb.py preview.png dist/rack.glb dist/tote.glb dist/robot.glb
  python tools/preview_glb.py lods.png dist/robot.lod0.glb dist/robot.lod1.glb dist/robot.lod2.glb --gap 1.2

glTF axes: X right, Y up, Z toward the viewer. Harmless "EGL Error ... BAD_MATCH" lines on a headless box are expected.
"""
import sys, os
import bpy
from mathutils import Vector

args = [a for a in sys.argv[1:] if not a.startswith("--")]
gap = float(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 0.8
if gap and str(gap) in args:  # the value after --gap is not a file
    args.remove(str(gap))
out, files = args[0], args[1:]
if not files:
    raise SystemExit(__doc__)

bpy.ops.wm.read_factory_settings(use_empty=True)
x = 0.0
for f in files:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=f)
    new = [o for o in bpy.data.objects if o not in before]
    roots = [o for o in new if o.parent is None]
    # measure width of what was just imported to space the models evenly
    xs = [(o.matrix_world @ Vector(c)).x for o in new if o.type == "MESH" for c in o.bound_box]
    w = (max(xs) - min(xs)) if xs else 1.0
    cx = (max(xs) + min(xs)) / 2 if xs else 0.0
    for r in roots:
        r.location.x += x - cx + w / 2
    x += w + gap

sc = bpy.context.scene
sc.render.engine = "BLENDER_WORKBENCH"
sc.display.shading.light = "STUDIO"
sc.display.shading.color_type = "MATERIAL"
sc.render.resolution_x, sc.render.resolution_y = 1600, 700
sc.render.film_transparent = False
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
sc.camera = cam
mid = (x - gap) / 2
tops = [(o.matrix_world @ Vector(c)).z for o in bpy.data.objects if o.type == "MESH" for c in o.bound_box]
height = max(tops) if tops else 1.0
dist = max(3.0, (x - gap) * 1.2, height * 3.6)
cam.location = (mid, -dist, height * 0.5 + 0.3)     # Blender is Z-up after import; the front of the scene is -Y
cam.rotation_euler = (Vector((mid, 0, height * 0.5)) - cam.location).to_track_quat("-Z", "Y").to_euler()
cam.data.lens = 35
sc.render.filepath = os.path.abspath(out)
bpy.ops.render.render(write_still=True)
print("wrote", sc.render.filepath)
```

## Appendix D — CI skeleton for the extended pipeline

```yaml
name: build-models
on:
  push: { branches: [main], paths: ["models/**", "lib/**", "tools/**", "tests/**", "requirements.txt", ".github/workflows/models.yml"] }
  pull_request: { paths: ["models/**", "lib/**", "tools/**", "tests/**", "requirements.txt"] }
  workflow_dispatch:
concurrency: { group: build-models-${{ github.ref }}, cancel-in-progress: true }
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13", cache: pip }
      - run: pip install -r requirements.txt pytest
      - run: python tools/build.py --preset large      # builds dist/ (models + LODs + layout + manifest)
      - run: python tools/validate.py dist             # V01–V17; fails the job on any violation
      - run: pytest -q
      - uses: actions/upload-artifact@v4
        with: { name: dist, path: dist/, retention-days: 14 }
      # TODO(other-agent): Node optimizer step  (reads dist/, writes dist-opt/, must keep named joint nodes)
      - name: Publish to Supabase
        if: github.ref == 'refs/heads/main' && github.event_name != 'pull_request'
        run: python tools/build.py --preset large --upload
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```
