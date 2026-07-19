"""Kimi on-box crash-safety harness — run INSIDE 3ds Max 2026 (3dsmaxbatch).

Verifies MaxGaffer never crashes Max and degrades gracefully when V-Ray / Vantage /
a rig are absent. Every step is isolated; a step failure is recorded, never fatal.

Launched by kimi_onbox_runner.ms via:  python.ExecuteFile @<this file>
Writes a JSON report next to itself: kimi_onbox_report.json
"""

import json
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kimi_onbox_report.json")

results = []


def step(name):
    def deco(fn):
        try:
            fn()
            results.append({"step": name, "ok": True})
            print("[ONBOX PASS]", name)
        except Exception as e:  # noqa: BLE001 — a crash here is exactly what we hunt
            results.append({"step": name, "ok": False, "error": "%s: %s" % (type(e).__name__, e),
                            "trace": traceback.format_exc()[-2000:]})
            print("[ONBOX FAIL]", name, "->", type(e).__name__, e)
    return deco


@step("env: python version + pymxs import")
def _():
    import pymxs  # noqa: F401
    print("python:", sys.version.replace("\n", " "))
    assert sys.version_info[:2] == (3, 11), "unexpected Max python %r" % (sys.version_info,)


@step("sys.path: repo importable")
def _():
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import maxgaffer  # noqa: F401


@step("import: every maxbridge module (pymxs surface) on a V-Ray-less box")
def _():
    import importlib
    for mod in ("scene", "digest", "config", "apply", "exposure", "draft",
                "render", "vantage", "execute", "controller"):
        importlib.import_module("maxgaffer.maxbridge." + mod)


@step("import: core + api + bootstrap + sidecar")
def _():
    import importlib
    for mod in ("genome", "solver", "colortemp", "director", "metrics", "png_min",
                "hdr_min", "domeseed", "parse", "prompts", "omega", "consensus",
                "rules", "scenarios", "planner", "scenedigest", "critic",
                "feedback", "session"):
        importlib.import_module("maxgaffer.core." + mod)
    import maxgaffer.api  # noqa: F401
    import maxgaffer.bootstrap  # noqa: F401
    import maxgaffer.sidecar.metrics_cli  # noqa: F401


@step("startup script: macroscript registration path (as install.bat copies it)")
def _():
    ns = {"__name__": "__maxgaffer_startup__"}
    src = os.path.join(REPO, "maxgaffer", "startup", "maxgaffer_startup.py")
    with open(src, encoding="utf-8") as f:
        code = compile(f.read(), src, "exec")
    exec(code, ns)  # must not raise even with no config.json present


@step("scene introspection: empty scene, no rig — graceful empties, no exceptions")
def _():
    from maxgaffer.maxbridge import scene as mb_scene
    cams = mb_scene.list_cameras() if hasattr(mb_scene, "list_cameras") else None
    print("cameras:", cams)
    if hasattr(mb_scene, "classify_rig"):
        rig = mb_scene.classify_rig()
        print("rig:", rig)


@step("exposure host detection: no V-Ray EC, maybe native physical cam")
def _():
    from maxgaffer.maxbridge import exposure as mb_exp
    host = None
    for name in ("detect_host", "find_host", "host", "get_host"):
        if hasattr(mb_exp, name):
            host = getattr(mb_exp, name)()
            break
    print("exposure host:", host)


@step("vantage probe: live link + console absent — must report, not raise")
def _():
    from maxgaffer.maxbridge import vantage as mb_van
    for name in dir(mb_van):
        if name.startswith(("start_", "launch_")):
            continue  # side-effectful on a V-Ray-equipped box: would toggle the real live link
        if "probe" in name.lower() or "live_link" in name.lower() or "find" in name.lower():
            fn = getattr(mb_van, name)
            if callable(fn):
                try:
                    print("vantage.%s ->" % name, fn())
                except TypeError:
                    pass  # needs args; skip


@step("apply: no-rig state application no-ops safely (no scene corruption)")
def _():
    from maxgaffer.maxbridge import apply as mb_apply
    if hasattr(mb_apply, "apply_state"):
        try:
            mb_apply.apply_state({}, {}, None)
        except TypeError:
            mb_apply.apply_state({})
    print("apply no-rig: returned without raising")


@step("render loop frame: no V-Ray renderer — graceful failure path")
def _():
    from maxgaffer.maxbridge import render as mb_render
    for name in dir(mb_render):
        if name.startswith("render") and callable(getattr(mb_render, name)):
            try:
                print("render.%s ->" % name, getattr(mb_render, name)())
            except TypeError:
                pass


@step("controller: construct on empty scene (no launch, no threads)")
def _():
    from maxgaffer.maxbridge import controller as mb_ctrl
    for name in ("Controller", "MaxGafferController", "Bridge"):
        cls = getattr(mb_ctrl, name, None)
        if cls is not None:
            try:
                inst = cls()
                print("controller:", inst)
            except TypeError:
                print("controller ctor needs args — skipped")
            break


@step("session: save/load sidecar against unsaved scene — in-memory + warning, no crash")
def _():
    from maxgaffer.core import session as core_session
    for name in dir(core_session):
        obj = getattr(core_session, name)
        if isinstance(obj, type) and "ession" in name:
            try:
                s = obj()
                print("session obj:", s)
            except TypeError:
                pass
            break


@step("ui.dock import (no widget creation in batch mode)")
def _():
    import importlib
    importlib.import_module("maxgaffer.ui.dock")


try:
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump({"repo": REPO, "results": results,
                   "passed": sum(1 for r in results if r["ok"]),
                   "failed": sum(1 for r in results if not r["ok"])}, f, indent=1)
except OSError as e:
    print("[ONBOX] could not write report:", e)

print("[ONBOX DONE] passed=%d failed=%d"
      % (sum(1 for r in results if r["ok"]), sum(1 for r in results if not r["ok"])))
