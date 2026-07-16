"""Chaos Vantage — live link (the user's real-time monitor) + final-frame batch renders.

Live link: every apply the match loop makes syncs to Vantage instantly once the link is up,
so the user literally watches the sun swing into place at full speed. Starting the link has
no single documented MAXScript entry point across V-Ray builds, so we probe known globals
first and then scan actionMan for the V-Ray menu action ("Chaos Vantage live link…") and
execute it — with a clear "click the menu once" fallback message if neither lands.

Batch: MaxDirector's verified CLI shape — export a .vrscene per camera (that camera active),
then run vantage_console sequentially: -scenefile -outputFile -outputWidth -outputHeight
-frames. Per-camera lighting states are applied before each export, which is what turns the
camera list into a one-click "render every shot with its own matched light" board.
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
    """Try to start the Vantage live link. → (started?, how/diagnostic)."""
    rt = _rt()
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


def render_stills(
    jobs: List[Dict],                      # {camera, scene_file, output}
    console_exe: str,
    width: int,
    height: int,
    on_progress: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, str]:
    """Sequential vantage_console batch ("finish all sequences one by one"). Halts on the
    first hard failure; earlier outputs stay on disk."""
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
