"""Round-3 stress regressions — attacks on the v0.5 agent mode that landed."""


from maxgaffer.core import scenedigest
from maxgaffer.core.genome import LightingState
from maxgaffer.core.planner import validate_plan
from maxgaffer.core.rules import initial_state

from tests.test_planner_digest import CAT, RAW, plan_reply


# ---------------------------------------------- refine loop must not kill plan lights
def test_rules_never_zero_mg_groups():
    st = LightingState()
    st.set("sun.enabled", 1)
    st.set("sun.azimuth_deg", 0.0)
    st.set("sun.altitude_deg", 45.0)
    st.groups["practicals"] = 1.0
    st.groups["MG_lights"] = 1.0          # the plan JUST created these fills
    sem = {"time_of_day": "golden_hour", "sky": "clear", "sun_active": True,
           "sun_bearing_deg": 0.0, "sun_altitude_band": "golden",
           "light_quality": "soft", "wb_kelvin_estimate": 5000.0,
           "practicals_on": False, "atmosphere": "none",
           "contrast_character": "balanced", "key_notes": "", "confidence": 0.9}
    out, why = initial_state(sem, st, camera_yaw_deg=0.0)
    assert out.groups["practicals"] == 0.0      # scene practicals off for daylight ✓
    assert out.groups["MG_lights"] == 1.0       # plan instruments SURVIVE
    # practicals_on=True still boosts MG groups (harmless, they participate)
    sem["practicals_on"] = True
    out2, _ = initial_state(sem, st, camera_yaw_deg=0.0)
    assert out2.groups["MG_lights"] >= 1.0


# ---------------------------------------------- digest truncation must eat only lights
def test_digest_truncation_preserves_cameras_and_names_the_tail():
    big = dict(RAW)
    big["lights"] = [{"name": f"IES_{i:03d}", "class": "VRayIES", "layer": "sconces",
                      "pos": [i, 0, 100], "props": {"on": True, "multiplier": 5.0}}
                     for i in range(200)]
    text = scenedigest.to_text(big)
    assert "CAMERAS (1)" in text and "PhysCam_Hero" in text   # cameras BEFORE lights now
    assert "LIGHTS (200)" in text
    assert "more lights, ALL still valid" in text             # loud cap, names listed
    assert "IES_050" in text                                  # tail names present
    # catalog unaffected by any text trimming — all 200 remain targetable
    cat = scenedigest.catalog(big)
    assert "node:IES_199" in cat


# ---------------------------------------------- renderer plumbing denylist
def test_renderer_output_and_system_props_refused():
    cat = dict(CAT)
    cat["renderer"] = set(CAT["renderer"]) | {"output_saveFileName", "system_numThreads",
                                              "options_distributedRender"}
    ops, rejected, _ = validate_plan(plan_reply([
        {"op": "set", "target": "renderer", "prop": "output_saveFileName",
         "value": "C:/evil.png", "why": "x"},
        {"op": "set", "target": "renderer", "prop": "system_numThreads", "value": 1,
         "why": "x"},
        {"op": "set", "target": "renderer", "prop": "options_distributedRender",
         "value": True, "why": "x"},
        {"op": "set", "target": "renderer", "prop": "environment_gi_on", "value": True,
         "why": "legit light-related setting"},
    ]), cat)
    assert [o["prop"] for o in ops] == ["environment_gi_on"]
    assert sum("off-limits" in r for r in rejected) == 3


# ---------------------------------------------- set on a light created earlier in-plan
def test_set_allowed_on_plan_created_light_only_after_creation():
    ops, rejected, _ = validate_plan(plan_reply([
        {"op": "set", "target": "node:MG_rim", "prop": "multiplier", "value": 4.0,
         "why": "too early — not created yet"},
        {"op": "create_light", "light_type": "VRayLight_disc", "name": "MG_rim",
         "placement": {"bearing_deg": 120, "distance": 300, "height": 200}, "why": "rim"},
        {"op": "set", "target": "node:MG_rim", "prop": "invisible", "value": True,
         "why": "after creation — allowed even though not in digest"},
    ]), CAT)
    kinds = [(o["op"], o.get("prop")) for o in ops]
    assert ("set", "invisible") in kinds
    assert ("set", "multiplier") not in kinds       # pre-creation reference rejected
    assert any("unknown target" in r for r in rejected)


# ---------------------------------------------- placement note is enforced in prompt
def test_prompt_carries_round3_contracts():
    from maxgaffer.core.planner import PLAN_SYSTEM

    assert "ALL THREE numbers" in PLAN_SYSTEM
    assert "VRayIES" in PLAN_SYSTEM and ".ies profile" in PLAN_SYSTEM
