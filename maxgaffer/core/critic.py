"""Tonal critic — a deterministic 0-100 "how close is the lighting mood" score.

Reference and render are DIFFERENT SCENES, so no SSIM/feature matching: the score is built
only from statistics that transfer across scenes — exposure key, tonal envelope, chromatic
mood. It is the loop's accept/revert arbiter and convergence signal, not a beauty judge; the
LLM (and the human) own the last mile of taste, exactly like MaxDirector's geometric critic
gates its storyboards.

Components (weights config-tunable, must sum to 1):
  key       exposure match — log2 distance between geometric-mean linear luminances
  envelope  shadow/highlight placement — p5 + p95 luminance deltas
  histogram luminance distribution shape — 1-D EMD
  color     chromatic mood — LAB mean distance (a*, b* weighted over L)
  hue       hue distribution — chroma-weighted cosine similarity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .metrics import cosine, hist_emd

DEFAULT_WEIGHTS: Dict[str, float] = {
    "key": 0.22,
    "envelope": 0.18,
    "histogram": 0.20,
    "color": 0.25,
    "hue": 0.15,
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


def score(ref: Dict, cur: Dict, weights: Dict[str, float] = None) -> Verdict:
    import math

    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    total_w = sum(w.values()) or 1.0

    key_ref = max(1e-5, float(ref.get("log_key", 0.0)))
    key_cur = max(1e-5, float(cur.get("log_key", 0.0)))
    s_key = 1.0 - min(1.0, abs(math.log2(key_ref / key_cur)) / 3.0)

    d5 = abs(_sub(ref, "p", "5") - _sub(cur, "p", "5"))
    d95 = abs(_sub(ref, "p", "95") - _sub(cur, "p", "95"))
    s_env = 1.0 - min(1.0, (d5 + d95) / 0.5)

    s_hist = 1.0 - min(1.0, hist_emd(ref.get("lum_hist", []), cur.get("lum_hist", [])) * 4.0)

    lr, lc = ref.get("lab_mean", [0, 0, 0]), cur.get("lab_mean", [0, 0, 0])
    d_col = math.sqrt(0.4 * (lr[0] - lc[0]) ** 2 + (lr[1] - lc[1]) ** 2 + (lr[2] - lc[2]) ** 2)
    s_col = 1.0 - min(1.0, d_col / 30.0)

    s_hue = max(0.0, cosine(ref.get("hue_hist", []), cur.get("hue_hist", [])))

    comps = {"key": s_key, "envelope": s_env, "histogram": s_hist, "color": s_col, "hue": s_hue}
    total = sum(w[k] * comps[k] for k in comps) / total_w
    return Verdict(score=round(100.0 * total, 2), components=comps)
