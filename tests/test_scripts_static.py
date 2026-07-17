"""Scripts that only ever EXECUTE on the box must still be VERIFIABLE off it.

Round-3 audit found onbox_spikes.py importing a symbol (CAM_SHUTTER) that a refactor had
split months of commits earlier — the resulting ImportError was swallowed by the
inside-Max guard and printed "must run INSIDE 3ds Max" ON the box, killing the one-command
P0 bring-up with a lie. This suite makes that class of break impossible to ship again:
every script must compile, and every `from maxgaffer... import X` must name a real X
(bridge modules import cleanly off-Max — pymxs is always lazy behind _rt()).
"""

import ast
import importlib
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ("scripts/onbox_spikes.py", "scripts/preflight.py",
           "scripts/live_gateway_smoke.py", "scripts/sim_match.py")


@pytest.mark.parametrize("rel", SCRIPTS)
def test_script_compiles_and_maxgaffer_imports_resolve(rel):
    path = os.path.join(REPO, rel)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    compile(src, rel, "exec")                       # syntax floor
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("maxgaffer") and not node.level:
            if ".ui" in node.module:                # dock needs PySide6 — scripts don't use it
                continue
            mod = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(mod, alias.name), (
                    f"{rel}: `from {node.module} import {alias.name}` — "
                    f"'{alias.name}' does not exist (stale after a refactor?)")
                checked += 1
    assert checked > 0, f"{rel}: no maxgaffer imports found — did the script move?"


def test_onbox_guard_only_wraps_pymxs():
    """The inside-Max message must be reachable ONLY from `import pymxs` failing — any
    other ImportError has to traceback (that's the round-3 lesson)."""
    with open(os.path.join(REPO, "scripts/onbox_spikes.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
              and any(isinstance(h.type, ast.Name) and h.type.id == "ImportError"
                      for h in n.handlers if h.type is not None)]
    assert guards, "the pymxs guard is gone"
    for g in guards:
        body_calls = {n.id for stmt in g.body for n in ast.walk(stmt)
                      if isinstance(n, ast.Name)}
        assert "main" not in body_calls, \
            "main() is inside the ImportError guard again — a stale import would " \
            "masquerade as 'not inside Max'"


def test_draft_restore_survives_renderer_swap(tmp_path, monkeypatch):
    """Crash with draft applied → renderer swapped → relaunch: a vanished prop
    (current=None) made type(None)(value) raise, stranding the remaining restores AND
    the snapshot file. Restore must degrade per-prop and always clear the snapshot."""
    from maxgaffer.maxbridge import draft as df

    snap = tmp_path / "draft_snapshot.json"
    snap.write_text(json.dumps({"gone_prop": 4.0, "also_gone": 24}))
    monkeypatch.setattr(df, "SNAPSHOT_PATH", str(snap))
    monkeypatch.setattr(df, "_renderer", lambda: object())
    monkeypatch.setattr(df, "get_prop", lambda obj, names, default=None: None)
    monkeypatch.setattr(df, "set_prop", lambda obj, names, v: None)
    lines = df.restore_draft()
    assert sum("could not restore" in ln for ln in lines) == 2   # both reported, none fatal
    assert not snap.exists(), "snapshot must clear even when props vanished"
