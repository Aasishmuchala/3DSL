"""Scene introspection — cameras and the lighting rig, read defensively.

Property names across V-Ray builds drift, so every read/write goes through CANDIDATES lists
(first property that exists wins) and the classifier records what it could not find in
``rig["notes"]`` instead of raising. The README's on-box checklist walks these exact names.

Conventions shared with core:
  * camera yaw / sun azimuth: world compass bearing, 0 = +Y, clockwise, degrees;
  * sun altitude: degrees above horizon;
  * Max cameras look down their local -Z axis.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def _rt():
    import pymxs

    return pymxs.runtime


# ------------------------------------------------------------------ candidates plumbing
def get_prop(obj, names: Tuple[str, ...], default=None):
    rt = _rt()
    for n in names:
        try:
            if rt.isProperty(obj, rt.Name(n)):
                return getattr(obj, n)
        except Exception:
            continue
    return default


def set_prop(obj, names: Tuple[str, ...], value) -> Optional[str]:
    """Set the first existing property; returns the name used (None = none existed)."""
    rt = _rt()
    for n in names:
        try:
            if rt.isProperty(obj, rt.Name(n)):
                setattr(obj, n, value)
                return n
        except Exception:
            continue
    return None


SUN_INTENSITY = ("intensity_multiplier", "intensityMultiplier", "intensity")
SUN_SIZE = ("size_multiplier", "sizeMultiplier")
SUN_TURBIDITY = ("turbidity",)
LIGHT_ON = ("enabled", "on")
LIGHT_MULT = ("multiplier", "intensity")
DOME_TEX_ROT = ("horizontalRotation", "horizontal_rotation", "hRot", "tex_hrotation")


# ------------------------------------------------------------------ cameras
def _class_name(obj) -> str:
    try:
        return str(_rt().classOf(obj))
    except Exception:
        return ""


def camera_yaw_deg(cam) -> float:
    """World compass bearing of the camera's look direction (0 = +Y, clockwise)."""
    try:
        row3 = cam.transform.row3            # local Z axis in world space
        dx, dy = -float(row3.x), -float(row3.y)   # look = -Z
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return 0.0
        return math.degrees(math.atan2(dx, dy)) % 360.0
    except Exception:
        return 0.0


def list_cameras() -> List[Dict[str, Any]]:
    rt = _rt()
    out: List[Dict[str, Any]] = []
    try:
        for o in rt.cameras:                  # cameras collection excludes targets? — filter anyway
            cname = _class_name(o)
            if "target" in cname.lower() and "camera" not in cname.lower():
                continue                       # Targetobject helpers
            try:
                out.append({"name": str(o.name), "class": cname,
                            "yaw_deg": camera_yaw_deg(o)})
            except Exception:
                continue
    except Exception:
        pass
    return out


def get_camera(name: str):
    rt = _rt()
    try:
        return rt.getNodeByName(name, exact=True)
    except Exception:
        return None


def set_active_camera(name: str) -> bool:
    rt = _rt()
    cam = get_camera(name)
    if cam is None:
        return False
    try:
        rt.viewport.setCamera(cam)
        rt.redrawViews()
        return True
    except Exception:
        return False


def scene_path() -> str:
    rt = _rt()
    try:
        p = str(rt.maxFilePath or "")
        n = str(rt.maxFileName or "")
        return (p + n) if (p and n) else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ rig classification
def _is_class(obj, class_names: Tuple[str, ...]) -> bool:
    return _class_name(obj).lower() in tuple(c.lower() for c in class_names)


def _dome_type_value(light) -> Optional[int]:
    v = get_prop(light, ("type",))
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def classify_rig() -> Dict[str, Any]:
    """→ {"sun": node|None, "dome": node|None, "sky_env": bool,
          "groups": {name: [nodes]}, "notes": [str]}

    Groups = every non-dome VRayLight (and VRayIES), keyed by the light's LAYER name —
    archviz scenes organize practicals by layer, which makes layers the natural dimmer
    boards. Lights on the default layer land in group "practicals".
    """
    rt = _rt()
    rig: Dict[str, Any] = {"sun": None, "dome": None, "sky_env": False,
                           "groups": {}, "notes": []}
    try:
        lights = list(rt.lights)
    except Exception:
        lights = []
    for lt in lights:
        cname = _class_name(lt).lower()
        if cname == "vraysun":
            if rig["sun"] is None:
                rig["sun"] = lt
                try:
                    ctrl = str(rt.classOf(lt.transform.controller)).lower()
                    if "position" not in ctrl and "prs" not in ctrl:
                        rig["notes"].append(
                            f"sun '{lt.name}' transform controller is {ctrl} — a Daylight "
                            "assembly may fight MaxGaffer's sun moves")
                except Exception:
                    pass
            else:
                rig["notes"].append(f"extra VRaySun '{lt.name}' ignored (first one wins)")
        elif cname == "vraylight":
            # dome detection: V-Ray light .type — dome is expected to be 1 (VERIFY ON BOX)
            if _dome_type_value(lt) == 1 and rig["dome"] is None:
                rig["dome"] = lt
            elif _dome_type_value(lt) == 1:
                rig["notes"].append(f"extra dome '{lt.name}' ignored")
            else:
                _add_group_light(rig, lt)
        elif cname in ("vrayies", "vrayambientlight"):
            _add_group_light(rig, lt)
        # photometric/standard lights are left alone in v1 (V-Ray pipeline assumption)
    try:
        env = rt.environmentMap
        rig["sky_env"] = env is not None and "vraysky" in _class_name(env).lower()
    except Exception:
        pass
    if rig["sun"] is None:
        rig["notes"].append("no VRaySun found — sun.* parameters disabled")
    if rig["dome"] is None:
        rig["notes"].append("no VRayLight dome found — dome.* parameters disabled")
    return rig


def _add_group_light(rig: Dict[str, Any], lt) -> None:
    try:
        layer = str(lt.layer.name)
    except Exception:
        layer = "0"
    group = "practicals" if layer in ("0", "") else layer
    rig["groups"].setdefault(group, []).append(lt)


# ------------------------------------------------------------------ sun geometry
def _sun_pivot(sun):
    rt = _rt()
    try:
        tgt = getattr(sun, "target", None)
        if tgt is not None:
            return tgt.pos
    except Exception:
        pass
    return rt.Point3(0.0, 0.0, 0.0)


def read_sun_angles(sun) -> Tuple[float, float, float]:
    """→ (azimuth_deg, altitude_deg, distance). Direction FROM pivot TO sun position."""
    pivot = _sun_pivot(sun)
    try:
        d = sun.pos - pivot
        dx, dy, dz = float(d.x), float(d.y), float(d.z)
    except Exception:
        return 0.0, 45.0, 10000.0
    dist = math.sqrt(dx * dx + dy * dy + dz * dz) or 10000.0
    horiz = math.sqrt(dx * dx + dy * dy)
    altitude = math.degrees(math.atan2(dz, horiz))
    azimuth = math.degrees(math.atan2(dx, dy)) % 360.0 if horiz > 1e-6 else 0.0
    return azimuth, altitude, dist


def write_sun_angles(sun, azimuth_deg: float, altitude_deg: float) -> bool:
    rt = _rt()
    pivot = _sun_pivot(sun)
    _, _, dist = read_sun_angles(sun)
    az, alt = math.radians(azimuth_deg), math.radians(altitude_deg)
    try:
        sun.pos = rt.Point3(
            float(pivot.x) + dist * math.sin(az) * math.cos(alt),
            float(pivot.y) + dist * math.cos(az) * math.cos(alt),
            float(pivot.z) + dist * math.sin(alt),
        )
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ dome rotation
def read_dome_rotation(dome) -> float:
    tex = get_prop(dome, ("texmap",))
    if tex is not None:
        v = get_prop(tex, DOME_TEX_ROT)
        if v is not None:
            try:
                return float(v) % 360.0
            except (TypeError, ValueError):
                pass
    try:  # fall back to the node's world-Z euler
        rt = _rt()
        eul = rt.quatToEuler(dome.transform.rotationpart)
        return float(eul.z) % 360.0
    except Exception:
        return 0.0


def write_dome_rotation(dome, degrees_: float) -> str:
    """Prefer the HDRI texmap's horizontal-rotation spinner; fall back to spinning the node
    about WORLD Z at its own pivot. Returns which path was used (log/verify checklist).

    The fallback composes W' = W · T(−p) · Rz(Δ) · T(p) explicitly instead of rt.rotate(),
    whose working coordsys is context-dependent — a dome that a previous artist tilted
    would otherwise spin around the wrong axis."""
    tex = get_prop(dome, ("texmap",))
    if tex is not None:
        used = set_prop(tex, DOME_TEX_ROT, float(degrees_ % 360.0))
        if used:
            return f"texmap.{used}"
    try:
        rt = _rt()
        current = read_dome_rotation(dome)
        delta = (degrees_ - current + 180.0) % 360.0 - 180.0
        tm = dome.transform
        p = tm.translationpart
        neg_p = rt.Point3(-float(p.x), -float(p.y), -float(p.z))
        dome.transform = (tm * rt.transMatrix(neg_p)
                          * rt.rotateZMatrix(float(delta)) * rt.transMatrix(p))
        return "node_z_rotation"
    except Exception:
        return "failed"
