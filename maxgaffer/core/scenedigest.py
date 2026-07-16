"""Scene digest formatting — the raw bridge dump becomes (a) the compact text the LLM
reads and (b) the CATALOG that grounds the planner.

"Know all the V-Ray settings" is implemented as introspection, not memorization: the bridge
dumps every property name+value it can read (renderer, environment map, exposure control,
every light, every camera), and the planner may only reference targets/properties that
appear in this catalog. That is what makes "no restrictions" safe from hallucinated
property names — full access to everything that EXISTS, zero access to what doesn't.

Pure python: the bridge hands over plain dicts; this module never imports pymxs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

MAX_PROPS_PER_TARGET_IN_TEXT = 48     # text digest trims; the CATALOG always stays complete
_PRIORITY_HINTS = (
    "on", "enabled", "multiplier", "intensity", "temperature", "color", "turbidity",
    "ozone", "size", "texmap", "mode", "ev", "iso", "f_number", "shutter", "white",
    "exposure", "gamma", "environment", "gi_", "lights", "sampler", "noise", "subdivs",
    "output", "camera", "sky", "albedo", "filter",
)


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_fmt_value(x) for x in v) + "]"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _sorted_props(props: Dict[str, Any]) -> List[str]:
    """Lighting-relevant names first, then alphabetical — so trimming keeps the signal."""
    def rank(name: str):
        low = name.lower()
        hit = min((i for i, h in enumerate(_PRIORITY_HINTS) if h in low),
                  default=len(_PRIORITY_HINTS))
        return (hit, low)

    return sorted(props.keys(), key=rank)


def catalog(raw: Dict) -> Dict[str, Set[str]]:
    """{target_id: {property names}} — the planner's grounding truth. Target ids:
    'renderer' · 'environment' · 'exposure' · 'node:<name>' (lights AND cameras)."""
    out: Dict[str, Set[str]] = {}
    for key in ("renderer", "environment", "exposure"):
        section = raw.get(key) or {}
        out[key] = set((section.get("props") or {}).keys())
    for group in ("lights", "cameras"):
        for item in raw.get(group) or []:
            name = item.get("name")
            if name:
                out[f"node:{name}"] = set((item.get("props") or {}).keys())
    return out


def to_text(raw: Dict, max_chars: int = 9000) -> str:
    """Compact, sectioned digest for the PLAN prompt. Complete inventories, trimmed
    property lists (priority-ranked); the trim note tells the model the catalog is larger."""
    lines: List[str] = []

    def section(title: str, d: Dict) -> None:
        props = d.get("props") or {}
        head = f"## {title}: {d.get('class', '?')}"
        lines.append(head)
        names = _sorted_props(props)
        for n in names[:MAX_PROPS_PER_TARGET_IN_TEXT]:
            lines.append(f"  {n} = {_fmt_value(props[n])}")
        if len(names) > MAX_PROPS_PER_TARGET_IN_TEXT:
            lines.append(f"  … +{len(names) - MAX_PROPS_PER_TARGET_IN_TEXT} more settable "
                         "properties exist on this target")

    section("RENDERER (target 'renderer')", raw.get("renderer") or {})
    section("ENVIRONMENT MAP (target 'environment')", raw.get("environment") or {})
    section("EXPOSURE CONTROL (target 'exposure')", raw.get("exposure") or {})

    lines.append(f"## LIGHTS ({len(raw.get('lights') or [])}) — target 'node:<name>'")
    for lt in raw.get("lights") or []:
        props = lt.get("props") or {}
        keys = _sorted_props(props)[:10]
        summary = " ".join(f"{k}={_fmt_value(props[k])}" for k in keys)
        lines.append(f"  {lt.get('name')} [{lt.get('class')}] layer={lt.get('layer', '?')} "
                     f"pos={_fmt_value(lt.get('pos', []))} · {summary}")

    lines.append(f"## CAMERAS ({len(raw.get('cameras') or [])}) — target 'node:<name>'")
    for cam in raw.get("cameras") or []:
        lines.append(f"  {cam.get('name')} [{cam.get('class')}] "
                     f"yaw={_fmt_value(cam.get('yaw_deg', 0))}° "
                     f"pos={_fmt_value(cam.get('pos', []))}")

    stats = raw.get("stats") or {}
    if stats:
        lines.append("## SCENE " + " ".join(f"{k}={_fmt_value(v)}" for k, v in stats.items()))

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 22] + "\n…digest truncated…"
    return text
