"""Settings + the oc_ key, stored at %LOCALAPPDATA%/MaxGaffer/config.json.

Stdlib-only (importable off-Max, same as MaxDirector's). If MaxGaffer has no key yet but a
MaxDirector install does, we borrow it silently — same gateway, same owner, one less paste.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict


def _appdata_dir(name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_PATH = os.path.join(_appdata_dir("MaxGaffer"), "config.json")


@dataclass
class Config:
    api_key: str = ""                        # oc_ gateway key
    model: str = "claude-opus-4-8"           # vision-capable — the loop shows it images
    vantage_console: str = r"C:\Program Files\Chaos\Vantage\vantage_console.exe"
    system_python: str = ""                  # optional Pillow-equipped python for the sidecar
    loop_width: int = 480                    # iteration-render size (speed over beauty)
    loop_height: int = 270
    final_width: int = 1920
    final_height: int = 1080
    max_iterations: int = 5
    target_score: float = 82.0
    sweep_count: int = 8
    keep_runs: int = 10                      # run folders kept per camera (0 = keep all)
    critic_weights: Dict[str, float] = field(default_factory=dict)   # override critic defaults
    repo_path: str = ""                      # clone folder, written by install.bat

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1)


def load() -> Config:
    cfg = Config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, v in d.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    except (OSError, ValueError):
        pass
    if not cfg.api_key:
        cfg.api_key = _borrow_maxdirector_key()
    return cfg


def _borrow_maxdirector_key() -> str:
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        with open(os.path.join(base, "MaxDirector", "config.json"), encoding="utf-8") as f:
            return str(json.load(f).get("api_key") or "")
    except (OSError, ValueError):
        return ""


def sessions_dir() -> str:
    d = os.path.join(_appdata_dir("MaxGaffer"), "sessions")
    os.makedirs(d, exist_ok=True)
    return d
