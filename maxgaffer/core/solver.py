"""Deterministic exposure + white-balance solve — math owns what math can own.

The single most reliable move in lighting matching: exposure and WB are *measurable*, so we
never let the LLM guess them (MaxDirector lesson: LLMs hallucinate spatial/metric precision;
anchor with computed values). Every iteration, before asking the LLM anything, we:

  EV  — compare the geometric-mean linear luminance ("key") of reference vs render.
        dEV = log2(key_ref / key_cur). V-Ray EV semantics: HIGHER EV = DARKER image, so a
        render darker than the reference (dEV > 0) needs new_ev = ev - dEV.  Center-weighted
        keys, per-iteration clamp and a deadband keep it from chasing noise.

  WB  — compare LAB b* means (blue-yellow axis). V-Ray white-balance temperature semantics:
        raising the WB kelvin renders WARMER (the camera compensates for a bluer assumed
        illuminant). If the reference is warmer than the render (db > 0) we raise kelvin.
        ~90 K per b* unit is an empirical slope; the visual sign-check lives in the on-box
        checklist and the slope is config-tunable.

Both return None inside their deadband so the caller can skip a no-op scene write.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from .genome import LightingState, clamp

EV_DEADBAND = 0.15
EV_MAX_STEP = 2.5
WB_DEADBAND_B = 1.5          # LAB b* units
WB_KELVIN_PER_B = 90.0
WB_MAX_STEP = 1500.0


def solve_ev(ref_stats: Dict, cur_stats: Dict, current_ev: float,
             tighten: float = 1.0) -> Optional[float]:
    """New EV that matches the render's key to the reference's, or None if close enough.
    ``tighten`` < 1 shrinks the deadband (and the per-step cap) as the match converges —
    a 0.15-stop tolerance is exploration slack, not a finishing standard."""
    key_ref = max(1e-5, float(ref_stats.get("log_key", 0.0)))
    key_cur = max(1e-5, float(cur_stats.get("log_key", 0.0)))
    d_ev = math.log2(key_ref / key_cur)
    if abs(d_ev) < EV_DEADBAND * max(0.1, tighten):
        return None
    # NOTE: only the DEADBAND anneals — the correction cap stays full-size, because a
    # measured 2-stop error deserves a 2-stop fix regardless of how well the rest of the
    # match is going (the cap is a stability rail, not a convergence knob)
    d_ev = max(-EV_MAX_STEP, min(EV_MAX_STEP, d_ev))
    return clamp("exposure.ev", current_ev - d_ev)


def solve_wb(ref_stats: Dict, cur_stats: Dict, current_kelvin: float,
             kelvin_per_b: float = WB_KELVIN_PER_B, tighten: float = 1.0) -> Optional[float]:
    """New WB kelvin nudging the render's blue-yellow balance toward the reference.

    Prefers HIGHLIGHT chromaticity (top luminance quartile — the white-patch assumption:
    highlights carry the illuminant, the full mean carries the furniture). This is the
    direct counter to the albedo trap; falls back to full-frame means on old stats."""
    def b_of(stats: Dict) -> float:
        hi = stats.get("lab_mean_hi")
        src = hi if isinstance(hi, (list, tuple)) and len(hi) == 3 else stats.get(
            "lab_mean", [0, 0, 0])
        return float(src[2])

    use_hi = ("lab_mean_hi" in ref_stats) == ("lab_mean_hi" in cur_stats)
    if use_hi:
        b_ref, b_cur = b_of(ref_stats), b_of(cur_stats)
    else:   # never compare a highlight mean against a full mean — different quantities
        b_ref = float(ref_stats.get("lab_mean", [0, 0, 0])[2])
        b_cur = float(cur_stats.get("lab_mean", [0, 0, 0])[2])
    db = b_ref - b_cur
    if abs(db) < WB_DEADBAND_B * max(0.1, tighten):
        return None
    delta = max(-WB_MAX_STEP, min(WB_MAX_STEP, db * kelvin_per_b))
    return clamp("exposure.wb_kelvin", current_kelvin + delta)


def analytic_pass(
    state: LightingState,
    ref_stats: Dict,
    cur_stats: Dict,
    locks: Optional[set] = None,
    tighten: float = 1.0,
) -> Dict[str, float]:
    """The changes the solver wants this iteration ({} when everything is in the deadband).

    Capability-gated: a key absent from ``state`` means the rig has no host for it
    (read_state only includes supported params) — proposing it anyway would create a
    phantom parameter the bridge warns about every iteration and, worse, walk the leash
    into a false albedo diagnosis while changing nothing on screen."""
    locks = locks or set()
    changes: Dict[str, float] = {}
    if "exposure.ev" in state.values and "exposure.ev" not in locks:
        ev = solve_ev(ref_stats, cur_stats, state.get("exposure.ev"), tighten)
        if ev is not None:
            changes["exposure.ev"] = ev
    if "exposure.wb_kelvin" in state.values and "exposure.wb_kelvin" not in locks:
        wb = solve_wb(ref_stats, cur_stats, state.get("exposure.wb_kelvin"),
                      tighten=tighten)
        if wb is not None:
            changes["exposure.wb_kelvin"] = wb
    return changes
