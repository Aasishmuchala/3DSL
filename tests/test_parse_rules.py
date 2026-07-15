import pytest

from maxgaffer.core import parse, rules
from maxgaffer.core.genome import LightingState


def rig_state():
    st = LightingState()
    for k, v in {
        "sun.enabled": 1, "sun.azimuth_deg": 0.0, "sun.altitude_deg": 45.0,
        "sun.intensity": 1.0, "sun.size": 1.0, "sun.turbidity": 3.0,
        "dome.enabled": 1, "dome.rotation_deg": 0.0, "dome.intensity": 1.0,
        "exposure.ev": 12.0, "exposure.wb_kelvin": 6500.0,
    }.items():
        st.set(k, v)
    st.groups["practicals"] = 1.0
    return st


# --------------------------------------------------------------------------- parse
def test_analysis_defaults_and_clamps():
    a = parse.validate_analysis(
        'Sure! {"time_of_day":"golden_hour","sun_bearing_deg":720,'
        '"wb_kelvin_estimate":"warm","confidence":3}')
    assert a["time_of_day"] == "golden_hour"
    assert a["sun_bearing_deg"] == 180.0          # clamped
    assert a["wb_kelvin_estimate"] == 6500.0      # non-numeric → default
    assert a["confidence"] == 1.0
    assert a["sky"] == "clear"                    # missing → default


def test_analysis_no_json_raises():
    with pytest.raises(parse.ParseError):
        parse.validate_analysis("I cannot analyze this image.")


def test_deltas_validation_shape():
    d = parse.validate_deltas(
        'thinking...\n{"assessment":"too flat","changes":['
        '{"param":"sun.altitude_deg","value":10,"why":"lower sun"},'
        '{"param":"sun.azimuth_deg","value":"oops"},'
        '{"not_a_param": true},'
        '{"param":"a","value":1},{"param":"b","value":2},{"param":"c","value":3}],'
        '"stop":"yes"}')
    assert d["changes"]["sun.altitude_deg"] == 10.0
    assert "sun.azimuth_deg" not in d["changes"]  # non-numeric dropped at shape level
    assert len(d["changes"]) <= 4                  # max_changes enforced
    assert d["stop"] is False                      # non-bool → default
    assert d["reasons"]["sun.altitude_deg"] == "lower sun"


def test_sweep_index_clamped():
    s = parse.validate_sweep('{"best_index": 99, "altitude_hint":"golden"}', 8)
    assert s["best_index"] == 7
    assert s["altitude_hint"] == "golden"


# --------------------------------------------------------------------------- rules
def golden_semantics(**over):
    base = {
        "scene_type": "exterior", "time_of_day": "golden_hour", "sky": "clear",
        "sun_active": True, "sun_bearing_deg": -120.0, "sun_altitude_band": "golden",
        "light_quality": "hard", "wb_kelvin_estimate": 5200.0, "practicals_on": False,
        "atmosphere": "light_haze", "contrast_character": "moody",
        "key_notes": "", "confidence": 0.9,
    }
    base.update(over)
    return base


def test_golden_hour_backlit_left():
    st, why = rules.initial_state(golden_semantics(), rig_state(), camera_yaw_deg=90.0)
    assert st.get("sun.enabled") == 1
    assert st.get("sun.altitude_deg") == 6.0
    # 90 (camera yaw) + (-120) = -30 → wraps to 330
    assert st.get("sun.azimuth_deg") == 330.0
    assert st.get("sun.size") == 1.0                       # hard light
    assert st.get("sun.turbidity") == 4.5                  # light haze
    assert st.get("exposure.wb_kelvin") == 5200.0
    assert st.groups["practicals"] == 0.0                  # daylight, practicals off
    assert len(why) >= 5


def test_overcast_kills_sun_and_boosts_dome():
    st, _ = rules.initial_state(
        golden_semantics(sky="overcast", time_of_day="overcast_day", sun_active=True),
        rig_state(), camera_yaw_deg=0.0)
    assert st.get("sun.enabled") == 0
    assert st.get("dome.intensity") >= 1.0


def test_night_turns_practicals_up():
    st, _ = rules.initial_state(
        golden_semantics(time_of_day="night", sky="night", sun_active=False,
                         practicals_on=True),
        rig_state(), camera_yaw_deg=0.0)
    assert st.get("sun.enabled") == 0
    assert st.groups["practicals"] >= 1.0


def test_rules_respect_locks_and_missing_rig_keys():
    rig = rig_state()
    st, _ = rules.initial_state(golden_semantics(), rig, camera_yaw_deg=0.0,
                                locks={"sun.altitude_deg"})
    assert st.get("sun.altitude_deg") == 45.0   # locked → untouched

    bare = LightingState()                       # rig with no sun at all
    bare.set("exposure.ev", 12.0)
    bare.set("exposure.wb_kelvin", 6500.0)
    st2, _ = rules.initial_state(golden_semantics(), bare, camera_yaw_deg=0.0)
    assert "sun.altitude_deg" not in st2.values  # never invents params the rig lacks


def test_sweep_azimuths():
    az = rules.sweep_azimuths(8)
    assert len(az) == 8 and az[0] == 0.0 and az[4] == 180.0
    assert rules.sweep_azimuths(1) == [0.0, 180.0]
