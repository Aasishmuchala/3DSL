# MaxGaffer

> Spec: **[SPEC.md](SPEC.md)** (v2, post stress-test — locked decisions, guards, reliability
> model) · Forward plan: **[tasks/plan.md](tasks/plan.md)** (P0 on-box bring-up → v1.0).

**The gaffer to MaxDirector's director.** Pick a camera from the shot board, hand it a
lighting reference image, press **MATCH LIGHTING** — and MaxGaffer analyzes the reference,
first-guesses a rig from gaffer craft tables, then iterates the scene's **sun, HDRI dome,
exposure, white balance and practical-light groups** until the render's light matches the
reference — while the **Chaos Vantage live link mirrors every step in real time**. Each
camera keeps its own matched lighting state, so "render every shot under its own light
through Vantage" is one button.

Target: **3ds Max 2026 (Py 3.11, PySide6) + V-Ray 7 + Chaos Vantage 3.x.** Internal Sthyra
pipeline tool, sibling of MaxDirector (same Omega gateway, same hexagon architecture, same
key — borrowed automatically if MaxDirector is installed).

## Who does what (the split of powers)

| Job | Owner | Why |
|---|---|---|
| Exposure (EV) + white balance | **histogram solver** (deterministic) | measurable — never let an LLM eyeball photometry |
| Sun azimuth (coarse) | **sweep grid + LLM multiple-choice** | comparison is reliable where estimation is not |
| Sun geometry refine, sky character, HDRI rotation, light-group balance, mood | **Opus 4.8 vision** via Omega, ≤4 bounded changes/iteration | semantic judgment, hard-railed by the genome |
| Accept / revert / stop | **tonal critic** (deterministic 0-100) | keep-best + slump-revert make exploration safe |
| Taste, final say | **you** | locks, live sliders, undo — one undo record per apply |

Every LLM proposal is validated against the **genome** (`core/genome.py`): unknown params
dropped, locked params refused, bounds clamped, per-iteration step limits enforced. The
critic compares only what transfers between *different* scenes — tonal envelope and color
mood — never SSIM.

## Architecture (hexagon, enforced)

```
maxgaffer/core/       PURE python, ZERO pymxs/Qt (test-enforced) — genome, solver, critic,
                      director loop, rules, session, prompts, Omega client, stdlib PNG stats
maxgaffer/maxbridge/  the ONLY pymxs importer — scene/rig introspection, apply, exposure
                      hosts, loop renders, Vantage (live link + vrscene + vantage_console)
maxgaffer/ui/         PySide6 dock (camera board · reference · match loop · rig sliders · Vantage)
maxgaffer/sidecar/    optional Pillow stats CLI for a system python
tests/                pytest — runs on any OS, no Max needed
```

Dependency floor is **stdlib-only**: the Omega client is urllib, loop-render stats decode
through a built-in PNG reader. Pillow (optional, installer tries) upgrades JPEG reference
ingestion and slims LLM payloads; without it, references transcode through Max's own bitmap
I/O. There is no torch, no OpenCV, no required pip package.

## Install (on the Max 2026 box)

1. Clone/copy this folder, double-click **`scripts\install.bat`**.
2. Restart Max → Customize → Customize User Interface → category **MaxGaffer** → drag the
   action to a toolbar.
3. Click it. If MaxDirector is installed, the oc_ key is borrowed automatically; otherwise
   Settings → paste key → **Test gateway**.
4. (Recommended) Start the Vantage live link once from the V-Ray menu if the in-plugin
   button reports it couldn't find the action.

`python scripts/preflight.py [oc_key]` — anywhere — prints exactly what's ready; run it in
Max's scripting listener for the pymxs/V-Ray/Vantage/rig checks too.

## Using it

1. **Refresh** — the camera board lists every camera (name · ref-dot · last score).
2. Select a camera → **Load reference…** (JPEG/PNG/WEBP). One reference per camera.
3. Lock anything that must not move (padlocks list — e.g. lock `exposure.ev` if the
   client's exposure is contractual).
4. **MATCH LIGHTING**. Watch the log — analysis → first guess → per-iteration score,
   analytic EV/WB, the model's changes with reasons. Watch Vantage — every apply syncs.
   Optional **sun sweep first** grid-solves the sun direction before iterating.
5. Nudge sliders in **RIG** (live-applied → Vantage mirrors). The state is saved per camera;
   re-selecting a camera re-applies its light (toggle on the board).
6. **VANTAGE → Render ALL matched cameras**: each camera gets its saved state applied,
   a `.vrscene` exported, and a sequential `vantage_console` still at final resolution.

Iteration renders go to `%LOCALAPPDATA%\MaxGaffer\sessions\<scene>\<camera>\<timestamp>\`.
Per-camera bindings persist in `<scene>.maxgaffer.json` next to the .max file.

## ON-BOX VERIFICATION CHECKLIST (do these before first client use)

Everything scene-touching is written candidates-based and fault-isolated, but property
names drift across V-Ray builds. Verify each once in the Max listener; fix the candidate
tuples in `maxbridge/scene.py` / `exposure.py` if your build differs.

| # | What | How to verify | Where to fix |
|---|---|---|---|
| 1 | VRaySun props: `intensity_multiplier`, `size_multiplier`, `turbidity` | `showProperties (getNodeByName "VRaySun001")` | `scene.SUN_*` |
| 2 | VRayLight dome `.type == 1` | make a dome, `(getNodeByName "VRayLight001").type` | `scene._dome_type_value` |
| 3 | Dome HDRI rotation prop (`horizontalRotation` on the VRayBitmap texmap) — else node-Z fallback engages | `showProperties dome.texmap` · then move the RIG dome.rotation slider and confirm the HDRI spins in Vantage | `scene.DOME_TEX_ROT` |
| 4 | Exposure host: V-Ray exposure control `.ev` + WB `temperature`/mode enum | Environment panel → Exposure Control → V-Ray; `showProperties SceneExposureControl.exposureControl` | `exposure.EC_*`, `_nudge_wb_mode_*` |
| 5 | Physical camera ISO/f/shutter prop names (only if you use per-camera exposure) | `showProperties (getNodeByName "PhysCamera001")` | `exposure.CAM_*` |
| 6 | **WB kelvin direction**: raise `exposure.wb_kelvin` slider by +2000 → render must get **warmer** | RIG slider + one loop render | if inverted: report — solver sign flips in `core/solver.solve_wb` |
| 7 | `vrayExportVRScene()` exists + kwargs (`exportCompressed`, `startFrame`) | listener: `vrayExportVRScene "C:\\tmp\\t.vrscene"` | `vantage.export_vrscene` |
| 8 | `vantage_console.exe` path + CLI flags (`-scenefile -outputFile -outputWidth -outputHeight -frames`) | run the command from `vantage.vantage_command` by hand once | `config.vantage_console`, `vantage.vantage_command` |
| 9 | Live-link autostart (maxscript global or actionMan scan) | click **Start live link**; if "no entry point found", start it via the V-Ray menu once and tell me the menu label — I'll pin the action | `vantage.LIVE_LINK_GLOBALS`, `_find_live_link_action` |
| 10 | Loop render writes PNG with VFB off, respects size, restores render setup | select camera → MATCH with 1 iteration → check the run folder | `render.render_frame` |
| 11 | Sun move on a Daylight-assembly sun (controller-locked transforms warn in rig notes) | try a scene with a Daylight system | `scene.classify_rig` note / detach sun |
| 12 | Sidecar (optional): point Settings → system python at any Pillow-equipped python | `python -m maxgaffer.sidecar.metrics_cli some.jpg` prints stats JSON | `config.system_python` |
| 13 | Sun-off looks: disabling VRaySun must not black out a VRaySky-driven environment | rules set `sun.enabled 0` for overcast refs — toggle it manually once and check the env | `core/rules.py` (keep sun on, intensity ↓ instead) |
| 14 | GPU contention: Vantage live link + V-Ray GPU loop renders on one card | run a match with the link up on a heavy scene; if VRAM-starved, set V-Ray to CPU for matching or close the link during MATCH | workflow note, no code |

## Known failure modes (stress-tested, round 2)

* **Albedo trap** — the reference and your scene are *different rooms*: matching a white
  Scandinavian reference inside a dark-walnut scene biases the exposure/WB solver (it can
  only see histograms, not albedo). Round-2 defenses: the key is **center-weighted** (60/40),
  the solver runs on a **leash** (±4 EV / ±3000 K total per run), and hitting the leash twice
  prints an explicit diagnosis telling you to lock `exposure.ev` and set it by eye. That is
  the honest limit of statistics across different scenes.
* **Contaminated iterations** — when the solver had to move EV ≥ 1.5 stops, the render the
  model just critiqued was badly mis-exposed, so its intensity/group proposals are dropped
  for that iteration (geometry proposals survive). Logged when it happens.
* **Loop cost is render cost** — iteration renders use your current V-Ray sampler at
  `loop_width` (480 px default). On a heavy interior that's minutes per iteration ×
  (8 sweep + N iterations). MaxGaffer deliberately never touches sampler settings (contract:
  render setups are the artist's); use a draft preset for matching sessions if needed.
* **Matches are explorations** — the pre-match light is snapshotted automatically per camera;
  **Restore pre-match light** puts it back exactly. Baselines for practical groups are
  adopted once (by light name, persisted) and never re-captured, so MaxGaffer dimming a
  group to 0 can never poison its authored value.

Known v1 limits (deliberate): VRayLights/VRayIES only for practical groups (no photometric),
one sun + one dome (extras are ignored with a note), no volumetrics/aerial-perspective
matching yet, no per-light solo — groups are layer-based dimmer boards, and the sweep's
altitude hint is not yet consumed (azimuth only).

## Dev (off-Max, any OS)

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pillow
.venv/bin/python -m pytest tests/ -q          # ~70 tests, all pure core
```

The suite catches the classics: EV/WB sign conventions, the 180°-wrap ambiguity, LLM junk
replies, slump-revert, lock enforcement, stdlib-vs-Pillow stats agreement — plus the round-2
stress regressions (baseline poisoning, analytic leash, contamination guard, center-weighted
key, pre-match persistence).
