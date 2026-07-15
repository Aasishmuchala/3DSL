"""Kelvin → RGB white-balance math for exposure hosts that only expose a WB *color*.

The equivalence that matters (verified reasoning, flagged for on-box visual check):
a camera's WB temperature spinner and its WB color swatch both mean "this is the scene's
illuminant — neutralize it". Swatch = the illuminant's own color, i.e. ``kelvin_to_rgb(K)``:
  * K = 3200 → orange swatch → camera divides out orange → image renders COOLER;
  * K = 9000 → blue swatch  → camera divides out blue  → image renders WARMER.
Both match the V-Ray spinner convention the solver uses (raise kelvin → warmer render), so
the bridge can write either the spinner or the swatch from the same genome value.
"""

from __future__ import annotations

import math
from typing import Tuple

NEUTRAL_K = 6500.0


def kelvin_to_rgb(kelvin: float) -> Tuple[float, float, float]:
    """Approximate sRGB (0..1) of blackbody light at ``kelvin`` (Tanner Helland fit,
    clamped 1000..40000)."""
    k = min(40000.0, max(1000.0, float(kelvin))) / 100.0
    if k <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(k) - 161.1195681661
    else:
        r = 329.698727446 * ((k - 60.0) ** -0.1332047592)
        g = 288.1221695283 * ((k - 60.0) ** -0.0755148492)
    if k >= 66:
        b = 255.0
    elif k <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(k - 10.0) - 305.0447927307

    def clamp01(v: float) -> float:
        return min(1.0, max(0.0, v / 255.0))

    return clamp01(r), clamp01(g), clamp01(b)


def wb_color_for_kelvin(kelvin: float) -> Tuple[float, float, float]:
    """WB swatch color equivalent to a temperature-spinner setting of ``kelvin``."""
    return kelvin_to_rgb(kelvin)
