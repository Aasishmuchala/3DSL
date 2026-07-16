"""Prompt builders — schema-in-prompt (the gateway rejects tool calls), archviz-gaffer voice.

Three calls, mirroring the split-of-powers design:
  ANALYZE  reference image → lighting semantics (the LLM is a location scout's eye)
  DELTAS   ref + current render + state table → bounded parameter changes (the gaffer)
  SWEEP    ref + labeled sun-azimuth thumbnails → pick (multiple-choice beats estimation)

Everything numeric the model is asked for is bounded inline; everything it must not touch
(analytic exposure/WB, user locks) is spelled out in the state table it receives.
"""

from __future__ import annotations

import json
from typing import Dict, Sequence, Tuple

ANALYZE_SYSTEM = """You are a master gaffer and DP analyzing a LIGHTING REFERENCE image for an
architectural visualization match in 3ds Max + V-Ray. Study the image's light, not its content.

Reply with ONLY a JSON object, no prose, exactly this shape:
{
  "scene_type": "exterior" | "interior" | "interior_with_view",
  "time_of_day": "night" | "blue_hour" | "golden_hour" | "morning" | "midday" | "afternoon" | "overcast_day",
  "sky": "clear" | "hazy" | "overcast" | "dramatic" | "night",
  "sun_active": true | false,          // does direct sun shape this image at all
  "sun_bearing_deg": -180..180,        // horizontal angle from the CAMERA'S VIEW DIRECTION to
                                       // the sun: 0 = sun straight ahead (backlight), +90 =
                                       // camera-right, -90 = camera-left, ±180 = behind camera
                                       // (front-lit). Judge from shadow directions + highlights.
  "sun_altitude_band": "just_set" | "golden" | "low" | "mid" | "high" | "overhead" | "na",
  "light_quality": "hard" | "soft" | "mixed",
  "wb_kelvin_estimate": 2000..15000,   // the white-balance feel: ~3200 tungsten-warm,
                                       // 5500 neutral daylight, 7500+ cool/blue shade
  "practicals_on": true | false,       // are artificial/practical lights contributing
  "atmosphere": "none" | "light_haze" | "heavy_haze" | "fog",
  "contrast_character": "airy" | "balanced" | "moody",
  "key_notes": "<= 40 words of gaffer shorthand about what makes this light this light",
  "confidence": 0.0..1.0
}"""

DELTAS_SYSTEM = """You are a master gaffer iterating a V-Ray lighting rig toward a reference.
You will see the REFERENCE image first, then the CURRENT RENDER, then the rig's parameter
table and history. Different scenes — match the LIGHT (direction, quality, color mood,
contrast), never the content.

Hard rules:
- Propose AT MOST 4 parameter changes per iteration, highest-impact first.
- Only use parameter names from the table. Respect min..max. Changes beyond max_step get
  clamped, so propose within it.
- NEVER touch parameters flagged LOCKED or ANALYTIC(hands-off). Exposure and white balance
  are solved by a histogram solver — judge light RATIOS and DIRECTION, not overall brightness.
- Early iterations: fix geometry first (sun azimuth/altitude, dome rotation). Later
  iterations: refine quality and mood (sun size, turbidity, group balance).
- If the render already matches the reference's lighting character, return "stop": true with
  no changes. Do not churn.

Reply with ONLY a JSON object:
{
  "assessment": "<= 50 words comparing current render's LIGHT to the reference",
  "changes": [ {"param": "<name from table>", "value": <number>, "why": "<= 15 words"} ],
  "stop": true | false
}"""

SWEEP_SYSTEM = """You are a gaffer choosing a sun DIRECTION. You will see a REFERENCE image,
then N candidate renders of the SAME scene labeled by index, each with the sun at a different
compass azimuth. Pick the candidate whose shadow direction and lit-vs-shade pattern best
matches the reference's light direction. Ignore exposure/color differences — direction only.

Reply with ONLY a JSON object:
{"best_index": <0-based int>, "altitude_hint": "just_set"|"golden"|"low"|"mid"|"high"|"na",
 "why": "<= 20 words"}"""


def analyze_user_text() -> str:
    return ("Analyze the lighting of this reference image. "
            "Reply with only the JSON object.")


def deltas_user_text(
    state_table: str,
    semantics: Dict,
    score_history: Sequence[Tuple[int, float]],
    analytic_applied: Dict[str, float],
    iteration: int,
    max_iterations: int,
    rig_notes: str = "",
    param_history: str = "",
    director_note: str = "",
) -> str:
    hist = (" · ".join(f"iter{i}={s:.1f}" for i, s in score_history)
            if score_history else "no scores yet (metrics unavailable — judge visually)")
    analytic = (json.dumps({k: round(v, 2) for k, v in analytic_applied.items()})
                if analytic_applied else "none")
    trajectory = (f"Your parameter trajectory so far (do NOT oscillate — if a value has "
                  f"ping-ponged, hold it and change something else):\n{param_history}\n"
                  if param_history else "")
    note = (f"DIRECTOR'S NOTE — the human reviewed the match; obey this ABOVE the "
            f"reference analysis when they conflict:\n  {director_note}\n"
            if director_note else "")
    return f"""Image 1 = REFERENCE. Image 2 = CURRENT RENDER (iteration {iteration} of {max_iterations}).

Reference lighting analysis (from the scout pass):
{json.dumps(semantics, indent=1)}

Current rig parameter table:
{state_table}

{('Rig notes: ' + rig_notes) if rig_notes else ''}
Tonal-critic score history (0-100, higher = closer): {hist}
{note}{trajectory}Analytic solver already applied this iteration (do NOT counteract): {analytic}

Propose the next changes. Reply with only the JSON object."""


def sweep_user_text(azimuths: Sequence[float]) -> str:
    labels = ", ".join(f"candidate {i} = azimuth {a:.0f}°" for i, a in enumerate(azimuths))
    return (f"Image 1 = REFERENCE. The following {len(azimuths)} images are the candidates in "
            f"order: {labels}. Pick the best sun direction. Reply with only the JSON object.")
