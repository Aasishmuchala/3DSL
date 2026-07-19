"""Cluster E wire/parse/planner regressions — malformed 200 payloads, jittered backoff,
image size guard, and NaN/Infinity rejection at the validation boundary."""

import json
import math

import pytest

from maxgaffer.core import omega, parse
from maxgaffer.core.planner import validate_plan

CAT = {
    "renderer": {"environment_gi_on", "options_progressiveNoiseThreshold"},
    "node:VRaySun001": {"enabled", "turbidity", "intensity_multiplier"},
}


# --------------------------------------------------------------------------- extract_text
def test_extract_text_tolerates_null_and_missing_text():
    assert omega.extract_text({"content": [{"type": "text", "text": None}]}) == ""
    assert omega.extract_text({"content": [{"type": "text"}]}) == ""
    assert omega.extract_text({}) == ""                       # missing content list
    assert omega.extract_text({"content": None}) == ""


def test_extract_text_coerces_non_string_and_skips_non_text():
    payload = {"content": [
        {"type": "text", "text": 5},                        # non-string coerces
        {"type": "image", "source": {}},                    # non-text block skipped
        "garbage",                                          # non-dict block skipped
        {"type": "text", "text": "hello"},
    ]}
    assert omega.extract_text(payload) == "5\nhello"


def test_extract_text_non_dict_payload():
    assert omega.extract_text([1, 2, 3]) == ""
    assert omega.extract_text(None) == ""
    assert omega.extract_text("just a string") == ""


# --------------------------------------------------------------------------- call() contract
def test_call_malformed_200_never_raises_typeerror(monkeypatch):
    """Every malformed-200 flavour must surface as OmegaError, not a raw TypeError."""
    monkeypatch.setattr(omega, "BACKOFF_S", (0.0,))
    bad_bodies = [
        '{"content":[{"type":"text","text":null}]}',        # null text
        '{"content":[{"type":"text","text":{}}]}',          # non-string text
        '{"no_content": true}',                             # missing content list
        "[1,2,3]",                                          # JSON but not an object
        "<html>cloudflare</html>",                          # not JSON at all
    ]
    for body in bad_bodies:
        with pytest.raises(omega.OmegaError) as e:
            omega.call("oc_x", "s", [], post=lambda *a, _b=body: (200, _b))
        assert e.value.kind == "network"


def test_call_returns_text_despite_junk_blocks(monkeypatch):
    monkeypatch.setattr(omega, "BACKOFF_S", (0.0,))
    body = json.dumps({"content": [
        {"type": "text", "text": None},
        {"type": "text", "text": "real answer"},
    ]})
    assert omega.call("oc_x", "s", [], post=lambda *a: (200, body)) == "real answer"


def test_backoff_sleeps_are_jittered_but_bounded(monkeypatch):
    sleeps = []
    monkeypatch.setattr(omega.time, "sleep", sleeps.append)
    monkeypatch.setattr(omega, "BACKOFF_S", (2.0, 6.0, 15.0))
    with pytest.raises(omega.OmegaError):
        omega.call("oc_x", "s", [], post=lambda *a: (503, "down"))
    assert len(sleeps) == 3
    for base, slept in zip((2.0, 6.0, 15.0), sleeps):
        assert base <= slept <= base * 1.5                  # base + uniform(0, 50%)


# --------------------------------------------------------------------------- image size guard
def test_image_block_from_file_size_guard(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    assert omega.image_block_from_file(str(p), max_bytes=10) is None
    block = omega.image_block_from_file(str(p), max_bytes=4096)
    assert block is not None and block["source"]["media_type"] == "image/png"


def test_image_block_from_file_default_cap_matches_controller(tmp_path):
    p = tmp_path / "huge.jpg"
    p.write_bytes(b"\xff\xd8" + b"x" * 3_500_001)
    assert omega.image_block_from_file(str(p)) is None      # 3.5 MB default guard


# --------------------------------------------------------------------------- parse: non-finite
def test_validate_deltas_rejects_nan_and_infinity():
    d = parse.validate_deltas(
        '{"assessment":"x","changes":['
        '{"param":"sun.azimuth_deg","value":NaN,"why":"nan"},'
        '{"param":"dome.intensity","value":0.8,"why":"ok"},'
        '{"param":"sun.altitude_deg","value":Infinity},'
        '{"param":"dome.rotation_deg","value":-Infinity},'
        '{"param":"sun.turbidity","value":1e999}]}')
    assert d["changes"] == {"dome.intensity": 0.8}
    assert d["reasons"]["dome.intensity"] == "ok"


def test_validate_analysis_nonfinite_falls_back_to_default():
    a = parse.validate_analysis(
        '{"sun_bearing_deg":NaN,"wb_kelvin_estimate":Infinity,"confidence":NaN}')
    assert a["sun_bearing_deg"] == 0.0
    assert a["wb_kelvin_estimate"] == 6500.0
    assert a["confidence"] == 0.5
    assert math.isfinite(a["sun_bearing_deg"])


# --------------------------------------------------------------------------- planner: non-finite
def _plan(ops):
    return json.dumps({"read": "r", "ops": ops, "expects": "e"})


def test_plan_rejects_nonfinite_set_values():
    ops, rejected, _ = validate_plan(_plan([
        {"op": "set", "target": "node:VRaySun001", "prop": "turbidity",
         "value": float("nan"), "why": "nan"},
        {"op": "set", "target": "node:VRaySun001", "prop": "intensity_multiplier",
         "value": float("inf"), "why": "inf"},
        {"op": "set", "target": "node:VRaySun001", "prop": "turbidity",
         "value": [float("nan"), 255, 0], "why": "nan color"},
        {"op": "set", "target": "node:VRaySun001", "prop": "turbidity",
         "value": 4.0, "why": "finite"},
        {"op": "set", "target": "node:VRaySun001", "prop": "enabled",
         "value": True, "why": "bool still fine"},
    ]), CAT)
    values = [(o["prop"], o["value"]) for o in ops]
    assert ("turbidity", 4.0) in values
    assert ("enabled", True) in values
    assert all(math.isfinite(o["value"]) for o in ops
               if isinstance(o["value"], float))
    assert sum("nan" in r or "inf" in r.lower() for r in rejected) >= 3


def test_plan_create_light_drops_nonfinite_props():
    ops, _, _ = validate_plan(_plan([
        {"op": "create_light", "light_type": "VRayLight_plane", "name": "fill",
         "placement": {"bearing_deg": -70, "distance": 250, "height": 120},
         "props": {"multiplier": float("nan"), "color": [255, 230, 200],
                   "size": float("inf")},
         "why": "warm fill"},
    ]), CAT)
    assert len(ops) == 1
    props = ops[0]["props"]
    assert "multiplier" not in props and "size" not in props
    assert props["color"] == [255, 230, 200]
