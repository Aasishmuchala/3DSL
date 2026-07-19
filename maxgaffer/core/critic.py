"""Tonal critic — a deterministic 0-100 "how close is the lighting mood" score.

Reference and render are DIFFERENT SCENES, so no SSIM/feature matching: the score is built
only from statistics that transfer across scenes — exposure key, tonal envelope, chromatic
mood. It is the loop's accept/revert arbiter and convergence signal, not a beauty judge; the
LLM (and the human) own the last mile of taste, exactly like MaxDirector's geometric critic
gates its storyboards.

Components (weights config-tunable; renormalized over whatever was measurable):
  key       exposure match — log2 distance between geometric-mean linear luminances
  envelope  shadow/highlight placement — p5 + p95 luminance deltas
  histogram luminance distribution shape — 1-D EMD
  color     chromatic mood — LAB mean distance (a*, b* weighted over L)
  hue       hue distribution — chroma-weighted cosine similarity
  direction WHERE the light lives — cosine of mean-centered 3×3 luminance grids (the one
            spatial signal that transfers across different scenes lit the same way)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List

from .metrics import cosine, hist_emd

DEFAULT_WEIGHTS: Dict[str, float] = {
    "key": 0.19,
    "envelope": 0.15,
    "histogram": 0.17,
    "color": 0.21,
    "hue": 0.13,
    "direction": 0.15,   # 3×3 luminance-grid cosine — WHERE the light lives
}


@dataclass
class Verdict:
    score: float                      # 0..100
    components: Dict[str, float] = field(default_factory=dict)   # each 0..1

    def summary(self) -> str:
        parts = ", ".join(f"{k}={v:.2f}" for k, v in sorted(self.components.items()))
        return f"{self.score:.1f}/100 ({parts})"


def _sub(d: Dict, *path, default=0.0):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _num(value, default: float = 0.0) -> float:
    """Finite-float coercion for stats fields — stats cross the sidecar trust boundary
    unvalidated, so a present-but-mistyped field (None, "junk", NaN) must degrade to the
    default, not raise on Max's main thread mid-loop."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _seq(value) -> List[float]:
    """Numeric-list coercion (histograms, grids): anything else → empty list."""
    if not isinstance(value, (list, tuple)):
        return []
    return [_num(v) for v in value]


def _lab(value) -> List[float]:
    """LAB mean coercion: exactly-3 numeric, else the neutral default."""
    if not (isinstance(value, (list, tuple)) and len(value) == 3):
        return [0.0, 0.0, 0.0]
    return [_num(v) for v in value]


def score(ref: Dict, cur: Dict, weights: Dict[str, float] = None) -> Verdict:
    w = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        # config.json is user-editable (tasks/plan.md says so) — accept only known keys
        # with finite, non-negative floats; anything else keeps the default weight
        for k, v in weights.items():
            if k not in w:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(fv) and fv >= 0.0:
                w[k] = fv

    key_ref = max(1e-5, _num(ref.get("log_key", 0.0)))
    key_cur = max(1e-5, _num(cur.get("log_key", 0.0)))
    s_key = 1.0 - min(1.0, abs(math.log2(key_ref / key_cur)) / 3.0)

    d5 = abs(_num(_sub(ref, "p", "5")) - _num(_sub(cur, "p", "5")))
    d95 = abs(_num(_sub(ref, "p", "95")) - _num(_sub(cur, "p", "95")))
    s_env = 1.0 - min(1.0, (d5 + d95) / 0.5)

    s_hist = 1.0 - min(1.0, hist_emd(_seq(ref.get("lum_hist")), _seq(cur.get("lum_hist"))) * 4.0)

    lr, lc = _lab(ref.get("lab_mean")), _lab(cur.get("lab_mean"))
    d_col = math.sqrt(0.4 * (lr[0] - lc[0]) ** 2 + (lr[1] - lc[1]) ** 2 + (lr[2] - lc[2]) ** 2)
    s_col = 1.0 - min(1.0, d_col / 30.0)

    comps = {"key": s_key, "envelope": s_env, "histogram": s_hist, "color": s_col}
    # hue carries information only when BOTH sides are chromatic — two achromatic images
    # (empty hue vectors) score cosine 1.0 for "no hue information", inflating exactly
    # the degenerate pairs; skip the component and renormalize, mirroring direction
    hue_ref, hue_cur = _seq(ref.get("hue_hist")), _seq(cur.get("hue_hist"))
    if any(abs(v) > 1e-6 for v in hue_ref) and any(abs(v) > 1e-6 for v in hue_cur):
        comps["hue"] = max(0.0, cosine(hue_ref, hue_cur))
    # prefer the finer 5×5 grid when both sides carry it (better azimuth acuity);
    # 3×3 remains for stats produced by older engine versions
    g_ref, g_cur = _seq(ref.get("grid5")), _seq(cur.get("grid5"))
    if not (g_ref and g_cur and len(g_ref) == len(g_cur)):
        g_ref, g_cur = _seq(ref.get("grid")), _seq(cur.get("grid"))
    if g_ref and g_cur and len(g_ref) == len(g_cur) and any(abs(v) > 1e-6 for v in g_ref):
        comps["direction"] = max(0.0, (cosine(g_ref, g_cur) + 1.0) / 2.0)
    # only weigh what was measurable — old stats without a grid renormalize cleanly
    total_w = sum(w[k] for k in comps) or 1.0
    total = sum(w[k] * comps[k] for k in comps) / total_w
    return Verdict(score=round(100.0 * total, 2), components=comps)
