"""Chaos Vantage — live link (the user's real-time monitor) + scene handoff.

VERIFIED against Chaos docs/forums 2026-07-16, for Vantage 3.x:
  * LIVE LINK — installed with V-Ray (5.1+ incl. 7) as the 3ds Max toolbar action
    "Initiate a Live-Link to Chaos Vantage": it STARTS Vantage if needed, streams on port
    20701 (20703 for V-Ray 7.3 DR2), and the SAME action TOGGLES the link off. We probe
    legacy maxscript globals, then scan actionMan for that action text.
  * COMMAND-LINE RENDERING WAS REMOVED in Vantage 2.0+ (Chaos support: "The command line
    control has been removed"; it returned only in the paid Developer Edition). On stock
    Vantage 3.3 there is NO -sceneFile/-outputFile headless render. Therefore:
      - the DEFAULT final-render backend is V-Ray inside Max (fully scriptable);
      - per-camera .vrscene exports remain first-class — drop them into Vantage's in-app
        Batch Render queue (or double-click one) for Vantage-quality finals;
      - the CLI runner below is kept ONLY for Developer-Edition/legacy consoles and is
        gated behind config.final_render_backend == "vantage_cli".
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from . import scene as sc

LIVE_LINK_GLOBALS = (
    "vantageStartLiveLink()",
    "startVantageLiveLink()",
    "vrayStartVantageLiveLink()",
    "vray_startVantageLiveLink()",
)


def _rt():
    import pymxs

    return pymxs.runtime


def start_live_link() -> Tuple[bool, str]:
    """Execute V-Ray's 'Initiate a Live-Link to Chaos Vantage' action (a TOGGLE — it also
    stops an active link). → (executed?, how/diagnostic). Degrades off-Max."""
    try:
        rt = _rt()
    except Exception:
        return False, "pymxs unavailable (not running inside 3ds Max)"
    for expr in LIVE_LINK_GLOBALS:
        try:
            rt.execute(expr)
            return True, f"maxscript global {expr}"
        except Exception:
            continue
    hit = _find_live_link_action()
    if hit is not None:
        action, label = hit
        try:
            action.execute()
            return True, f"actionMan: {label}"
        except Exception as e:  # noqa: BLE001
            return False, f"found action '{label}' but execute failed: {e}"
    return False, ("no live-link entry point found — start it once via the V-Ray menu "
                   "(Chaos Vantage live link); the link then mirrors everything MaxGaffer does")


def _find_live_link_action():
    """Scan actionMan for an action whose text mentions Vantage. Returns (action, label)."""
    rt = _rt()
    try:
        num_tables = int(rt.actionMan.numActionTables)
    except Exception:
        return None
    for t in range(1, num_tables + 1):
        try:
            table = rt.actionMan.getActionTable(t)
            for a in range(1, int(table.numActions) + 1):
                action = table.getAction(a)
                label = ""
                for getter in ("getDescriptionText", "getButtonText", "getMenuText"):
                    try:
                        label = str(getattr(action, getter)())
                        if label:
                            break
                    except Exception:
                        continue
                low = label.lower()
                if "vantage" in low and ("live" in low or "link" in low):
                    return action, label
        except Exception:
            continue
    return None


def export_vrscene(path: str, camera_name: Optional[str] = None) -> Optional[str]:
    """Export the current scene (single frame, active camera) as .vrscene."""
    rt = _rt()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if camera_name and not sc.set_active_camera(camera_name):
        return None
    if not hasattr(rt, "vrayExportVRScene"):
        return None
    try:
        frame = int(rt.currentTime.frame) if hasattr(rt.currentTime, "frame") else 0
    except Exception:
        frame = 0
    try:  # rich signature first (compressed keeps multi-GB interiors manageable)
        rt.vrayExportVRScene(path, exportCompressed=True, startFrame=frame, endFrame=frame)
    except Exception:
        try:
            rt.vrayExportVRScene(path)
        except Exception:
            return None
    return path if os.path.exists(path) else None


def _output_written(output: str) -> bool:
    """Vantage may write frame-suffixed files (out.0000.png) instead of the exact name —
    accept any non-empty file sharing the stem in the same folder."""
    if os.path.exists(output) and os.path.getsize(output) > 0:
        return True
    folder = os.path.dirname(output) or "."
    stem = os.path.splitext(os.path.basename(output))[0]
    try:
        return any(f.startswith(stem) and os.path.getsize(os.path.join(folder, f)) > 0
                   for f in os.listdir(folder))
    except OSError:
        return False


def vantage_command(console_exe: str, scene_file: str, output: str,
                    width: int, height: int, frame: int = 0) -> List[str]:
    return [
        console_exe,
        f"-scenefile={scene_file}",
        f"-outputFile={output}",
        f"-outputWidth={int(width)}",
        f"-outputHeight={int(height)}",
        f"-frames={int(frame)}-{int(frame)}",
    ]


def launch_vantage(vantage_exe: str, scene_file: Optional[str] = None) -> bool:
    """Open Vantage (optionally on a vrscene) — the handoff for the in-app batch queue."""
    if not os.path.exists(vantage_exe):
        return False
    try:
        args = [vantage_exe] + ([scene_file] if scene_file else [])
        subprocess.Popen(args, close_fds=True)
        return True
    except Exception:
        return False


def render_stills(
    jobs: List[Dict],                      # {camera, scene_file, output}
    console_exe: str,
    width: int,
    height: int,
    on_progress: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, str]:
    """LEGACY/Developer-Edition ONLY: sequential vantage_console CLI batch. Stock Vantage
    2.0+ removed these flags — use the V-Ray backend or the in-app batch queue instead."""
    results: Dict[str, str] = {}
    if not os.path.exists(console_exe):
        return {j["camera"]: f"vantage_console not found: {console_exe}" for j in jobs}
    for job in jobs:
        cam = job["camera"]
        if on_progress:
            on_progress(cam, "rendering (vantage)")
        cmd = vantage_command(console_exe, job["scene_file"], job["output"], width, height)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
            if proc.returncode == 0 and _output_written(job["output"]):
                results[cam] = "ok"
                if on_progress:
                    on_progress(cam, "done")
            else:
                results[cam] = f"vantage exit {proc.returncode}"
                if on_progress:
                    on_progress(cam, results[cam])
                break
        except Exception as e:  # noqa: BLE001
            results[cam] = f"error: {e}"
            if on_progress:
                on_progress(cam, results[cam])
            break
    return results
