"""The refine conversation loop — craft-table notes, lenses, note threading, session log."""

import json

from maxgaffer.core import feedback
from maxgaffer.core.director import Hooks, MatchConfig, run_match
from maxgaffer.core.genome import LightingState
from maxgaffer.core.prompts import DELTAS_SYSTEM, deltas_user_text
from maxgaffer.core.session import Session

from tests.test_stress_round2 import REF, near


def rig_keys_state():
    st = LightingState()
    for k, v in {"sun.enabled": 1, "sun.azimuth_deg": 100.0, "sun.altitude_deg": 30.0,
                 "sun.size": 2.0, "sun.turbidity": 3.0,
                 "exposure.ev": 12.0, "exposure.wb_kelvin": 6500.0}.items():
        st.set(k, v)
    st.groups["practicals"] = 1.0
    return st


# ------------------------------------------------------------------ craft table
def test_notes_translate_to_bounded_nudges():
    st = rig_keys_state()
    d = feedback.nudges_from_note("exposure is too much and it's too warm",
                                  st.keys(), list(st.groups))
    assert d["exposure.ev"] == 0.7
    assert d["exposure.wb_kelvin"] == -800.0
    new, applied = feedback.apply_note_deltas(st, d)
    assert applied["exposure.ev"] == 12.7
    assert applied["exposure.wb_kelvin"] == 5700.0


def test_intensifiers_double_and_log_keys_scale():
    st = rig_keys_state()
    d = feedback.nudges_from_note("way too dark, shadows are too hard",
                                  st.keys(), list(st.groups))
    assert d["exposure.ev"] == -1.4                    # doubled
    assert d["sun.size"] == 0.5
    new, applied = feedback.apply_note_deltas(st, d)
    assert abs(applied["sun.size"] - 2.0 * 2 ** 0.5) < 1e-9   # log2 half-stop softer


def test_direction_practicals_and_unknown_notes():
    st = rig_keys_state()
    d = feedback.nudges_from_note("sun more left, practicals too bright",
                                  st.keys(), list(st.groups))
    assert d["sun.azimuth_deg"] == -20.0
    assert d["group.practicals"] == -0.5
    assert feedback.nudges_from_note("hmm the vibes feel off",
                                     st.keys(), list(st.groups)) == {}
    # params the rig lacks are never nudged
    bare = LightingState()
    bare.set("exposure.ev", 12.0)
    d2 = feedback.nudges_from_note("sun more left", bare.keys(), [])
    assert d2 == {}


def test_note_deltas_respect_genome_bounds():
    st = rig_keys_state()
    st.set("exposure.ev", 19.8)
    _new, applied = feedback.apply_note_deltas(st, {"exposure.ev": 5.0})
    assert applied["exposure.ev"] == 20.0              # clamped by the genome


# ------------------------------------------------------------------ lenses + prompt
def test_lenses_and_note_injection():
    assert len(feedback.LENSES) == 3
    sys_txt = feedback.lens_system(DELTAS_SYSTEM, feedback.LENSES[1][1])
    assert "LENS:" in sys_txt and "DIRECTION" in sys_txt
    user = deltas_user_text("T", {}, [], {}, 0, 1, "", "",
                            director_note="exposure is too much")
    assert "DIRECTOR'S NOTE" in user and "exposure is too much" in user
    assert "DIRECTOR'S NOTE" not in deltas_user_text("T", {}, [], {}, 0, 1)


def test_run_match_threads_note_into_llm_ctx():
    seen = []

    def llm(ctx):
        seen.append(ctx.get("director_note"))
        return json.dumps({"assessment": "", "changes": [], "stop": False})

    hooks = Hooks(apply=lambda s: None, render=lambda t: f"/tmp/{t}.png",
                  stats=lambda p: near(0.15), llm_deltas=llm, log=lambda m: None)
    run_match(rig_keys_state(), REF, {}, hooks,
              MatchConfig(max_iterations=2, target_score=101, stall_patience=99),
              director_note="sun more left")
    assert seen and seen[0] == "sun more left"


# ------------------------------------------------------------------ session notes
def test_session_notes_roundtrip(tmp_path):
    p = str(tmp_path / "s.maxgaffer.json")
    s = Session(p, now_fn=lambda: "t")
    e = s.entry("cam")
    e.notes = ["too bright", "sun more left"]
    assert s.save()
    s2 = Session.load(p)
    assert s2.entry("cam").notes == ["too bright", "sun more left"]
