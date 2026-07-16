
from maxgaffer.core import critic, solver
from maxgaffer.core.genome import LightingState


def stats(log_key=0.18, b=5.0, a=0.0, p5=0.02, p95=0.9, hist_shift=0):
    lum_hist = [0.0] * 32
    lum_hist[10 + hist_shift] = 0.6
    lum_hist[20 + hist_shift] = 0.4
    hue_hist = [0.0] * 12
    hue_hist[1] = 0.7
    hue_hist[7] = 0.3
    return {
        "log_key": log_key,
        "lab_mean": [50.0, a, b],
        "lab_std": [20.0, 5.0, 5.0],
        "p": {"5": p5, "25": 0.2, "50": 0.45, "75": 0.7, "95": p95},
        "contrast": p95 - p5,
        "lum_hist": lum_hist,
        "hue_hist": hue_hist,
    }


def state():
    st = LightingState()
    st.set("exposure.ev", 12.0)
    st.set("exposure.wb_kelvin", 6500.0)
    return st


# ------------------------------------------------------------------- EV sign convention
def test_ev_darker_render_lowers_ev():
    # render key is 2 stops darker than reference → EV must DROP by 2 (higher EV = darker)
    new_ev = solver.solve_ev(stats(log_key=0.2), stats(log_key=0.05), current_ev=12.0)
    assert new_ev is not None and abs(new_ev - 10.0) < 1e-6


def test_ev_brighter_render_raises_ev_and_clamps():
    # render 4 stops too bright → dEV = -4, clamped to the 2.5 per-iteration max
    new_ev = solver.solve_ev(stats(log_key=0.05), stats(log_key=0.8), current_ev=12.0)
    assert new_ev is not None and abs(new_ev - 14.5) < 1e-6


def test_ev_deadband():
    assert solver.solve_ev(stats(log_key=0.2), stats(log_key=0.2), 12.0) is None


# ------------------------------------------------------------------- WB sign convention
def test_wb_warmer_reference_raises_kelvin():
    # reference is yellower (higher b*) → raise WB kelvin to warm the render
    new_wb = solver.solve_wb(stats(b=15.0), stats(b=5.0), current_kelvin=6500.0)
    assert new_wb is not None and new_wb > 6500.0
    assert abs(new_wb - (6500.0 + 10.0 * solver.WB_KELVIN_PER_B)) < 1e-6


def test_wb_cooler_reference_lowers_kelvin_and_deadband():
    new_wb = solver.solve_wb(stats(b=-10.0), stats(b=5.0), current_kelvin=6500.0)
    assert new_wb is not None and new_wb < 6500.0
    assert solver.solve_wb(stats(b=5.5), stats(b=5.0), 6500.0) is None


def test_analytic_pass_respects_locks():
    changes = solver.analytic_pass(
        state(), stats(log_key=0.4, b=20.0), stats(log_key=0.1, b=0.0),
        locks={"exposure.ev"})
    assert "exposure.ev" not in changes
    assert "exposure.wb_kelvin" in changes


# ------------------------------------------------------------------- critic
def test_identical_stats_score_100():
    v = critic.score(stats(), stats())
    assert v.score == 100.0
    assert all(abs(c - 1.0) < 1e-9 for c in v.components.values())


def test_score_monotone_with_distance():
    ref = stats()
    near = critic.score(ref, stats(log_key=0.14, b=8.0)).score
    far = critic.score(ref, stats(log_key=0.02, b=-20.0, p5=0.3, p95=0.5, hist_shift=8)).score
    assert 100.0 > near > far >= 0.0


def test_weights_override_changes_emphasis():
    ref, cur = stats(), stats(b=25.0)  # only color differs
    default = critic.score(ref, cur).score
    color_heavy = critic.score(ref, cur, {"color": 0.9, "key": 0.025, "envelope": 0.025,
                                          "histogram": 0.025, "hue": 0.025}).score
    assert color_heavy < default


def test_missing_fields_do_not_crash():
    v = critic.score({}, {})
    assert 0.0 <= v.score <= 100.0
