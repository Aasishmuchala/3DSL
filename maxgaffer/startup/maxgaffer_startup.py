"""Copied into Max's scripts/startup by install.bat — registers the MaxGaffer macro.

Reads the clone path from %LOCALAPPDATA%/MaxGaffer/config.json (written by the installer),
puts it on sys.path, and defines a macroscript so the action can live on any toolbar:
Customize → category "MaxGaffer".
"""

import json
import os
import sys


def _repo_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    try:
        with open(os.path.join(base, "MaxGaffer", "config.json"), encoding="utf-8") as f:
            return str(json.load(f).get("repo_path") or "")
    except (OSError, ValueError):
        return os.environ.get("MAXGAFFER", "")


def _register():
    repo = _repo_path()
    if repo and os.path.isdir(repo) and repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from pymxs import runtime as rt

        rt.execute(r'''
macroScript MaxGaffer category:"MaxGaffer" tooltip:"MaxGaffer — reference lighting match" (
    on execute do (
        python.Execute "import maxgaffer.bootstrap; maxgaffer.bootstrap.launch()"
    )
)
''')
    except Exception as e:  # noqa: BLE001
        print("[MaxGaffer] startup registration failed:", e)


_register()
