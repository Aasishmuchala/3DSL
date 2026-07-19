"""Round-I audit regressions — apply/exposure/draft, driven off-Max with stubs.

Covers: 0-poisoning guards in capture_baselines + baseline read/apply symmetry,
fault-isolated float() conversions inside the undo record, ensure_exposure_control
never clobbering a native (non-V-Ray) exposure control + undo-wrapped creation,
draft snapshot-first ordering + honest logging, and restore_draft string/garbage
tolerance. pymxs and the scene bridge are stubbed the same way test_scripts_static
and test_ui_dock do it — everything here is pure python.
"""

import builtins
import json
import sys
import types

import pytest

from maxgaffer.core.genome import LightingState
from maxgaffer.core.session import Session
from maxgaffer.maxbridge import apply as ap
from maxgaffer.maxbridge import draft as df
from maxgaffer.maxbridge import exposure as exp


# --------------------------------------------------------------------- fakes
class FakeLight:
    def __init__(self, name, multiplier):
        self.name = name
        self.multiplier = multiplier


class FakeHost:
    """ExposureHost stand-in: no scene, nothing to read, every write accepted."""
    kind = "none"

    def __init__(self, camera=None):
        pass

    def read_ev(self):
        return None

    def read_wb_kelvin(self):
        return None

    def write_ev(self, ev):
        return True

    def write_wb_kelvin(self, k):
        return True


class FakeRt:
    """pymxs.runtime stand-in with a SceneExposureControl slot and a class registry."""
    def __init__(self):
        self.SceneExposureControl = types.SimpleNamespace(exposureControl=None)
        self.classes = {}
        self.created = 0

    def classOf(self, obj):
        return self.classes.get(id(obj), "Unknown")

    def vrayCreateVRayExposureControl(self):
        self.created += 1
        ec = object()
        self.classes[id(ec)] = "VRayExposureControl"
        return ec


class FakeUndo:
    calls = []

    def __init__(self, *args):
        FakeUndo.calls.append(args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mult_get(obj, names, default=None):
    return getattr(obj, "multiplier", default)


# --------------------------------------------------------------------- apply.py
def test_capture_baselines_skips_zero_with_note(monkeypatch):
    """A light renamed while dimmed to 0 must NOT be captured as a 0.0 baseline —
    that is exactly how the 0-poisoning failure resurrects (SPEC: adopt-once kills it)."""
    rig = {"groups": {"practicals": [FakeLight("Key_A", 0.0), FakeLight("Spot_B", 30.0)]}}
    monkeypatch.setattr(ap.sc, "get_prop", _mult_get)
    fresh = ap.capture_baselines(rig)
    assert dict(fresh) == {"Spot_B": 30.0}
    assert any("Key_A" in n and "forget_baseline" in n for n in fresh.notes)


def test_capture_result_feeds_adopt_baselines_unchanged(monkeypatch):
    """Return-contract compatibility: the capture feeds Session.adopt_baselines like a
    plain dict (controller.py and onbox_spikes.py consume it that way) — and 0-poison
    can no longer reach the session through the capture path."""
    rig = {"groups": {"g": [FakeLight("Key_A", 0.0), FakeLight("Spot_B", 30.0)]}}
    monkeypatch.setattr(ap.sc, "get_prop", _mult_get)
    fresh = ap.capture_baselines(rig)
    assert isinstance(fresh, dict)
    session = Session()
    assert session.adopt_baselines(fresh) == ["Spot_B"]
    assert session.baselines == {"Spot_B": 30.0}


def test_zero_baseline_reads_and_applies_as_authored_one(monkeypatch):
    """A 0.0 baseline already in the session (legacy poison / hand edit) must behave
    IDENTICALLY on the read and apply paths: treated as authored 1.0 in both, never a
    divide-by-0 ghost factor and never a 0-write."""
    light = FakeLight("Key_A", 10.0)
    rig = {"groups": {"practicals": [light]}}
    writes = []
    monkeypatch.setattr(ap.sc, "get_prop", _mult_get)

    def fake_set(obj, names, value):
        writes.append((obj.name, names[0], value))
        obj.multiplier = value
        return names[0]

    monkeypatch.setattr(ap.sc, "set_prop", fake_set)
    monkeypatch.setattr(ap, "ExposureHost", FakeHost)
    st = ap.read_state(rig, {"Key_A": 0.0})
    assert st.groups["practicals"] == pytest.approx(10.0)     # 10.0/1.0, not /0 → 1.0
    st2 = LightingState()
    st2.groups["practicals"] = 0.5
    warnings = []
    ap._apply_inner(rig, {"Key_A": 0.0}, st2, None, warnings)
    assert writes == [("Key_A", "multiplier", 0.5)]           # 1.0×0.5, never 0.0×0.5


def test_apply_inner_survives_non_numeric_state_values(monkeypatch):
    """Raw junk written into the public state dicts must downgrade to warnings, not
    detonate a float() inside the undo record — valid params in the same apply survive."""
    sun, light = FakeLight("Sun", 1.0), FakeLight("Spot_A", 30.0)
    rig = {"sun": sun, "groups": {"practicals": [light]}}
    writes = []
    monkeypatch.setattr(ap.sc, "get_prop", _mult_get)
    monkeypatch.setattr(ap.sc, "set_prop",
                        lambda obj, names, v: writes.append((obj.name, names[0], v))
                        or names[0])
    monkeypatch.setattr(ap.sc, "read_sun_angles", lambda s: (100.0, 30.0, None))
    monkeypatch.setattr(ap.sc, "write_sun_angles", lambda s, az, alt: True)
    monkeypatch.setattr(ap, "ExposureHost", FakeHost)
    st = LightingState()
    st.set("sun.turbidity", 4.0)
    st.values["sun.intensity"] = "junk"          # raw write past the clamps
    st.values["sun.enabled"] = "on?"             # raw junk (float() used to raise)
    st.groups["practicals"] = "loud"             # raw junk factor
    warnings = []
    ap._apply_inner(rig, {"Spot_A": 30.0}, st, None, warnings)
    assert writes == [("Sun", "turbidity", 4.0)]  # the one valid write still landed
    assert any("sun.intensity" in w and "non-numeric" in w for w in warnings)
    assert any("sun.enabled" in w and "non-numeric" in w for w in warnings)
    assert any("group.practicals" in w and "non-numeric" in w for w in warnings)


# --------------------------------------------------------------------- exposure.py
def test_ensure_exposure_control_never_clobbers_native_ec(monkeypatch):
    """A native Photographic/Logarithmic EC is the artist's own exposure setup —
    ensure_exposure_control must leave it assigned, create nothing, and say the truth."""
    rt = FakeRt()
    native_ec = object()
    rt.classes[id(native_ec)] = "PhotographicExposureControl"
    rt.SceneExposureControl.exposureControl = native_ec
    monkeypatch.setattr(exp, "_rt", lambda: rt)
    msg = exp.ensure_exposure_control()
    assert msg is not None and "non-V-Ray exposure control" in msg
    assert "PhotographicExposureControl" in msg
    assert rt.created == 0
    assert rt.SceneExposureControl.exposureControl is native_ec      # untouched


def test_ensure_exposure_control_returns_none_when_vray_exists(monkeypatch):
    rt = FakeRt()
    rt.SceneExposureControl.exposureControl = rt.vrayCreateVRayExposureControl()
    monkeypatch.setattr(exp, "_rt", lambda: rt)
    assert exp.ensure_exposure_control() is None
    assert rt.created == 1                                           # no new EC


def test_ensure_exposure_control_creates_only_when_slot_empty_undo_wrapped(monkeypatch):
    """Auto-create fires only on a truly EMPTY slot, inside its own undo record (the
    call site sits outside apply_state's record), with an honest log line."""
    rt = FakeRt()
    FakeUndo.calls = []
    fake_pymxs = types.ModuleType("pymxs")     # real module object — shiboken's
    fake_pymxs.undo = lambda *a, **k: FakeUndo(*a)   # __import__ hook requires
    fake_pymxs.runtime = rt                          # __name__ on sys.modules entries
    monkeypatch.setitem(sys.modules, "pymxs", fake_pymxs)
    monkeypatch.setattr(exp, "_rt", lambda: rt)
    try:
        msg = exp.ensure_exposure_control()
    finally:
        sys.modules.pop("pymxs", None)
    assert msg is not None and "slot was empty" in msg
    assert rt.created == 1
    assert rt.SceneExposureControl.exposureControl is not None       # created + assigned
    assert FakeUndo.calls == [(True, "MaxGaffer exposure control")]  # undo-wrapped


def test_exposure_host_falls_through_past_native_ec(monkeypatch):
    """Host chain with a non-V-Ray EC in the slot: the EC is NOT a host — resolution
    falls through to a native Physical camera (direct Target-EV), else to none."""
    rt = FakeRt()
    native_ec = object()
    rt.classes[id(native_ec)] = "LogarithmicExposureControl"
    rt.SceneExposureControl.exposureControl = native_ec
    cam, plain_cam = object(), object()
    rt.classes[id(cam)] = "Physical"
    rt.classes[id(plain_cam)] = "TargetCamera"
    cam_props = {("exposure_value",): 11.0}                          # Target-EV prop
    monkeypatch.setattr(exp, "_rt", lambda: rt)
    monkeypatch.setattr(exp, "get_prop",
                        lambda obj, names, default=None:
                        cam_props.get((names[0],), default) if obj is cam else default)
    host = exp.ExposureHost(cam)
    assert host.kind == "physical_cam"                               # not the native EC
    assert host.read_ev() == pytest.approx(11.0)
    assert exp.ExposureHost(plain_cam).kind == "none"                # → none (auto-lock)


# --------------------------------------------------------------------- draft.py
def _draft_stub(tmp_path, monkeypatch, props):
    """Stub renderer + props; returns (set_calls list, snapshot path)."""
    snap = tmp_path / "draft_snapshot.json"
    calls = []
    monkeypatch.setattr(df, "SNAPSHOT_PATH", str(snap))
    monkeypatch.setattr(df, "_renderer", lambda: object())
    monkeypatch.setattr(df, "get_prop",
                        lambda obj, names, default=None: props.get(names[0], default))

    def fake_set(obj, names, value):
        calls.append((names[0], value))
        return names[0]

    monkeypatch.setattr(df, "set_prop", fake_set)
    return calls, snap


def test_apply_draft_writes_snapshot_before_mutating(tmp_path, monkeypatch):
    """The module's stated contract: the crash-safe file exists BEFORE the first
    set_prop — a mid-apply crash must never leave mutated props with no recovery file."""
    props = {"options_progressiveNoiseThreshold": 0.01}
    calls, snap = _draft_stub(tmp_path, monkeypatch, props)

    def guarded_set(obj, names, value):
        assert snap.exists(), "set_prop ran before the snapshot file was written"
        calls.append((names[0], value))
        return names[0]

    monkeypatch.setattr(df, "set_prop", guarded_set)
    lines = df.apply_draft()
    assert calls == [("options_progressiveNoiseThreshold", 0.05)]
    assert any("draft: options_progressiveNoiseThreshold 0.01 → 0.05" in ln
               for ln in lines)
    assert json.loads(snap.read_text()) == {"options_progressiveNoiseThreshold": 0.01}


def test_apply_draft_snapshot_write_failure_touches_nothing(tmp_path, monkeypatch):
    """If the safety file can't be written, draft mode aborts with ZERO props mutated
    (the old code rolled back props it had already set — now nothing is set at all)."""
    props = {"options_progressiveNoiseThreshold": 0.01}
    calls, _ = _draft_stub(tmp_path, monkeypatch, props)

    def bad_open(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(builtins, "open", bad_open)
    lines = df.apply_draft()
    assert calls == []                                               # nothing touched
    assert any("ABORTED" in ln and "untouched" in ln for ln in lines)


def test_apply_draft_logs_only_changes_that_happened(tmp_path, monkeypatch):
    """set_prop can pass isProperty yet fail the setattr — the log must not claim a
    change that never happened (and restore must not be lied to either)."""
    props = {"options_progressiveNoiseThreshold": 0.01,
             "options_progressiveMaxSubdivs": 24}
    calls, snap = _draft_stub(tmp_path, monkeypatch, props)
    monkeypatch.setattr(df, "set_prop", lambda obj, names, v: None)  # every set fails
    lines = df.apply_draft()
    assert not any("→" in ln and "draft: options" in ln and "would not" not in ln
                   for ln in lines)
    assert sum("would not take the draft value" in ln for ln in lines) == 2
    assert json.loads(snap.read_text()) == props                     # honest originals


def test_restore_draft_handles_string_typed_snapshot_values(tmp_path, monkeypatch):
    """Hand-edited snapshot with quoted numbers: coerce, restore, format the COERCED
    value (f'{str:g}' used to raise outside the guard and re-fail at every launch)."""
    snap = tmp_path / "draft_snapshot.json"
    snap.write_text(json.dumps({"options_progressiveNoiseThreshold": "0.05",
                                "options_progressiveMaxSubdivs": "12"}))
    currents = {"options_progressiveNoiseThreshold": 0.01,
                "options_progressiveMaxSubdivs": 24}
    writes = []
    monkeypatch.setattr(df, "SNAPSHOT_PATH", str(snap))
    monkeypatch.setattr(df, "_renderer", lambda: object())
    monkeypatch.setattr(df, "get_prop",
                        lambda obj, names, default=None: currents.get(names[0], default))
    monkeypatch.setattr(df, "set_prop",
                        lambda obj, names, v: writes.append((names[0], v)) or names[0])
    lines = df.restore_draft()
    assert writes == [("options_progressiveNoiseThreshold", 0.05),
                      ("options_progressiveMaxSubdivs", 12)]         # int re-coerced
    assert any("restored options_progressiveNoiseThreshold → 0.05" in ln for ln in lines)
    assert any("restored options_progressiveMaxSubdivs → 12" in ln for ln in lines)
    assert not snap.exists()                                         # always cleared


def test_restore_draft_never_raises_on_garbage_values(tmp_path, monkeypatch):
    """A structurally-valid JSON snapshot with nonsense values degrades per-prop and
    still clears the file — restore is on the launch/finally path and must never raise."""
    snap = tmp_path / "draft_snapshot.json"
    snap.write_text(json.dumps({"weird": {"nested": 1}, "ok_prop": 2.0}))
    monkeypatch.setattr(df, "SNAPSHOT_PATH", str(snap))
    monkeypatch.setattr(df, "_renderer", lambda: object())
    monkeypatch.setattr(df, "get_prop", lambda obj, names, default=None: default)
    monkeypatch.setattr(df, "set_prop",
                        lambda obj, names, v: names[0] if names[0] == "ok_prop" else None)
    lines = df.restore_draft()
    assert any("could not restore weird" in ln for ln in lines)
    assert any("restored ok_prop → 2" in ln for ln in lines)
    assert not snap.exists()
