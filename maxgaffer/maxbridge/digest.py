"""Full scene + settings introspection — "get the current settings, see the current scene".

Dumps EVERY readable property (via MAXScript getPropNames) on: the current renderer (all
V-Ray render settings), the environment map, the exposure control, every light, every
camera — plus scene stats. Values are normalized to JSON-safe types; unreadable properties
are skipped per-property, never fatally. The result feeds core.scenedigest (LLM text +
planner catalog) and the change-report's before/after capture.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from . import scene as sc


def _rt():
    import pymxs

    return pymxs.runtime


def _norm(value) -> Any:
    """MAXScript value → JSON-safe python (numbers/bool/str/[r,g,b]/None)."""
    rt = _rt()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        if rt.classOf(value) == rt.Color:
            return [round(float(value.r), 1), round(float(value.g), 1),
                    round(float(value.b), 1)]
    except Exception:
        pass
    try:
        if rt.classOf(value) == rt.Point3:
            return [round(float(value.x), 2), round(float(value.y), 2),
                    round(float(value.z), 2)]
    except Exception:
        pass
    try:
        if rt.superClassOf(value) == rt.textureMap:
            return f"<map:{rt.classOf(value)}>"
    except Exception:
        pass
    s = str(value)
    return s if len(s) <= 80 else s[:77] + "…"


def dump_props(obj) -> Dict[str, Any]:
    """{prop_name: normalized value} for every property getPropNames can see."""
    rt = _rt()
    out: Dict[str, Any] = {}
    try:
        names = rt.getPropNames(obj)
    except Exception:
        return out
    for n in names:
        name = str(n)
        try:
            out[name] = _norm(getattr(obj, name))
        except Exception:
            continue   # write-only/context-dependent props exist; skip, never die
    return out


def _classed(obj) -> Dict[str, Any]:
    try:
        return {"class": str(_rt().classOf(obj)), "props": dump_props(obj)}
    except Exception:
        return {"class": "?", "props": {}}


def build_digest() -> Dict[str, Any]:
    rt = _rt()
    digest: Dict[str, Any] = {"renderer": {}, "environment": {}, "exposure": {},
                              "lights": [], "cameras": [], "stats": {}}
    try:
        digest["renderer"] = _classed(rt.renderers.current)
    except Exception:
        pass
    try:
        env = rt.environmentMap
        digest["environment"] = _classed(env) if env is not None else {
            "class": "none", "props": {}}
    except Exception:
        pass
    try:
        ec = rt.SceneExposureControl.exposureControl
        digest["exposure"] = _classed(ec) if ec is not None else {
            "class": "none", "props": {}}
    except Exception:
        pass

    try:
        for lt in rt.lights:
            cname = str(rt.classOf(lt)).lower()
            if cname == "targetobject":
                continue
            entry = _classed(lt)
            entry["name"] = str(lt.name)
            try:
                entry["layer"] = str(lt.layer.name)
            except Exception:
                entry["layer"] = "?"
            try:
                entry["pos"] = _norm(lt.pos)
            except Exception:
                pass
            digest["lights"].append(entry)
    except Exception:
        pass

    for cam in sc.list_cameras():
        node = sc.get_camera(cam["name"])
        entry: Dict[str, Any] = {"name": cam["name"], "class": cam["class"],
                                 "yaw_deg": round(cam["yaw_deg"], 1)}
        if node is not None:
            try:
                entry["pos"] = _norm(node.pos)
            except Exception:
                pass
            entry["props"] = dump_props(node)
        digest["cameras"].append(entry)

    try:
        digest["stats"] = {
            "objects": int(rt.objects.count),
            "lights": len(digest["lights"]),
            "cameras": len(digest["cameras"]),
            "frame": int(rt.currentTime.frame) if hasattr(rt.currentTime, "frame") else 0,
            "scene": str(rt.maxFileName or "unsaved"),
        }
    except Exception:
        pass
    return digest


def camera_basis(camera) -> Optional[Dict[str, Any]]:
    """Camera position + yaw + a look-at point — the anchor for relative light placement."""
    try:
        pos = camera.pos
        yaw = sc.camera_yaw_deg(camera)
        try:
            target = camera.target.pos
            look = [float(target.x), float(target.y), float(target.z)]
        except Exception:
            yr = math.radians(yaw)
            look = [float(pos.x) + math.sin(yr) * 200.0,
                    float(pos.y) + math.cos(yr) * 200.0, float(pos.z)]
        return {"pos": [float(pos.x), float(pos.y), float(pos.z)],
                "yaw_deg": yaw, "look": look}
    except Exception:
        return None
