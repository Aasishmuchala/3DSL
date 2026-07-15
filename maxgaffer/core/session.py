"""Per-camera lighting sessions — every camera can carry its own reference + matched rig.

Archviz reality (the TULA shot-book workflow): each shot wants its own sun. So MaxGaffer's
unit of work is (camera, reference, LightingState, score), persisted in a sidecar JSON next
to the .max file — human-readable, diff-able, survives Max crashes, never bloats the scene.

The bridge owns *when* to apply a camera's state (on select / on render); this owns the data.
Timestamps are injected so tests stay deterministic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .genome import LightingState

FORMAT_VERSION = 1


def sidecar_path(scene_path: str) -> Optional[str]:
    """foo.max → foo.maxgaffer.json (None for an unsaved scene)."""
    if not scene_path:
        return None
    root, _ = os.path.splitext(scene_path)
    return root + ".maxgaffer.json" if root else None


@dataclass
class CameraEntry:
    reference: str = ""                       # reference image path ("" = none bound)
    state: Optional[LightingState] = None     # last accepted rig for this camera
    score: Optional[float] = None
    matched_at: str = ""
    locks: Set[str] = field(default_factory=set)
    semantics: Dict = field(default_factory=dict)   # cached ANALYZE result for the reference

    def to_dict(self) -> Dict:
        return {
            "reference": self.reference,
            "state": self.state.to_dict() if self.state else None,
            "score": self.score,
            "matched_at": self.matched_at,
            "locks": sorted(self.locks),
            "semantics": self.semantics,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "CameraEntry":
        state = d.get("state")
        return cls(
            reference=str(d.get("reference") or ""),
            state=LightingState.from_dict(state) if isinstance(state, dict) else None,
            score=d.get("score") if isinstance(d.get("score"), (int, float)) else None,
            matched_at=str(d.get("matched_at") or ""),
            locks=set(x for x in (d.get("locks") or []) if isinstance(x, str)),
            semantics=d.get("semantics") if isinstance(d.get("semantics"), dict) else {},
        )


class Session:
    def __init__(self, path: Optional[str] = None, now_fn: Callable[[], str] = None):
        self.path = path
        self.cameras: Dict[str, CameraEntry] = {}
        self.settings: Dict = {"apply_on_select": True}
        self._now = now_fn or _iso_now

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Optional[str], now_fn: Callable[[], str] = None) -> "Session":
        s = cls(path, now_fn)
        if not path or not os.path.exists(path):
            return s
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return s
        for name, entry in (d.get("cameras") or {}).items():
            if isinstance(entry, dict):
                s.cameras[str(name)] = CameraEntry.from_dict(entry)
        if isinstance(d.get("settings"), dict):
            s.settings.update(d["settings"])
        return s

    def save(self) -> bool:
        if not self.path:
            return False
        payload = {
            "version": FORMAT_VERSION,
            "cameras": {n: e.to_dict() for n, e in self.cameras.items()},
            "settings": self.settings,
        }
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ camera API
    def entry(self, camera: str) -> CameraEntry:
        if camera not in self.cameras:
            self.cameras[camera] = CameraEntry()
        return self.cameras[camera]

    def set_reference(self, camera: str, ref_path: str) -> None:
        e = self.entry(camera)
        if e.reference != ref_path:
            e.reference = ref_path
            e.semantics = {}          # a new reference invalidates the cached analysis
            e.score = None

    def record_match(self, camera: str, state: LightingState,
                     score: Optional[float]) -> None:
        e = self.entry(camera)
        e.state = state.copy()
        e.score = score
        e.matched_at = self._now()

    def cameras_with_states(self) -> List[str]:
        return [n for n, e in self.cameras.items() if e.state is not None]


def _iso_now() -> str:
    import datetime

    return datetime.datetime.now().replace(microsecond=0).isoformat()
