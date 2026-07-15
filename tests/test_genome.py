from maxgaffer.core.genome import (
    GROUP_PREFIX,
    LightingState,
    apply_changes,
    clamp,
    limit_step,
    spec_for,
    state_table,
)


def make_state():
    st = LightingState()
    st.set("sun.enabled", 1)
    st.set("sun.azimuth_deg", 180.0)
    st.set("sun.altitude_deg", 30.0)
    st.set("sun.intensity", 1.0)
    st.set("exposure.ev", 12.0)
    st.set("exposure.wb_kelvin", 6500.0)
    st.groups["practicals"] = 1.0
    return st


def test_clamp_bounds_and_wrap():
    assert clamp("sun.altitude_deg", 500) == 88.0
    assert clamp("sun.altitude_deg", -90) == -4.0
    assert clamp("sun.azimuth_deg", 370) == 10.0
    assert clamp("sun.azimuth_deg", -10) == 350.0
    assert clamp("group.practicals", 99) == 10.0


def test_unknown_param_raises():
    import pytest

    with pytest.raises(KeyError):
        clamp("nonsense.param", 1)
    assert spec_for("nonsense.param") is None
    assert spec_for("group.") is None            # empty group name is not a param
    assert spec_for("group.spots") is not None   # dynamic group spec


def test_limit_step_linear():
    # altitude step is 25 — a 60-degree jump gets limited
    assert limit_step("sun.altitude_deg", 10.0, 70.0) == 35.0
    assert limit_step("sun.altitude_deg", 70.0, 10.0) == 45.0


def test_limit_step_wrap_short_way():
    # 350 → 20 is +30 the short way, within the 60-degree azimuth step
    assert limit_step("sun.azimuth_deg", 350.0, 20.0) == 20.0
    # 0 → 180 is a 180-degree move, limited to +60
    assert limit_step("sun.azimuth_deg", 0.0, 180.0) == 60.0


def test_limit_step_log_scale():
    # intensity step is 1 in log2 — 1.0 → 8.0 (3 stops) limits to 2.0 (1 stop)
    assert abs(limit_step("sun.intensity", 1.0, 8.0) - 2.0) < 1e-9
    assert abs(limit_step("sun.intensity", 4.0, 0.1) - 2.0) < 1e-9


def test_apply_changes_validation():
    st = make_state()
    new, accepted, rejected = apply_changes(
        st,
        {
            "sun.altitude_deg": 45.0,          # fine
            "sun.azimuth_deg": "junk",         # non-numeric
            "bogus.param": 3,                  # unknown
            "exposure.ev": 5.0,                # locked below
            "group.practicals": 3.0,           # fine (log step 1 → limited to 2.0)
        },
        locks={"exposure.ev"},
    )
    assert accepted["sun.altitude_deg"] == 45.0
    assert abs(accepted["group.practicals"] - 2.0) < 1e-9
    assert new.get("exposure.ev") == 12.0
    assert any("locked" in r for r in rejected)
    assert any("unknown" in r for r in rejected)
    assert any("non-numeric" in r for r in rejected)
    # original untouched
    assert st.get("sun.altitude_deg") == 30.0


def test_state_table_flags_and_diff():
    st = make_state()
    table = state_table(st, locks={"dome.intensity", "sun.turbidity", "sun.intensity"})
    assert "ANALYTIC(hands-off)" in table
    assert "sun.azimuth_deg" in table and "wraps" in table
    other = make_state()
    other.set("sun.altitude_deg", 50.0)
    d = st.diff(other)
    assert d == {"sun.altitude_deg": (30.0, 50.0)}


def test_roundtrip_and_group_prefix():
    st = make_state()
    st2 = LightingState.from_dict(st.to_dict())
    assert st2.diff(st) == {}
    assert st2.get(GROUP_PREFIX + "practicals") == 1.0
    # junk keys dropped, values clamped on load
    st3 = LightingState.from_dict(
        {"values": {"sun.altitude_deg": 999, "hax": 1}, "groups": {"x": -5}})
    assert st3.get("sun.altitude_deg") == 88.0
    assert "hax" not in st3.values
    assert st3.groups["x"] == 0.0
