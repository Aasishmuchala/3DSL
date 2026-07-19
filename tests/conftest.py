"""Session-wide harness guards for the MaxGaffer suite.

Two jobs, both root causes of real failures under Max's bundled Python 3.11:

1. Qt platform selection. Max 2026's Qt ships ONLY ``qwindows.dll`` (in
   ``<maxroot>/qt/plugins/platforms``) — there is no ``qoffscreen.dll`` and no
   ``qminimal.dll``. Requesting ``QT_QPA_PLATFORM=offscreen`` there makes
   ``QApplication()`` wedge in native code (Qt's missing-plugin error path never
   returns in a non-interactive session) — a forever hang, P0 class. Test
   modules historically did ``os.environ.setdefault("QT_QPA_PLATFORM",
   "offscreen")`` at import time; this conftest runs first and pins the
   variable to a platform whose plugin actually exists (headless preferred,
   ``windows`` as the always-present fallback inside Max). Off-Max (managed
   Python, no PySide6) nothing is touched and the Qt tests skip as before.

2. sys.modules hygiene. Under real PySide6, shiboken replaces
   ``builtins.__import__`` with its ``__feature_import__`` hook, which reads
   ``module.__name__`` on EVERY import result. A ``types.SimpleNamespace``
   "pymxs" stub (no ``__name__``) then makes any lazy ``import pymxs`` in
   source raise ``AttributeError`` — the order-dependent failures seen only in
   full runs (collection imports PySide6; single-file runs never do). Stubs
   must be real ``types.ModuleType`` objects; this autouse fixture removes any
   non-module stub that still leaks past a test's teardown.
"""

import os
import sys
import types

import pytest


def _available_qt_platforms():
    """Names of Qt platform plugins ('windows', 'offscreen', ...) this Qt can load."""
    try:
        from PySide6 import QtCore  # noqa: PLC0415 — probing only when Qt exists
    except Exception:
        return set()
    dirs = []
    try:
        from PySide6.QtCore import QLibraryInfo

        dirs.append(os.path.join(
            QLibraryInfo.path(QLibraryInfo.PluginsPath), "platforms"))
    except Exception:
        pass
    dirs.append(os.path.join(os.path.dirname(QtCore.__file__), "plugins", "platforms"))
    if sys.platform == "win32":
        try:  # where Qt6Core.dll was ACTUALLY loaded from (Autodesk layout + qt.conf)
            import ctypes
            from ctypes import wintypes

            k = ctypes.windll.kernel32
            k.GetModuleHandleW.restype = wintypes.HMODULE
            k.GetModuleFileNameW.restype = wintypes.DWORD
            k.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR,
                                             wintypes.DWORD]
            handle = k.GetModuleHandleW("Qt6Core.dll")
            buf = ctypes.create_unicode_buffer(1024)
            if handle and k.GetModuleFileNameW(handle, buf, 1024):
                qt_dir = os.path.dirname(buf.value)
                dirs.append(os.path.join(qt_dir, "qt", "plugins", "platforms"))
                dirs.append(os.path.join(qt_dir, "plugins", "platforms"))
        except Exception:
            pass
    found = set()
    for d in dirs:
        if os.path.isdir(d):
            for f in os.listdir(d):
                stem, ext = os.path.splitext(f)
                if ext.lower() == ".dll" and stem.lower().startswith("q"):
                    found.add(stem[1:].lower())          # qwindows -> windows
    return found


def _select_qt_platform():
    """Pin QT_QPA_PLATFORM to a platform whose plugin exists — BEFORE any test
    module's setdefault can choose a missing one and hang QApplication()."""
    available = _available_qt_platforms()
    if not available:
        return                                   # no PySide6 here: Qt tests will skip
    requested = os.environ.get("QT_QPA_PLATFORM", "")
    name = requested.split(";")[0].split(":")[0].strip().lower()
    if name and name in available:
        return                                   # requested platform exists — honor it
    for cand in ("offscreen", "minimal", "windows"):   # headless first, native last
        if cand in available:
            os.environ["QT_QPA_PLATFORM"] = cand
            return
    os.environ.pop("QT_QPA_PLATFORM", None)      # nothing matched — Qt default wins


_select_qt_platform()


def _tolerate_nonmodule_import_results():
    """Make shiboken's import hook tolerate sys.modules entries that are NOT real
    modules (e.g. a types.SimpleNamespace "pymxs" stub installed by a test).

    Under Max's real PySide6, builtins.__import__ is replaced by shiboken's
    __feature_import__, which calls feature.feature_imported(module) on EVERY
    import result — including `import pymxs` served straight from sys.modules.
    feature_imported unconditionally reads ``module.__name__`` (feature.py:135),
    so a SimpleNamespace stub raises AttributeError out of any lazy
    ``import pymxs`` in source — but only in full runs (collection imports
    PySide6; single-file runs never install the hook), which is what made these
    failures order-dependent. The hook trampoline in loader.py re-reads
    ``feature.feature_imported`` on every call, so rebinding it here is enough;
    real modules always carry a str __name__, so production imports are
    untouched. This hardens the harness for stub styles in files this wave may
    not edit (test_kimi_render_vantage.py)."""
    try:
        import PySide6  # noqa: F401, PLC0415 — bootstraps the embedded shibokensupport
        from shibokensupport import feature  # noqa: PLC0415
    except Exception:
        return                                     # no PySide6 here — hook never installs
    original = feature.feature_imported

    def _tolerant(module, *args, **kwargs):
        if not isinstance(getattr(module, "__name__", None), str):
            return None                            # test stub, not a module: nothing to classify
        return original(module, *args, **kwargs)

    feature.feature_imported = _tolerant


_tolerate_nonmodule_import_results()


_STUB_NAMES = ("pymxs", "qtmax")


@pytest.fixture(autouse=True)
def _no_nonmodule_sysmodules_leaks():
    """Drop sys.modules entries that are not real modules after every test, so a
    SimpleNamespace-style stub can never poison `import pymxs` in a later file
    (shiboken's import hook dereferences __name__ on whatever the import returns)."""
    yield
    for name in _STUB_NAMES:
        mod = sys.modules.get(name)
        if mod is not None and not isinstance(mod, types.ModuleType):
            del sys.modules[name]
