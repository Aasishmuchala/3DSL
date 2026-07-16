"""v0.6 quality release — consensus, direction metric, highlight WB, oscillation text,
sweep cross-check. Each encodes a weakness the live runs exposed."""

import json

import pytest

from maxgaffer.core import critic, solver
from maxgaffer.core.consensus import consolidate_analyses
from maxgaffer.core.director import Hooks, run_sun_sweep
from maxgaffer.core.genome import LightingState


def sample(**over):
    base = {"scene_type": "exterior", "time_of_day": "golden_hour", "sky": "hazy",
            "sun_active": True, "sun_bearing_deg": -40.0, "sun_altitude_band": "golden",
            "light_quality": "soft", "wb_kelvin_estimate": 4200.0, "practicals_on": False,
            "atmosphere": "light_haze", "contrast_character": "moody",
            "key_notes": "warm low sun", "confidence": 0.9}
    base.update(over)
    return base


# --------------------------------------------------------------- ANALYZE consensus
def test_consensus_majority_beats_the_outlier():
    """The live failure: one sample read the golden ref as midday — 2-of-3 must win."""
    out = consolidate_analyses([
        sample(),
        sample(time_of_day="midday", sun_altitude_band="high",
               wb_kelvin_estimate=6500.0, confidence=0.6),
        sample(confidence=0.85),
    ])
    assert out["time_of_day"] == "golden_hour"
    assert out["sun_altitude_band"] == "golden"
    assert out["wb_kelvin_estimate"] == 4200.0        # median of 4200/6500/4200
    assert out["consensus_agreement"] == pytest.approx(2 / 3, abs=0.01)


def test_consensus_circular_bearing_and_bools():
    # -170 and +170 straddle the wrap: arithmetic median would say ~0 (dead wrong)
    out = consolidate_analyses([
        sample(sun_bearing_deg=-170.0), sample(sun_bearing_deg=170.0),
        sample(sun_bearing_deg=180.0),
    ])
    assert abs(out["sun_bearing_deg"]) > 160.0        # stays near the wrap, not near 0
    out2 = consolidate_analyses([
        sample(practicals_on=True), sample(practicals_on=True),
        sample(practicals_on=False),
    ])
    assert out2["practicals_on"] is True


def test_consensus_single_sample_passthrough_and_tiebreak():
    assert consolidate_analyses([sample()])["time_of_day"] == "golden_hour"
    out = consolidate_analyses([
        sample(sky="clear", confidence=0.95), sample(sky="hazy", confidence=0.5),
    ])
    assert out["sky"] == "clear"                      # tie → most confident sample


# --------------------------------------------------------------- direction metric
def grid_stats(grid):
    return {"log_key": 0.2, "lab_mean": [50, 0, 5], "lab_std": [20, 4, 4],
            "p": {"5": 0.05, "25": 0.2, "50": 0.5, "75": 0.7, "95": 0.9},
            "contrast": 0.85, "lum_hist": [1.0 / 32] * 32, "hue_hist": [1.0 / 12] * 12,
            "grid": grid}


LEFT_BRIGHT = [0.2, 0, -0.2, 0.2, 0, -0.2, 0.2, 0, -0.2]
RIGHT_BRIGHT = [-0.2, 0, 0.2, -0.2, 0, 0.2, -0.2, 0, 0.2]


def test_critic_direction_component_separates_sun_sides():
    same = critic.score(grid_stats(LEFT_BRIGHT), grid_stats(LEFT_BRIGHT))
    flipped = critic.score(grid_stats(LEFT_BRIGHT), grid_stats(RIGHT_BRIGHT))
    assert same.score == 100.0
    assert flipped.components["direction"] < 0.1      # opposite pattern ≈ 0
    assert flipped.score < same.score - 10
    # stats without a grid renormalize cleanly (back-compat with old fixtures)
    old = {k: v for k, v in grid_stats(LEFT_BRIGHT).items() if k != "grid"}
    v = critic.score(old, dict(old))
    assert v.score == 100.0 and "direction" not in v.components


def test_metrics_grid_and_highlights_from_real_image(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    from maxgaffer.core import metrics

    im = PIL.new("RGB", (96, 60))
    px = im.load()
    for y in range(60):
        for x in range(96):                            # bright warm LEFT, dark cool right
            px[x, y] = (250, 210, 150) if x < 32 else (20, 25, 45)
    p = str(tmp_path / "dirwarm.png")
    im.save(p)
    s = metrics.compute_stats(p)
    assert s["grid"][0] > 0 > s["grid"][2]             # left cells above mean, right below
    assert s["lab_mean_hi"][2] > s["lab_mean"][2]      # highlights warmer than full mean


# --------------------------------------------------------------- highlight WB solve
def test_wb_solver_prefers_highlights_over_albedo():
    """Warm-furniture room under NEUTRAL light: full-mean b* screams 'warm scene' but the
    highlights say the illuminant already matches — the solver must stay in the deadband."""
    ref = {"lab_mean": [50, 0, 18.0], "lab_mean_hi": [80, 0, 4.0]}   # warm walls, neutral light
    cur = {"lab_mean": [50, 0, 2.0], "lab_mean_hi": [80, 0, 3.5]}    # neutral render
    assert solver.solve_wb(ref, cur, 6500.0) is None    # highlights agree → no move
    # but a genuinely warm illuminant in the highlights still drives the solve
    warm_ref = {"lab_mean": [50, 0, 18.0], "lab_mean_hi": [80, 0, 14.0]}
    assert solver.solve_wb(warm_ref, cur, 6500.0) > 6500.0
    # never compare highlight mean against full mean (old stats one side)
    old_cur = {"lab_mean": [50, 0, 2.0]}
    moved = solver.solve_wb(warm_ref, old_cur, 6500.0)
    assert moved is not None and moved > 6500.0         # falls back to full-vs-full


# --------------------------------------------------------------- sweep cross-check
def test_sweep_metric_overrides_clearly_wrong_llm_pick():
    ref = grid_stats(LEFT_BRIGHT)
    probe_grids = {"sweep000": RIGHT_BRIGHT, "sweep090": LEFT_BRIGHT}
    applied = []
    hooks = Hooks(apply=lambda st: applied.append(st.get("sun.azimuth_deg")),
                  render=lambda tag: f"/tmp/{tag}.png",
                  stats=lambda p: grid_stats(probe_grids[p.split('/')[-1][:-4]]),
                  llm_deltas=lambda ctx: "", log=lambda m: None)
    st = LightingState()
    st.set("sun.azimuth_deg", 0.0)
    az, hint, why = run_sun_sweep(
        st, [0.0, 90.0], hooks,
        llm_pick=lambda p, a: json.dumps({"best_index": 0, "altitude_hint": "golden",
                                          "why": "llm says 0"}),
        ref_stats=ref)
    assert az == 90.0                                   # metric’s probe matches the ref
    # without ref grid → pure LLM behavior unchanged
    az2, _, _ = run_sun_sweep(st, [0.0, 90.0], hooks,
                              llm_pick=lambda p, a: json.dumps(
                                  {"best_index": 0, "altitude_hint": "na", "why": "x"}),
                              ref_stats=None)
    assert az2 == 0.0


# --------------------------------------------------------------- oscillation damping
def test_param_history_reaches_the_prompt():
    from maxgaffer.core.director import MatchConfig, run_match
    from tests.test_stress_round2 import REF, near

    replies = [json.dumps({"assessment": "", "changes": [
        {"param": "sun.altitude_deg", "value": v, "why": "hunt"}], "stop": False})
        for v in (6.0, 30.0, 6.0)]
    seen = []

    def llm(ctx):
        seen.append(ctx.get("param_history", ""))
        return replies.pop(0) if replies else json.dumps(
            {"assessment": "", "changes": [], "stop": False})

    st = LightingState()
    for k, v in {"sun.enabled": 1, "sun.azimuth_deg": 0.0, "sun.altitude_deg": 55.0,
                 "exposure.ev": 12.0, "exposure.wb_kelvin": 6500.0}.items():
        st.set(k, v)
    hooks = Hooks(apply=lambda s: None, render=lambda t: f"/tmp/{t}.png",
                  stats=lambda p: near(0.19), llm_deltas=llm, log=lambda m: None)
    run_match(st, REF, {}, hooks,
              MatchConfig(max_iterations=4, target_score=101, stall_patience=99))
    assert any("sun.altitude_deg" in h and "→" in h for h in seen[2:])   # trajectory shown
    from maxgaffer.core.prompts import deltas_user_text
    txt = deltas_user_text("T", {}, [], {}, 1, 5, "", "  sun.altitude_deg: 6.0 → 30.0")
    assert "do NOT oscillate" in txt
