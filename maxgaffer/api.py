"""MaxGaffer's public API — the LightMatch engine MaxDirector's SPEC deferred to P2.

Import inside 3ds Max (any tool, MaxDirector's pipeline, a listener one-liner):

    from maxgaffer.api import match_camera, apply_camera_state, render_cameras_vantage

    result = match_camera("PhysCam_Hero", r"D:/refs/dusk.jpg", log=print)
    # → {"score": 84.2, "stop_reason": "target_reached", "iterations": 4,
    #    "state": {...genome dict...}, "run_dir": "...", "renders": [...]}

Contract notes:
  * main-thread only (it drives pymxs) — callers own threading;
  * per-camera state/reference/locks persist in the scene's session sidecar exactly as if
    driven from the dock, so the UI and API stay interchangeable mid-project;
  * ``config_overrides`` tweaks the loaded config for THIS call only (nothing written).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .maxbridge import config as _config
from .maxbridge.controller import Controller

__all__ = ["match_camera", "match_all_cameras", "apply_camera_state",
           "render_cameras_vantage", "get_controller"]

_shared: Optional[Controller] = None


def get_controller(config_overrides: Optional[Dict] = None) -> Controller:
    """One shared Controller (session/rig caches stay warm across API calls)."""
    global _shared
    if _shared is None:
        _shared = Controller(_config.load())
    if config_overrides:
        for k, v in config_overrides.items():
            if hasattr(_shared.cfg, k):
                setattr(_shared.cfg, k, v)
    return _shared


def match_camera(
    camera_name: str,
    reference_path: str = "",
    log: Callable[[str], None] = lambda m: None,
    should_cancel: Callable[[], bool] = lambda: False,
    locks: Optional[set] = None,
    sweep: bool = True,
    config_overrides: Optional[Dict] = None,
) -> Dict:
    """Bind ``reference_path`` (if given) to ``camera_name`` and run the full match loop."""
    ctrl = get_controller(config_overrides)
    if reference_path:
        ctrl.session.set_reference(camera_name, reference_path)
        ctrl.save_session()
    result = ctrl.run_match(camera_name, log=log, should_cancel=should_cancel,
                            locks=locks, do_sweep=sweep)
    return {
        "score": result.best_score,
        "stop_reason": result.stop_reason,
        "iterations": len(result.iterations),
        "state": result.best_state.to_dict(),
        "run_dir": ctrl._run_dir,
        "renders": [r.render_path for r in result.iterations if r.render_path],
    }


def match_all_cameras(
    log: Callable[[str], None] = lambda m: None,
    should_cancel: Callable[[], bool] = lambda: False,
    sweep: bool = True,
    config_overrides: Optional[Dict] = None,
) -> Dict[str, str]:
    """Match every camera that has a reference bound (unattended queue)."""
    return get_controller(config_overrides).match_all(log, should_cancel, do_sweep=sweep)


def apply_camera_state(camera_name: str) -> List[str]:
    """Re-apply a camera's saved lighting state. Returns warnings."""
    ctrl = get_controller()
    e = ctrl.session.cameras.get(camera_name)
    if not (e and e.state is not None):
        raise RuntimeError(f"no saved lighting state for camera '{camera_name}'")
    return ctrl.apply_state(e.state, camera_name)


def render_cameras_vantage(
    camera_names: List[str],
    out_dir: str,
    on_progress: Callable[[str, str], None] = lambda c, s: None,
) -> Dict[str, str]:
    """Apply each camera's saved state, export per-camera vrscenes, run the vantage_console
    batch (BLOCKING — wrap in your own worker if you need Max responsive)."""
    ctrl = get_controller()
    jobs = ctrl.prepare_vantage_jobs(camera_names, out_dir, on_progress)
    return ctrl.run_vantage_jobs(jobs, on_progress)
