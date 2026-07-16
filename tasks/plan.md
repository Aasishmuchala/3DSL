# MaxGaffer — Execution Plan (v2, post stress-test)

Everything off-Max is DONE (core + bridge + UI + 69 tests + round-2 fixes). What remains is
on-box bring-up → first live match → production trial → v0.2 backlog. Each phase has
acceptance criteria; nothing advances on vibes.

---

## P0 — On-box bring-up (one session at the Max 2026 box, ~90 min)

Run `scripts\install.bat`, restart Max, open a THROWAWAY copy of a real scene.
Then four spikes. For every mismatch: fix the named candidates tuple, re-test, commit.

### Spike A — property-name audit (~20 min) · checklist #1 #2 #3 #4 #5
Listener:
```maxscript
showProperties (getNodeByName "VRaySun001")        -- intensity_multiplier? size_multiplier? turbidity?
(getNodeByName "VRayLightDome").type               -- dome enum: expect 1
showProperties dome.texmap                          -- horizontalRotation?
showProperties SceneExposureControl.exposureControl -- .ev? .temperature? WB mode enum ints?
showProperties (getNodeByName "PhysCamera001")      -- ISO / f_number / shutter names
```
Patch targets: `scene.SUN_* / DOME_TEX_ROT / _dome_type_value` · `exposure.EC_* / CAM_* /
_nudge_wb_mode_*`. **Accept:** preflight "scene rig" line shows sun=yes dome=yes and the
expected groups; no ⚠ property warnings when moving each RIG slider once.

### Spike B — sign conventions (~15 min) · checklist #6
1. RIG slider `exposure.wb_kelvin` +2000 → loop render must be **warmer**. If inverted,
   report before touching code — the flip lives in ONE place (`solver.solve_wb` sign).
2. `exposure.ev` +2 → darker. 3. `sun.altitude_deg` 6° → long shadows; azimuth slider
   swings shadow direction; `dome.rotation_deg` spins the HDRI (watch in Vantage).
**Accept:** all four directions visually correct.

### Spike C — render / export / Vantage CLI (~30 min) · checklist #7 #8 #10
1. MATCH with 1 iteration, sweep off → run folder has `iter00.png` at 480×270, render
   setup untouched after.
2. `vrayExportVRScene "C:\tmp\t.vrscene"` → file exists; then with kwargs.
3. Hand-run one `vantage_console` command from `vantage.vantage_command` → PNG lands.
**Accept:** "Final render (selected)" produces a Vantage still end-to-end from the UI.

### Spike D — live link, daylight, contention (~25 min) · checklist #9 #11 #13 #14 (+#12)
1. **Start live link** button: if "no entry point found", start via V-Ray menu, note the
   exact menu label → pin it in `vantage.LIVE_LINK_GLOBALS`/action scan.
2. Daylight-assembly scene: confirm the rig-note warning appears; sun move either works or
   warns (never silently fails).
3. Toggle `sun.enabled` 0 with a VRaySky env: sky must not black out (else rules switch to
   intensity-dimming — one-line change in `core/rules.py`, flagged in SPEC).
4. Heavy scene + live link + GPU loop render together: watch VRAM. If starved → matching
   sessions run V-Ray CPU or link closed during MATCH (workflow note, no code).
5. Optional: point Settings → system python at a Pillow venv; `metrics_cli some.jpg` prints
   stats.
**→ CHECKPOINT 0: preflight all-green inside Max. Commit "on-box verified" with the diff.**

## P1 — First live match (MVP smoke, ~1 hr)
Small-but-real interior. Bind a reference close in albedo (fair first fight). Sweep ON,
5 iterations, target 82. Record: wall time/iteration, final score, leash hits, LLM
rejections, and the money shot — reference vs `iterNN.png` side-by-side.
Then: select another camera (state recall applies), Restore pre-match (rig returns), reopen
scene (session + baselines survive reload).
**Accept (=C1):** ≥75 score or visual acceptance in ≤2 runs · recall + restore + reload all
correct · one full-res Vantage still of the matched camera.
**Then the hostile case:** deliberately mismatched-albedo reference → confirm the leash
diagnosis fires and lock-EV workflow feels usable (this is the tool's honest edge — feel it).

## P2 — Shot-board production trial (overnight)
TULA-style multi-camera scene: reference per camera (mixed times of day), match each
(note per-shot wall time), then **Render ALL matched cameras** to an output folder overnight.
**Accept (=C2):** every shot renders under its own light, zero manual steps after launch;
per-shot cost known → decide default iterations/sweep for heavy scenes.

## P3 — v0.2 backlog (post-C2, ranked)

| # | Item | Why / note |
|---|---|---|
| 1 | Consume sweep `altitude_hint` (2-band probe after azimuth pick) | free accuracy, ~20 LoC in director |
| 2 | Per-iteration thumbnails in the log panel | trust + faster human abort |
| 3 | A/B flip button (pre-match ↔ matched) | the director's favorite toggle |
| 4 | Draft-sampler session preset — OPT-IN, explicit banner + restore | loop cost is render cost; only after C2 timing data |
| 5 | Run-dir auto-cleanup (keep last K runs/camera) | disk hygiene |
| 6 | Photometric/standard lights in groups | only if a real scene demands it |
| 7 | EXR/ACES-aware stats (current: sRGB PNG/JPEG assumption) | needed the day refs arrive as EXR |
| 8 | Sun-off alternative: intensity-dim mode if #13 shows VRaySky coupling | decided by Spike D |
| 9 | Batch MATCH (queue all cameras w/ refs, unattended) | overnight matching, after loop is trusted |
| 10 | MaxDirector integration: expose `run_match` as its LightMatch stage | SPEC'd in MaxDirector P2 — this repo IS that engine |

## Risks (top 5)
1. **WB direction inverted on box** (#6) — one visual check, one-line flip. Do it FIRST.
2. **Live-link has no scriptable entry** (#9) — degraded path already shipped (manual menu
   click once per session); pin the action label when known.
3. **Loop render cost on heavy interiors** — mitigations exist (res, iterations, sweep off,
   CPU mode); measure at P1/P2 before adding machinery (#4 backlog).
4. **Albedo trap in daily use** — leash + diagnosis shipped; if it fires constantly on real
   pairs, revisit weights (`config.critic_weights`) with logged run data.
5. **V-Ray build drift** (property names) — candidates + checklist; one-time cost per build.

## Definition of done — v1.0
C0 + C1 + C2 checked · WB/EV/azimuth/dome directions verified · README checklist all ✓ ·
backlog #1–#3 shipped · memory + SPEC updated with measured per-iteration costs.
