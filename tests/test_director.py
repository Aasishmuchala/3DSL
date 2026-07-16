"""The match loop, exercised with fake hooks — no Max, no network, no images."""

import json

from maxgaffer.core.director import Hooks, MatchConfig, run_match, run_sun_sweep
from maxgaffer.core.genome import LightingState

REF = {"log_key": 0.20, "lab_mean": [55.0, 2.0, 12.0], "lab_std": [20, 4, 6],
       "p": {"5": 0.03, "25": 0.2, "50": 0.45, "75": 0.7, "95": 0.92},
       "contrast": 0.89,
       "lum_hist": [0.0] * 10 + [0.5, 0.5] + [0.0] * 20,
       "hue_hist": [0.6, 0.4] + [0.0] * 10}

SEMANTICS = {"time_of_day": "golden_hour"}


def start_state():
    st = LightingState()
    for k, v in {"sun.enabled": 1, "sun.azimuth_deg": 100.0, "sun.altitude_deg": 30.0,
                 "sun.intensity": 1.0, "sun.size": 2.0, "sun.turbidity": 3.0,
                 "exposure.ev": 12.0, "exposure.wb_kelvin": 6500.0}.items():
        st.set(k, v)
    return st


class Rig:
    """Fake world: renders 'succeed', stats converge toward REF as altitude approaches 6,
    the LLM proposes a fixed altitude walk. Records every applied state."""

    def __init__(self, scores=None, llm_replies=None, stats_seq=None):
        self.applied = []
        self.renders = []
        self.llm_calls = []
        self.scores = scores          # unused when stats_seq drives the critic
        self.llm_replies = llm_replies or []
        self.stats_seq = stats_seq or []
        self.cancel = False

    def hooks(self):
        def apply(st):
            self.applied.append(st.copy())

        def render(tag):
            self.renders.append(tag)
            return f"/tmp/{tag}.png"

        def stats(path):
            if self.stats_seq:
                return self.stats_seq.pop(0)
            return None

        def llm(ctx):
            self.llm_calls.append(ctx)
            if self.llm_replies:
                return self.llm_replies.pop(0)
            return json.dumps({"assessment": "closer", "changes": [
                {"param": "sun.altitude_deg", "value": 6.0, "why": "golden"}], "stop": False})

        return Hooks(apply=apply, render=render, stats=stats, llm_deltas=llm,
                     should_cancel=lambda: self.cancel)


def near(ref, log_key):
    s = dict(ref)
    s["log_key"] = log_key
    return s


def test_target_reached_stops_early_and_applies_best():
    # iter0 far (log_key 0.05 → ~2 stops off), iter1 exact match → 100 ≥ target
    rig = Rig(stats_seq=[near(REF, 0.05), dict(REF)])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=5, target_score=90))
    assert res.stop_reason == "target_reached"
    assert res.best_score == 100.0
    assert len(res.iterations) == 2
    # final apply == best state (the iter-1 state)
    assert rig.applied[-1].diff(res.best_state) == {}


def test_analytic_solver_corrects_ev_between_iterations():
    rig = Rig(stats_seq=[near(REF, 0.05), near(REF, 0.19)],
              llm_replies=[json.dumps({"assessment": "", "changes": [], "stop": False})] * 3)
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=2, target_score=99))
    it0 = res.iterations[0]
    # render was 2 stops dark → EV must have been lowered by 2
    assert abs(it0.analytic_changes["exposure.ev"] - 10.0) < 1e-6


def test_keep_best_reapplied_when_later_iterations_worsen():
    rig = Rig(stats_seq=[dict(REF), near(REF, 0.02), near(REF, 0.02)])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=3, target_score=101, stall_patience=99))
    assert res.best_score == 100.0
    # the state applied last equals the best (iteration-0) state, not the worsened one
    assert rig.applied[-1].diff(res.best_state) == {}
    assert res.iterations[0].score == 100.0


def test_slump_reverts_to_best_state():
    rig = Rig(stats_seq=[dict(REF), near(REF, 0.02), near(REF, 0.02), near(REF, 0.02)])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=4, target_score=101, stall_patience=99,
                                slump_tolerance=1.0))
    assert any(r.reverted_to_best for r in res.iterations)


def test_stall_stops_loop():
    rig = Rig(stats_seq=[near(REF, 0.1)] * 5)
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=5, target_score=101, stall_patience=2))
    assert res.stop_reason == "stalled"
    assert len(res.iterations) <= 4


def test_llm_garbage_never_kills_the_loop():
    rig = Rig(stats_seq=[near(REF, 0.1), near(REF, 0.12)],
              llm_replies=["I refuse to answer with JSON, sorry."])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=2, target_score=101, stall_patience=99))
    assert len(res.iterations) == 2
    assert res.iterations[0].llm_accepted == {}


def test_locks_block_llm_changes():
    reply = json.dumps({"assessment": "", "changes": [
        {"param": "sun.altitude_deg", "value": 6.0, "why": ""},
        {"param": "sun.turbidity", "value": 8.0, "why": ""}], "stop": False})
    rig = Rig(stats_seq=[near(REF, 0.1), near(REF, 0.1)], llm_replies=[reply])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=2, target_score=101, stall_patience=99),
                    locks={"sun.altitude_deg"})
    it0 = res.iterations[0]
    assert "sun.altitude_deg" not in it0.llm_accepted
    assert "sun.turbidity" in it0.llm_accepted
    assert any("locked" in r for r in it0.llm_rejected)


def test_llm_stop_with_no_changes_ends_loop():
    reply = json.dumps({"assessment": "matched", "changes": [], "stop": True})
    # stats close enough that analytic solver is inside its deadbands
    rig = Rig(stats_seq=[dict(REF)] * 3, llm_replies=[reply])
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(),
                    MatchConfig(max_iterations=3, target_score=101, stall_patience=99))
    assert res.stop_reason == "llm_satisfied"
    assert len(res.iterations) == 1


def test_cancel_and_render_failure():
    rig = Rig(stats_seq=[near(REF, 0.1)])
    rig.cancel = True
    res = run_match(start_state(), REF, SEMANTICS, rig.hooks(), MatchConfig())
    assert res.stop_reason == "cancelled"

    rig2 = Rig()
    hooks = rig2.hooks()
    hooks.render = lambda tag: None
    res2 = run_match(start_state(), REF, SEMANTICS, hooks, MatchConfig())
    assert res2.stop_reason == "render_failed"


def test_metrics_unavailable_degrades_to_llm_only():
    logs = []
    rig = Rig(llm_replies=[json.dumps(
        {"assessment": "", "changes": [{"param": "sun.altitude_deg", "value": 6, "why": ""}],
         "stop": False})] * 2)
    hooks = rig.hooks()
    hooks.log = logs.append
    res = run_match(start_state(), None, SEMANTICS, hooks, MatchConfig(max_iterations=3))
    assert res.best_score is None
    assert any("metrics unavailable" in m for m in logs)
    assert res.iterations[0].analytic_changes == {}      # solver off without stats
    assert res.iterations[0].llm_accepted                # LLM still drives


def test_no_llm_call_after_final_render():
    rig = Rig(stats_seq=[near(REF, 0.1), near(REF, 0.11)])
    run_match(start_state(), REF, SEMANTICS, rig.hooks(),
              MatchConfig(max_iterations=2, target_score=101, stall_patience=99))
    assert len(rig.llm_calls) == 1        # only between iter0 and iter1


def test_sun_sweep_picks_applies_and_returns_altitude_hint():
    rig = Rig()
    hooks = rig.hooks()
    az, hint, why = run_sun_sweep(
        start_state(), [0.0, 90.0, 180.0, 270.0], hooks,
        llm_pick=lambda paths, azs: json.dumps(
            {"best_index": 2, "altitude_hint": "golden", "why": "shadows fall left"}))
    assert az == 180.0
    assert hint == "golden"
    assert "shadows" in why
    assert len(rig.renders) == 4
    # each probe applied its azimuth
    assert [round(s.get("sun.azimuth_deg")) for s in rig.applied] == [0, 90, 180, 270]


def test_sun_sweep_survives_bad_reply_and_failed_probes():
    rig = Rig()
    hooks = rig.hooks()
    az, hint, why = run_sun_sweep(start_state(), [0.0, 90.0], hooks,
                                  llm_pick=lambda p, a: "nope")
    assert az is None and hint == "na" and "unusable" in why

    hooks.render = lambda tag: None
    az2, hint2, why2 = run_sun_sweep(start_state(), [0.0, 90.0], hooks,
                                     llm_pick=lambda p, a: "{}")
    assert az2 is None and hint2 == "na" and "not enough" in why2


def test_altitude_hint_bands_all_mapped():
    """Every altitude band the sweep can return must map to degrees in the rules table —
    a missing key would silently skip the refinement."""
    from maxgaffer.core.parse import ALTITUDE_BANDS
    from maxgaffer.core.rules import ALTITUDE_DEG

    for band in ALTITUDE_BANDS:
        assert band in ALTITUDE_DEG
