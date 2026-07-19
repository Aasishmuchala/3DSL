"""Cluster D regressions (FIXER brief D_domeseed_scenarios):

* scenarios: a hand-edited/corrupted sidecar semantics cache (json.load accepts
  NaN/Infinity literals) must never push NaN/inf/non-numeric values through
  rules.initial_state into a scene write.
* domeseed: a .hdr WRITE failure is not an unreadable reference (typed SeedError),
  and the pano_path seam ingests Radiance .hdr equirects via hdr_min.read_hdr
  (SPEC §10's DiffusionLight-class gate) without an 8-bit crush; .exr fails loudly.

Pure python, off-Max; .hdr fixtures are built with hdr_min itself (stdlib).
"""

import math

import pytest

from maxgaffer.core import domeseed, hdr_min, scenarios
from maxgaffer.core.genome import LightingState


# --------------------------------------------------------------------------- helpers
def _full_rig_state() -> LightingState:
    st = LightingState()
    st.set("sun.enabled", 1)
    st.set("sun.azimuth_deg", 120.0)
    st.set("sun.altitude_deg", 35.0)
    st.set("sun.intensity", 1.0)
    st.set("sun.size", 3.0)
    st.set("sun.turbidity", 3.0)
    st.set("dome.enabled", 1)
    st.set("dome.rotation_deg", 0.0)
    st.set("dome.intensity", 1.0)
    st.set("exposure.ev", 12.0)
    st.set("exposure.wb_kelvin", 6500.0)
    st.groups["practicals"] = 1.0
    return st


def _write_pano_hdr(path, painter, w=96, h=32):
    rows = [[painter(x, y) for x in range(w)] for y in range(h)]
    assert hdr_min.write_hdr(str(path), rows)
    return str(path)


def _assert_board_finite(board):
    assert board, "expected a non-empty board"
    for b in board:
        for k in b["state"].keys():
            assert math.isfinite(b["state"].get(k)), \
                f"{b['key']}: non-finite value at {k}"


# --------------------------------------------------------------------------- scenarios
def test_sanitize_semantics_drops_and_clamps():
    out = scenarios.sanitize_semantics({
        "sun_bearing_deg": float("nan"),
        "wb_kelvin_estimate": 99999.0,          # out of range → clamped, kept
        "confidence": 2.5,                       # out of range → clamped, kept
        "sky": "clear",                          # strings pass
        "sun_active": False,                     # bools pass
        "atmosphere": float("inf"),              # unknown numeric key, non-finite → drop
        "scene_type": {"nested": 1},             # container junk → drop
        "time_of_day": ["morning"],              # container junk → drop
    })
    assert "sun_bearing_deg" not in out
    assert "atmosphere" not in out
    assert "scene_type" not in out and "time_of_day" not in out
    assert out["wb_kelvin_estimate"] == 15000.0
    assert out["confidence"] == 1.0
    assert out["sky"] == "clear" and out["sun_active"] is False


def test_board_rejects_nan_bearing_from_cache():
    """Corrupted cache: "sun_bearing_deg": NaN used to reach genome.clamp's wrap path
    (fmod(nan, 360) = nan) and land in sun.azimuth_deg → NaN scene write."""
    sem = {"sky": "clear", "sun_active": True, "time_of_day": "afternoon",
           "sun_bearing_deg": float("nan")}
    board = scenarios.build_scenarios(sem, _full_rig_state(), camera_yaw_deg=30.0)
    _assert_board_finite(board)
    assert board[0]["key"] == "as_analyzed"
    # the NaN bearing is rejected → neutral-base bearing (−60°) → 30 − 60 → wraps to 330
    assert abs(board[0]["state"].get("sun.azimuth_deg") - 330.0) < 1e-6


def test_board_rejects_inf_and_nan_wb_from_cache():
    sem = {"sky": "clear", "sun_active": True, "time_of_day": "afternoon",
           "sun_bearing_deg": float("-inf"),
           "wb_kelvin_estimate": float("nan")}
    board = scenarios.build_scenarios(sem, _full_rig_state(), camera_yaw_deg=45.0)
    _assert_board_finite(board)
    assert abs(board[0]["state"].get("sun.azimuth_deg") - 345.0) < 1e-6   # 45 − 60 wrap


def test_board_survives_non_numeric_strings_from_cache():
    """"sun_bearing_deg": "west-ish" used to raise ValueError inside float() on the
    board path (rules.initial_state) — an uncaught crash mid-run."""
    sem = {"sky": "clear", "sun_active": True, "time_of_day": "afternoon",
           "sun_bearing_deg": "west-ish", "wb_kelvin_estimate": "warm"}
    board = scenarios.build_scenarios(sem, _full_rig_state(), camera_yaw_deg=10.0)
    _assert_board_finite(board)
    assert abs(board[0]["state"].get("sun.azimuth_deg") - 310.0) < 1e-6   # 10 − 60 wrap


def test_board_clamps_out_of_range_bearing():
    sem = {"sky": "clear", "sun_active": True, "time_of_day": "afternoon",
           "sun_bearing_deg": 999.0}
    board = scenarios.build_scenarios(sem, _full_rig_state(), camera_yaw_deg=30.0)
    _assert_board_finite(board)
    # clamped to the parse bound (±180) → yaw 30 + 180 = 210
    assert abs(board[0]["state"].get("sun.azimuth_deg") - 210.0) < 1e-6


def test_board_valid_semantics_pass_through_unmangled():
    sem = {"sky": "clear", "sun_active": True, "time_of_day": "golden_hour",
           "sun_bearing_deg": -60.0, "wb_kelvin_estimate": 3800.0,
           "light_quality": "hard", "atmosphere": "light_haze"}
    board = scenarios.build_scenarios(sem, _full_rig_state(), camera_yaw_deg=100.0)
    _assert_board_finite(board)
    first = board[0]["state"]
    assert abs(first.get("sun.azimuth_deg") - 40.0) < 1e-6    # 100 + (−60)
    assert abs(first.get("exposure.wb_kelvin") - 3800.0) < 1e-6


# --------------------------------------------------------------------------- domeseed
def test_build_seed_missing_reference_still_returns_none(tmp_path):
    """Read failures keep the None contract (the bridge retries via Max transcode)."""
    out = str(tmp_path / "seed.hdr")
    assert domeseed.build_seed(out, ref_path=str(tmp_path / "missing.png")) is None
    assert domeseed.build_seed(out, pano_path=str(tmp_path / "missing.hdr")) is None


def test_build_seed_write_failure_raises_seed_error(tmp_path):
    """Read-only/full/over-long output: used to return None → the bridge reported
    'could not read the reference for seeding' though the reference read fine."""
    pano = _write_pano_hdr(tmp_path / "sky.hdr", lambda x, y: (0.4, 0.5, 0.9))
    bad_out = str(tmp_path / "no_such_dir" / "seed.hdr")     # OSError on open
    with pytest.raises(domeseed.SeedError) as exc:
        domeseed.build_seed(bad_out, pano_path=pano, out_w=32, out_h=16,
                            semantics={"sun_active": False})
    assert "WRITE" in str(exc.value).upper()
    assert "no_such_dir" in str(exc.value)


def test_build_seed_exr_pano_fails_with_clear_message(tmp_path):
    exr = tmp_path / "sky.exr"
    exr.write_bytes(b"\x76\x2f\x31\x01")                      # EXR magic, unreadable here
    with pytest.raises(domeseed.SeedError) as exc:
        domeseed.build_seed(str(tmp_path / "seed.hdr"), pano_path=str(exr))
    msg = str(exc.value).lower()
    assert "exr" in msg and "hdr" in msg                     # says what + what to do


def test_build_seed_hdr_pano_ingest_end_to_end(tmp_path):
    """SPEC §10's gate: an external Radiance .hdr equirect flows through
    read_hdr → orient → write, reported as source 'pano'."""
    pano = _write_pano_hdr(tmp_path / "ext.hdr", lambda x, y: (0.3, 0.6, 0.2))
    out = str(tmp_path / "seed.hdr")
    meta = domeseed.build_seed(out, pano_path=pano, cam_yaw_deg=0.0,
                               out_w=64, out_h=32,
                               semantics={"sun_active": False, "sky": "clear"})
    assert meta is not None and meta["source"] == "pano"
    rows = hdr_min.read_hdr(out)
    assert rows is not None and len(rows) == 32 and len(rows[0]) == 64


def test_hdr_pano_stays_hdr_through_ingest(tmp_path):
    """The LDR path crushes to 8-bit before sun injection; the .hdr path must not.
    A 100:1 brightness split survives with its ratio (8-bit + LUT would clamp it)."""
    pano = _write_pano_hdr(
        tmp_path / "range.hdr",
        lambda x, y: (100.0, 0.0, 0.0) if x >= 48 else (1.0, 0.0, 0.0))
    out = str(tmp_path / "seed.hdr")
    meta = domeseed.build_seed(out, pano_path=pano, cam_yaw_deg=180.0,
                               out_w=96, out_h=16,
                               semantics={"sun_active": False, "sky": "clear"})
    assert meta is not None
    rows = hdr_min.read_hdr(out)
    mid = 8                                                   # horizon band row
    reds = [rows[mid][x][0] for x in range(96)]
    assert max(reds) / max(min(reds), 1e-9) > 20.0, \
        "dynamic range collapsed — the pano was crushed through an 8-bit path"


def test_hdr_pano_orientation_matches_ldr_convention(tmp_path):
    # camera-forward at u=0.5; dark left half, bright right half; yaw 90 → world az 100
    # (10° right of forward) must be brighter than az 80
    pano = _write_pano_hdr(
        tmp_path / "orient.hdr",
        lambda x, y: (0.05, 0.05, 0.05) if x < 48 else (0.8, 0.8, 0.8))
    out = str(tmp_path / "seed.hdr")
    meta = domeseed.build_seed(out, pano_path=pano, cam_yaw_deg=90.0,
                               out_w=64, out_h=16,
                               semantics={"sun_active": False, "sky": "clear"})
    assert meta is not None
    rows = hdr_min.read_hdr(out)
    y = 8
    left = rows[y][int((80.0 / 360.0) * 64)]
    right = rows[y][int((100.0 / 360.0) * 64)]
    assert right[0] > left[0], "hdr pano landed mirrored vs the LDR ingest convention"


def test_ingest_pano_hdr_empty_is_safe():
    assert domeseed.ingest_pano_hdr([], 0.0, out_w=8, out_h=4) == []
