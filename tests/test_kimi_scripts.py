"""Cluster-L regression tests — scripts/install.bat + the four scripts/*.py fixes.

Everything runs off-Max: pymxs is stubbed in sys.modules where a script's in-Max branch
is exercised, and the Omega gateway is always monkeypatched (tests never touch the
network). Covers the round-4 audit fixes:
  * install.bat — no delayed expansion ('!' in clone paths survives) · repo_path
    recording tolerates a corrupt config.json and is errorlevel-checked;
  * preflight.py — vantage_console.exe only required on the "vantage_cli" backend;
  * live_gateway_smoke.py — the API key is never printed, not even a prefix;
  * sim_match.py — docstring matches asserted criteria · Pillow need is loud ·
    ANALYZE/DELTAS/SWEEP degrade to scripted on OmegaError instead of dying;
  * onbox_spikes.py — EV-direction spike is genome-clamp-aware · the scene restore
    in the finally is itself guarded.
"""

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_KEY = "oc_SUPERSECRETdo_not_leak_0123456789abcdef"


def load_script(name):
    """Import a scripts/*.py file as a module (scripts/ is deliberately not a package)."""
    path = os.path.join(REPO, "scripts", name)
    spec = importlib.util.spec_from_file_location(
        "maxgaffer_script_" + name[:-3].replace(os.sep, "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_script(name):
    with open(os.path.join(REPO, "scripts", name), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- install.bat

def test_install_bat_leaves_delayed_expansion_off():
    """A clone path containing '!' (legal NTFS, e.g. D:\\Dropbox!\\3DSL) must survive —
    EnableDelayedExpansion re-scans %REPO% at use sites and eats the bangs."""
    text = read_script("install.bat")
    assert "EnableDelayedExpansion" not in text
    assert "DisableDelayedExpansion" in text


def _repo_record_payload():
    m = re.search(r'-c "(import json,os,sys;.+?)" "%REPO%"', read_script("install.bat"))
    assert m, "install.bat no longer records repo_path via a python -c one-liner"
    return m.group(1)


def _run_record_payload(tmp_path, preexisting):
    cfgdir = tmp_path / "MaxGaffer"
    cfgdir.mkdir(exist_ok=True)
    if preexisting is not None:
        (cfgdir / "config.json").write_text(preexisting, encoding="utf-8")
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path))
    repo = "D:\\Dropbox!\\3DSL" if os.sep == "\\" else "/tmp/Dropbox!/3DSL"
    r = subprocess.run([sys.executable, "-c", _repo_record_payload(), repo],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads((cfgdir / "config.json").read_text(encoding="utf-8")), repo


def test_install_bat_records_repo_path_over_corrupt_config(tmp_path):
    """A crash-truncated config.json must not kill the recording step (or the install)."""
    cfg, repo = _run_record_payload(tmp_path, '{"api_key": "oc_')
    assert cfg["repo_path"] == repo          # started over from {} and still recorded


def test_install_bat_record_preserves_existing_config_keys(tmp_path):
    cfg, repo = _run_record_payload(tmp_path, json.dumps({"api_key": "oc_keepme"}))
    assert cfg["api_key"] == "oc_keepme"     # merged, not clobbered
    assert cfg["repo_path"] == repo


def test_install_bat_record_step_is_errorlevel_checked():
    """The recording step (and its setx fallback) must not fail silently."""
    text = read_script("install.bat")
    section = text[text.index("recording the clone path"):text.index("=== done ===")]
    assert section.count("errorlevel 1") >= 2   # python step + setx fallback


# ---------------------------------------------------------------- preflight.py

def _fake_pymxs():
    mod = types.ModuleType("pymxs")
    mod.runtime = types.SimpleNamespace(
        classOf=lambda obj: "VRay7",
        renderers=types.SimpleNamespace(current=object()),
        vrayExportVRScene=lambda **kw: True,
    )
    return mod


def _run_preflight_in_fake_max(monkeypatch, capsys, cfg):
    from maxgaffer.maxbridge import config as cfgmod
    from maxgaffer.maxbridge import scene

    monkeypatch.setattr(sys, "argv", ["preflight.py"])      # pytest's argv is not a key
    monkeypatch.setitem(sys.modules, "pymxs", _fake_pymxs())
    monkeypatch.setattr(cfgmod, "load", lambda: cfg)
    monkeypatch.setattr(scene, "classify_rig",
                        lambda: {"sun": None, "dome": None, "groups": {}, "notes": []})
    monkeypatch.setattr(scene, "list_cameras",
                        lambda: [{"name": "Cam001", "yaw_deg": 0.0}])
    load_script("preflight.py").main()
    return capsys.readouterr().out


def _cfg(tmp_path, backend, console_exists):
    from maxgaffer.maxbridge.config import Config

    exe = tmp_path / "vantage.exe"
    exe.write_bytes(b"")
    console = tmp_path / "vantage_console.exe"
    if console_exists:
        console.write_bytes(b"")
    return Config(api_key="", vantage_exe=str(exe), vantage_console=str(console),
                  final_render_backend=backend)


def test_preflight_stock_vantage_box_does_not_require_console(tmp_path, monkeypatch, capsys):
    """SPEC §2: stock Vantage 3.x removed its render CLI and 'vray' is the default
    backend — a clean supported box must read green, not [!!]."""
    out = _run_preflight_in_fake_max(monkeypatch, capsys, _cfg(tmp_path, "vray", False))
    assert "[ok] vantage.exe" in out
    assert "[!!] vantage" not in out
    assert "not required" in out                       # the skip is explained, not silent


def test_preflight_vantage_cli_backend_requires_console(tmp_path, monkeypatch, capsys):
    out = _run_preflight_in_fake_max(
        monkeypatch, capsys, _cfg(tmp_path, "vantage_cli", False))
    assert "[!!] vantage_console.exe" in out
    assert "Developer Edition" in out


def test_preflight_vantage_cli_backend_console_present(tmp_path, monkeypatch, capsys):
    out = _run_preflight_in_fake_max(
        monkeypatch, capsys, _cfg(tmp_path, "vantage_cli", True))
    assert "[ok] vantage_console.exe" in out


# ---------------------------------------------------------------- live_gateway_smoke.py

def test_smoke_never_prints_any_key_characters(monkeypatch, capsys):
    """Docstring contract: 'The key is never printed' — CI logs/pasted reports must not
    carry even a prefix/suffix of the credential."""
    smoke = load_script("live_gateway_smoke.py")
    monkeypatch.setattr(smoke, "discover_key", lambda: FAKE_KEY)

    def auth_fail(*a, **kw):
        raise smoke.omega.OmegaError("Gateway returned 401", "auth")

    monkeypatch.setattr(smoke.omega, "ping", auth_fail)
    assert smoke.main() == 1
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    for frag in (FAKE_KEY[:6], FAKE_KEY[-4:], "SUPERSECRET", "0123456789"):
        assert frag not in out, f"key fragment leaked into output: {frag!r}"
    assert "redacted" in out


# ---------------------------------------------------------------- sim_match.py

def test_sim_docstring_matches_asserted_criteria_and_declares_pillow():
    doc = ast.get_docstring(ast.parse(read_script("sim_match.py")))
    assert "0.5 stop" in doc and "700 K" in doc        # the code asserts exactly these
    assert "0.75" not in doc and "800 K" not in doc    # stale looser criteria are gone
    assert "Pillow" in doc                             # the hard dependency is documented


def test_sim_render_without_pillow_fails_loud_and_friendly(tmp_path, monkeypatch):
    sim = load_script("sim_match.py")
    monkeypatch.setitem(sys.modules, "PIL", None)      # simulate a Pillow-less python
    with pytest.raises(SystemExit) as exc:
        sim.World(str(tmp_path)).render(sim.state_of(sim.TARGET), "t")
    assert "pip install pillow" in str(exc.value)


def _ref_png(tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(b"\x89PNG-fake-but-base64s-fine")    # only base64'd, never decoded
    return str(p)


def test_analyze_semantics_falls_back_when_gateway_down(tmp_path, monkeypatch):
    """OmegaError on every sample → scripted semantics, no traceback, no OVERALL lost."""
    sim = load_script("sim_match.py")

    def down(*a, **kw):
        raise sim.omega.OmegaError("Gateway returned 401", "auth")

    monkeypatch.setattr(sim.omega, "call", down)
    sem = sim.analyze_semantics("oc_x", True, sim.ScriptedLLM(), _ref_png(tmp_path),
                                log=lambda m: None)
    assert sem["time_of_day"] == "golden_hour"         # scripted semantics stood in
    assert sem["sun_active"] is True


def test_analyze_semantics_falls_back_when_all_replies_junk(tmp_path, monkeypatch):
    """Non-JSON on all 3 samples → consolidate_analyses([]) would raise ValueError;
    the scripted fallback must stand in instead (the offline contract, mid-run)."""
    sim = load_script("sim_match.py")
    monkeypatch.setattr(sim.omega, "call", lambda *a, **kw: "the sun is probably up?")
    sem = sim.analyze_semantics("oc_x", True, sim.ScriptedLLM(), _ref_png(tmp_path),
                                log=lambda m: None)
    assert sem["time_of_day"] == "golden_hour"


def test_analyze_semantics_consolidates_real_live_samples(tmp_path, monkeypatch):
    sim = load_script("sim_match.py")
    replies = iter([
        json.dumps({"time_of_day": "midday", "sun_bearing_deg": 10.0, "confidence": 0.9}),
        json.dumps({"time_of_day": "midday", "sun_bearing_deg": 20.0, "confidence": 0.8}),
        json.dumps({"time_of_day": "golden_hour", "sun_bearing_deg": 15.0,
                    "confidence": 0.4}),
    ])
    monkeypatch.setattr(sim.omega, "call", lambda *a, **kw: next(replies))
    sem = sim.analyze_semantics("oc_x", True, sim.ScriptedLLM(), _ref_png(tmp_path),
                                log=lambda m: None)
    assert sem["time_of_day"] == "midday"              # majority won
    assert 14.0 <= sem["sun_bearing_deg"] <= 16.0      # circular mean of 10/20/15
    assert sem["consensus_agreement"] == pytest.approx(2 / 3, abs=0.01)


def test_sim_catches_omega_error_on_every_live_call_site():
    """ANALYZE, DELTAS and SWEEP each guard omega.call — run_match/run_sun_sweep only
    tolerate ParseError, so an uncaught OmegaError would still kill Phase B."""
    src = read_script("sim_match.py")
    assert src.count("except omega.OmegaError") >= 3


# ---------------------------------------------------------------- onbox_spikes.py

def test_ev_probe_delta_stays_inside_genome_bounds(capsys):
    spikes = load_script("onbox_spikes.py")            # prints the off-Max notice; harmless
    capsys.readouterr()
    assert spikes._ev_probe_delta(11.5) == 2.0
    assert spikes._ev_probe_delta(18.0) == 2.0         # exactly at the bound is a full probe
    assert spikes._ev_probe_delta(19.0) == -2.0        # +2 would clamp at genome max 20.0


def test_ev_probe_delta_matches_genome_spec(capsys):
    """The helper reads the real genome table, not a hard-coded bound."""
    spikes = load_script("onbox_spikes.py")
    capsys.readouterr()
    from maxgaffer.core.genome import SPEC_BY_KEY

    spec = SPEC_BY_KEY["exposure.ev"]
    assert spikes._ev_probe_delta(spec.hi - 1.0) == -2.0
    assert spikes._ev_probe_delta(spec.lo + 1.0) == 2.0


def test_onbox_scene_restore_in_finally_is_guarded():
    """The restore apply_state in the finally must sit inside its own try — a failing
    restore may not propagate out of main() and eat the spike report."""
    tree = ast.parse(read_script("onbox_spikes.py"))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.finalbody:
            calls_apply = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "apply_state" for n in ast.walk(stmt))
            if calls_apply and not isinstance(stmt, ast.Try):
                problems.append("unguarded apply_state directly in a finally block")
    assert not problems, problems
