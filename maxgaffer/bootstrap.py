"""Launch entry point — the startup macro calls ``maxgaffer.bootstrap.launch()``.

MaxGaffer's hard floor is stdlib-only (the Omega client is urllib, loop-render stats decode
via the stdlib PNG reader), so unlike MaxDirector there are NO required pip packages.
Pillow in Max's user-site is optional and self-detected: it upgrades reference ingestion
(JPEG stats without transcode) and slims LLM payloads.
"""

from __future__ import annotations

import os
import sys

OPTIONAL = ("PIL",)


def _ensure_usersite_on_path() -> None:
    candidates = []
    try:
        import site

        candidates.append(site.getusersitepackages())
    except Exception:
        pass
    for env_var in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, "Python", "Python311", "site-packages"))
    for path in candidates:
        for cand in (path, os.path.realpath(path) if path else None):
            if cand and cand not in sys.path and os.path.isdir(cand):
                sys.path.insert(0, cand)


def launch():
    _ensure_usersite_on_path()
    try:
        from .ui.dock import show_dock

        return show_dock()
    except Exception as e:  # noqa: BLE001 surface a readable message inside Max
        msg = f"MaxGaffer failed to launch:\n{type(e).__name__}: {e}"
        try:
            from pymxs import runtime as rt  # type: ignore

            rt.messageBox(msg, title="MaxGaffer")
        except Exception:
            print("[MaxGaffer] " + msg)
        return None
