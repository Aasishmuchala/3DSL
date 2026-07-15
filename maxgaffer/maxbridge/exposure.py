"""Exposure host abstraction — one EV + one WB-kelvin, whatever the scene actually uses.

Host priority (first available wins):
  1. scene-level VRay exposure control (camera-agnostic, the modern V-Ray way)
  2. the active camera, if it's a Max Physical or legacy VRayPhysicalCamera —
     EV is realized by moving ISO ONLY (f-stop = DOF and shutter = motion blur are the
     photographer's, not the gaffer's)
  3. none → exposure.ev / exposure.wb_kelvin report unsupported and the UI auto-locks them.

EV convention throughout: EV100 = log2(N² / t) - log2(ISO / 100); HIGHER = DARKER.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from ..core.colortemp import wb_color_for_kelvin
from .scene import get_prop, set_prop

EC_EV = ("ev", "EV", "exposure_value")
EC_WB_MODE = ("whitebalance_mode", "wb_mode")
EC_WB_KELVIN = ("temperature", "whitebalance_temperature", "wb_temperature")
EC_WB_COLOR = ("whitebalance", "white_balance", "wb_color")

CAM_ISO = ("ISO", "iso", "film_speed", "exposure_gain_iso")
CAM_FNUM = ("f_number", "fnumber", "f_stop", "aperture_f_number")
CAM_SHUTTER = ("shutter_speed", "shutter_speed_value")   # 1/t as "speed" on both cams
CAM_WB_KELVIN = ("temperature", "white_balance_kelvin", "whiteBalance_temperature")
CAM_WB_COLOR = ("whiteBalance", "white_balance_color", "wb_color")
CAM_WB_MODE = ("white_balance_type", "whiteBalance_mode", "wb_mode")


def _rt():
    import pymxs

    return pymxs.runtime


def _find_exposure_control():
    rt = _rt()
    try:
        ec = rt.SceneExposureControl.exposureControl
        if ec is not None and "vray" in str(rt.classOf(ec)).lower():
            return ec
    except Exception:
        pass
    return None


class ExposureHost:
    """Resolved once per apply/read; ``kind`` ∈ exposure_control | physical_cam | none."""

    def __init__(self, camera=None):
        self.ec = _find_exposure_control()
        self.cam = None
        self.kind = "none"
        if self.ec is not None and get_prop(self.ec, EC_EV) is not None:
            self.kind = "exposure_control"
        elif camera is not None:
            cname = ""
            try:
                cname = str(_rt().classOf(camera)).lower()
            except Exception:
                pass
            if "physical" in cname and get_prop(camera, CAM_ISO) is not None:
                self.cam = camera
                self.kind = "physical_cam"

    # ------------------------------------------------------------------ EV
    def read_ev(self) -> Optional[float]:
        if self.kind == "exposure_control":
            v = get_prop(self.ec, EC_EV)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        if self.kind == "physical_cam":
            try:
                iso = float(get_prop(self.cam, CAM_ISO, 100.0))
                n = float(get_prop(self.cam, CAM_FNUM, 8.0))
                shutter = float(get_prop(self.cam, CAM_SHUTTER, 200.0))  # speed = 1/t
                t = 1.0 / max(1e-6, shutter)
                return math.log2((n * n) / t) - math.log2(max(1e-6, iso) / 100.0)
            except Exception:
                return None
        return None

    def write_ev(self, ev: float) -> bool:
        if self.kind == "exposure_control":
            return set_prop(self.ec, EC_EV, float(ev)) is not None
        if self.kind == "physical_cam":
            current = self.read_ev()
            if current is None:
                return False
            try:
                iso = float(get_prop(self.cam, CAM_ISO, 100.0))
                new_iso = iso * (2.0 ** (current - float(ev)))   # lower EV target → more ISO
                new_iso = min(51200.0, max(6.0, new_iso))
                return set_prop(self.cam, CAM_ISO, new_iso) is not None
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------ WB
    def read_wb_kelvin(self) -> Optional[float]:
        host = self.ec if self.kind == "exposure_control" else self.cam
        if host is None:
            return None
        v = get_prop(host, EC_WB_KELVIN if self.kind == "exposure_control" else CAM_WB_KELVIN)
        try:
            k = float(v)
            if 1000.0 <= k <= 40000.0:
                return k
        except (TypeError, ValueError):
            pass
        return None

    def write_wb_kelvin(self, kelvin: float) -> bool:
        host = self.ec if self.kind == "exposure_control" else self.cam
        if host is None:
            return False
        kelvin_props = EC_WB_KELVIN if self.kind == "exposure_control" else CAM_WB_KELVIN
        mode_props = EC_WB_MODE if self.kind == "exposure_control" else CAM_WB_MODE
        if get_prop(host, kelvin_props) is not None:
            ok = set_prop(host, kelvin_props, float(kelvin)) is not None
            if ok:
                _nudge_wb_mode_to_temperature(host, mode_props)
            return ok
        # color-swatch-only host: write the illuminant color (same spinner convention)
        color_props = EC_WB_COLOR if self.kind == "exposure_control" else CAM_WB_COLOR
        r, g, b = wb_color_for_kelvin(kelvin)
        try:
            rt = _rt()
            ok = set_prop(host, color_props,
                          rt.color(r * 255.0, g * 255.0, b * 255.0)) is not None
            if ok:
                _nudge_wb_mode_to_custom(host, mode_props)
            return ok
        except Exception:
            return False

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ev": self.read_ev(), "wb_kelvin": self.read_wb_kelvin()}


def _nudge_wb_mode_to_temperature(host, mode_props) -> None:
    """Best-effort: hosts with a WB mode dropdown need 'temperature' selected for the kelvin
    spinner to take effect. Mode enum ints differ per host — VERIFY ON BOX; failure is silent
    and the checklist covers it."""
    v = get_prop(host, mode_props)
    if v is None:
        return
    try:
        for candidate in (1, 2):    # commonly: 0=custom/preset, 1 or 2 = temperature
            set_prop(host, mode_props, candidate)
            if get_prop(host, mode_props) == candidate:
                return
    except Exception:
        pass


def _nudge_wb_mode_to_custom(host, mode_props) -> None:
    v = get_prop(host, mode_props)
    if v is None:
        return
    try:
        set_prop(host, mode_props, 0)
    except Exception:
        pass
