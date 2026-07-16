"""Controller — the one object the UI talks to. Wires core (brains) to bridge (hands).

Owns: the per-scene Session, the classified rig + light baselines, the stats-provider chain,
the three LLM calls, and the director hooks. Everything scene-touching stays main-thread
(the UI guarantees callers); LLM/stats calls are pure I/O the UI may run on workers.

Stats provider chain (first that yields wins):
  1. core.metrics in-process — Pillow if installed; for our own PNG renders the stdlib
     reader ALWAYS works, so loop stats never fail;
  2. sidecar: config.system_python -m maxgaffer.sidecar.metrics_cli (Pillow there);
  3. references only: Max transcode → PNG → stdlib reader.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..core import metrics, omega, prompts, rules
from ..core.director import Hooks, MatchConfig, MatchResult, run_match, run_sun_sweep
from ..core.genome import LightingState
from ..core.parse import ParseError, validate_analysis
from ..core.session import Session, preset_dumps, preset_loads, sidecar_path
from . import apply as ap
from . import config as cfgmod
from . import draft as df
from . import render as rd
from . import scene as sc
from . import vantage as vt

# formats Max reads natively but Pillow/stdlib can't — always ingest via Max transcode
MAX_FIRST_EXTS = (".exr", ".hdr", ".tif", ".tiff")


def _needs_max_ingest(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MAX_FIRST_EXTS


class Controller:
    def __init__(self, cfg: Optional[cfgmod.Config] = None):
        self.cfg = cfg or cfgmod.load()
        self._session: Optional[Session] = None
        self._session_scene = None
        self._rig = None
        self._baselines: Dict[int, float] = {}
        self._ref_cache: Dict[str, Dict] = {}     # ref path+mtime → stats
        self._run_dir: Optional[str] = None
        # pure-I/O runner — the UI swaps in a worker-thread pump so gateway waits never
        # freeze Max; pymxs is NEVER called through this (network/subprocess only)
        self.io: Callable = lambda fn: fn()

    # ------------------------------------------------------------------ scene / session
    @property
    def session(self) -> Session:
        scene = sc.scene_path()
        if self._session is None or scene != self._session_scene:
            self._session = Session.load(sidecar_path(scene))
            self._session_scene = scene
        return self._session

    def save_session(self) -> bool:
        self.session.path = sidecar_path(sc.scene_path())   # scene may have been saved-as
        return self.session.save()

    def rig(self, refresh: bool = False):
        if self._rig is None or refresh:
            self._rig = sc.classify_rig()
            # adopt-only-new into the session: re-scans NEVER overwrite a known baseline,
            # so a group MaxGaffer previously dimmed to 0 keeps its authored value
            fresh = ap.capture_baselines(self._rig)
            if self.session.adopt_baselines(fresh):
                self.save_session()
            self._baselines = dict(self.session.baselines)
        return self._rig

    def cameras(self) -> List[Dict]:
        cams = sc.list_cameras()
        for c in cams:
            e = self.session.cameras.get(c["name"])
            c["reference"] = e.reference if e else ""
            c["score"] = e.score if e else None
            c["has_state"] = bool(e and e.state)
        return cams

    def read_state(self, camera_name: str = "") -> LightingState:
        cam = sc.get_camera(camera_name) if camera_name else None
        return ap.read_state(self.rig(), self._baselines, cam)

    def apply_state(self, state: LightingState, camera_name: str = "") -> List[str]:
        cam = sc.get_camera(camera_name) if camera_name else None
        return ap.apply_state(self.rig(), self._baselines, state, cam)

    def select_camera(self, camera_name: str, apply_saved: bool = True) -> List[str]:
        sc.set_active_camera(camera_name)
        warnings: List[str] = []
        if apply_saved and self.session.settings.get("apply_on_select", True):
            e = self.session.cameras.get(camera_name)
            if e and e.state is not None:
                warnings = self.apply_state(e.state, camera_name)
        return warnings

    # ------------------------------------------------------------------ stats providers
    def stats_for(self, path: str) -> Optional[Dict]:
        s = metrics.compute_stats(path)
        if s is not None:
            return s
        return self._sidecar_stats(path)

    def _sidecar_stats(self, path: str) -> Optional[Dict]:
        py = self.cfg.system_python
        if not py or not os.path.exists(py):
            return None
        try:
            repo = self.cfg.repo_path or os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            proc = subprocess.run(
                [py, "-m", "maxgaffer.sidecar.metrics_cli", path],
                capture_output=True, text=True, timeout=60, cwd=repo)
            data = json.loads(proc.stdout or "null")
            if isinstance(data, list) and data and isinstance(data[0].get("stats"), dict):
                return data[0]["stats"]
        except Exception:
            pass
        return None

    def ref_stats(self, ref_path: str) -> Optional[Dict]:
        try:
            key = f"{ref_path}:{os.path.getmtime(ref_path)}"
        except OSError:
            return None
        if key in self._ref_cache:
            return self._ref_cache[key]
        s = None
        if _needs_max_ingest(ref_path):   # EXR/HDR/TIFF: Max's bitmap I/O is the reader
            png = self._transcode_ref(ref_path)
            if png:
                s = metrics.compute_stats(png)
        if s is None:
            s = self.stats_for(ref_path)
        if s is None:  # last resort: Max transcodes anything it can read to a small PNG
            png = self._transcode_ref(ref_path)
            if png:
                s = metrics.compute_stats(png)
        if s is not None:
            self._ref_cache[key] = s
        return s

    def _transcode_ref(self, ref_path: str) -> Optional[str]:
        png = os.path.join(self._ensure_run_dir("refs"),
                           "ref_" + _safe(os.path.basename(ref_path)) + ".png")
        return rd.transcode_to_png(ref_path, png)

    # ------------------------------------------------------------------ LLM plumbing
    def _image_block(self, path: str) -> Optional[dict]:
        """Payload-slim image block: Pillow in-process → sidecar --b64 → raw file (small
        renders) → Max transcode to PNG. EXR/HDR/TIFF skip straight to Max transcode."""
        if _needs_max_ingest(path):
            png = os.path.join(self._ensure_run_dir("refs"),
                               "llm_" + _safe(os.path.basename(path)) + ".png")
            if rd.transcode_to_png(path, png, max_dim=768):
                return omega.image_block_from_file(png)
            return None
        try:
            from PIL import Image  # type: ignore
            import io

            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
            return omega.image_block(
                base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg")
        except Exception:
            pass
        py = self.cfg.system_python
        if py and os.path.exists(py):
            try:
                repo = self.cfg.repo_path or os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                proc = subprocess.run(
                    [py, "-m", "maxgaffer.sidecar.metrics_cli", path, "--b64"],
                    capture_output=True, text=True, timeout=60, cwd=repo)
                data = json.loads(proc.stdout or "null")
                if isinstance(data, list) and data and data[0].get("b64"):
                    return omega.image_block(data[0]["b64"],
                                             data[0].get("media_type", "image/jpeg"))
            except Exception:
                pass
        try:
            if os.path.getsize(path) <= 3_500_000:
                block = omega.image_block_from_file(path)
                if block is not None:
                    return block
        except OSError:
            return None
        png = os.path.join(self._ensure_run_dir("refs"),
                           "llm_" + os.path.basename(path) + ".png")
        if rd.transcode_to_png(path, png, max_dim=768):
            return omega.image_block_from_file(png)
        return None

    def analyze_reference(self, camera_name: str) -> Dict:
        """ANALYZE call (cached in the session until the reference changes)."""
        e = self.session.entry(camera_name)
        if not e.reference:
            raise RuntimeError("no reference image bound to this camera")
        if e.semantics:
            return e.semantics
        block = self._image_block(e.reference)
        if block is None:
            raise RuntimeError(f"could not read reference image: {e.reference}")
        messages = [{"role": "user",
                     "content": [block, omega.text_block(prompts.analyze_user_text())]}]
        reply = self.io(lambda: omega.call(
            self.cfg.api_key, prompts.ANALYZE_SYSTEM, messages,
            model=self.cfg.model, max_tokens=2048))
        try:
            semantics = validate_analysis(reply)
        except ParseError:   # one strict retry — a wasted run costs far more than a call
            retry = messages + [
                {"role": "assistant", "content": reply[:1500]},
                {"role": "user", "content": "That was not valid JSON. Reply with ONLY the "
                                            "JSON object, nothing else."}]
            semantics = validate_analysis(self.io(lambda: omega.call(
                self.cfg.api_key, prompts.ANALYZE_SYSTEM, retry,
                model=self.cfg.model, max_tokens=2048)))
        e.semantics = semantics
        self.save_session()
        return semantics

    def _llm_deltas_hook(self, ref_block: dict) -> Callable[[Dict], str]:
        def call_llm(ctx: Dict) -> str:
            render_block = self._image_block(ctx["render_path"])
            content = [ref_block]
            if render_block is not None:
                content.append(render_block)
            content.append(omega.text_block(prompts.deltas_user_text(
                ctx["state_table"], ctx["semantics"], ctx["score_history"],
                ctx["analytic_applied"], ctx["iteration"], ctx["max_iterations"],
                ctx.get("rig_notes", ""))))
            return self.io(lambda: omega.call(
                self.cfg.api_key, prompts.DELTAS_SYSTEM,
                [{"role": "user", "content": content}],
                model=self.cfg.model, max_tokens=2048))
        return call_llm

    # ------------------------------------------------------------------ the headline act
    def run_match(
        self,
        camera_name: str,
        log: Callable[[str], None],
        should_cancel: Callable[[], bool] = lambda: False,
        locks: Optional[set] = None,
        do_sweep: bool = False,
    ) -> MatchResult:
        e = self.session.entry(camera_name)
        if not e.reference:
            raise RuntimeError("bind a reference image to this camera first")
        rig = self.rig(refresh=True)
        cam = sc.get_camera(camera_name)
        if cam is None:
            raise RuntimeError(f"camera '{camera_name}' not found in the scene")
        sc.set_active_camera(camera_name)
        locks = set(locks if locks is not None else e.locks)
        run_dir = self._new_run_dir(camera_name)
        log(f"run dir: {run_dir}")

        # snapshot the light as it stands — matches are explorations, not commitments
        e.pre_match = ap.read_state(rig, self._baselines, cam)
        self.save_session()

        log("analyzing reference…")
        semantics = self.analyze_reference(camera_name)
        log(f"reference: {semantics['time_of_day']}, {semantics['sky']} sky, "
            f"sun {semantics['sun_altitude_band']} @ bearing "
            f"{semantics['sun_bearing_deg']:+.0f}°, wb ~{semantics['wb_kelvin_estimate']:.0f}K"
            f" — {semantics['key_notes']}")

        ref_stats = self.ref_stats(e.reference)
        if ref_stats is None:
            log("⚠ reference stats unavailable (install Pillow or set system_python) — "
                "running LLM-visual mode")
        ref_block = self._image_block(e.reference)
        if ref_block is None:
            raise RuntimeError("reference image could not be prepared for the LLM")

        current = ap.read_state(rig, self._baselines, cam)
        start, why = rules.initial_state(semantics, current, sc.camera_yaw_deg(cam), locks,
                                         overcast_sun_mode=self.cfg.overcast_sun_mode)
        for line in why:
            log("first guess: " + line)

        draft_applied = False
        if self.cfg.draft_sampler:
            for line in df.apply_draft():
                log(line)
            draft_applied = df.pending_snapshot()

        def render_hook(tag: str):
            path = rd.render_frame(cam, os.path.join(run_dir, f"{tag}.png"),
                                   self.cfg.loop_width, self.cfg.loop_height)
            if path:
                log(f"THUMB::{path}")   # UI renders these markers as inline thumbnails
            return path

        hooks = Hooks(
            apply=lambda st: self._apply_logged(rig, st, cam, log),
            render=render_hook,
            stats=self.stats_for,
            llm_deltas=self._llm_deltas_hook(ref_block),
            log=log,
            should_cancel=should_cancel,
        )

        if do_sweep and rig.get("sun") is not None and "sun.azimuth_deg" not in locks:
            log(f"sun sweep: {self.cfg.sweep_count} directions…")
            az, alt_hint, _why = run_sun_sweep(
                start, rules.sweep_azimuths(self.cfg.sweep_count), hooks,
                llm_pick=lambda paths, azs: self._sweep_call(ref_block, paths, azs))
            if az is not None:
                start.set("sun.azimuth_deg", az)
                # the hint was judged against real renders of THIS scene — trust it over
                # the ANALYZE band when the altitude isn't locked
                if alt_hint != "na" and "sun.altitude_deg" not in locks \
                        and "sun.altitude_deg" in start.values:
                    start.set("sun.altitude_deg", rules.ALTITUDE_DEG.get(
                        alt_hint, start.get("sun.altitude_deg")))
                    log(f"sweep: altitude refined to "
                        f"{start.get('sun.altitude_deg'):.0f}° ('{alt_hint}')")

        cfg = MatchConfig(
            max_iterations=int(self.cfg.max_iterations),
            target_score=float(self.cfg.target_score),
            analytic=ref_stats is not None,
            weights=self.cfg.critic_weights or None,
        )
        try:
            result = run_match(start, ref_stats, semantics, hooks, cfg, locks,
                               rig_notes="; ".join(rig.get("notes", [])))
        finally:
            if draft_applied:   # crash-safe: even a raise puts the artist's sampler back
                for line in df.restore_draft():
                    log(line)
        e.locks = locks
        self.session.record_match(camera_name, result.best_state, result.best_score)
        self.save_session()
        score_txt = f"{result.best_score:.1f}" if result.best_score is not None else "n/a"
        log(f"match finished: {result.stop_reason}, best score {score_txt} "
            f"({len(result.iterations)} iterations)")
        return result

    def match_all(
        self,
        log: Callable[[str], None],
        should_cancel: Callable[[], bool] = lambda: False,
        do_sweep: bool = True,
    ) -> Dict[str, str]:
        """Unattended queue: match every camera that has a reference bound, sequentially.
        Per-camera failures are recorded and the queue continues; cancel stops between
        cameras (and mid-match via the shared flag)."""
        results: Dict[str, str] = {}
        queue = [name for name, e in self.session.cameras.items() if e.reference]
        if not queue:
            return {"": "no cameras have references bound"}
        for i, name in enumerate(queue):
            if should_cancel():
                results[name] = "cancelled"
                break
            log(f"— batch {i + 1}/{len(queue)}: {name} —")
            try:
                r = self.run_match(name, log, should_cancel,
                                   locks=None, do_sweep=do_sweep)
                results[name] = (f"{r.best_score:.1f}" if r.best_score is not None
                                 else r.stop_reason)
            except Exception as err:  # noqa: BLE001 one bad camera must not kill the night
                results[name] = f"error: {err}"
                log(f"✗ {name}: {err}")
        return results

    # ------------------------------------------------------------------ presets / HDRI
    def save_preset(self, path: str, camera_name: str = "") -> bool:
        state = self.read_state(camera_name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(preset_dumps(state, name=os.path.basename(path), now=_stamp()))
            return True
        except OSError:
            return False

    def load_preset(self, path: str, camera_name: str = "") -> List[str]:
        """Apply a preset to the scene; if a camera is given, it becomes that camera's
        saved state too. Raises on an unreadable/invalid file."""
        with open(path, "r", encoding="utf-8") as f:
            state = preset_loads(f.read())
        if state is None:
            raise RuntimeError(f"not a MaxGaffer preset: {path}")
        warnings = self.apply_state(state, camera_name)
        if camera_name:
            self.session.record_match(camera_name, state, None)
            self.save_session()
        return warnings

    def set_dome_hdri(self, hdri_path: str) -> str:
        dome = self.rig().get("dome")
        if dome is None:
            return "failed"
        how = sc.set_dome_texture(dome, hdri_path)
        try:
            import pymxs

            pymxs.runtime.redrawViews()
        except Exception:
            pass
        return how

    def _sweep_call(self, ref_block: dict, paths: List[str], azimuths: List[float]) -> str:
        content: List[dict] = [ref_block]
        for p in paths:
            block = self._image_block(p)
            if block is not None:
                content.append(block)
        content.append(omega.text_block(prompts.sweep_user_text(azimuths)))
        return self.io(lambda: omega.call(
            self.cfg.api_key, prompts.SWEEP_SYSTEM,
            [{"role": "user", "content": content}],
            model=self.cfg.model, max_tokens=1024))

    def _apply_logged(self, rig, state: LightingState, cam, log) -> None:
        for w in ap.apply_state(rig, self._baselines, state, cam):
            log("⚠ " + w)

    # ------------------------------------------------------------------ restore
    def restore_pre_match(self, camera_name: str) -> bool:
        e = self.session.cameras.get(camera_name)
        if not (e and e.pre_match is not None):
            return False
        self.apply_state(e.pre_match, camera_name)
        return True

    # ------------------------------------------------------------------ vantage
    def start_live_link(self) -> Tuple[bool, str]:
        return vt.start_live_link()

    def prepare_vantage_jobs(
        self,
        camera_names: List[str],
        out_dir: str,
        on_progress: Callable[[str, str], None],
        use_saved_states: bool = True,
    ) -> List[Dict]:
        """MAIN-THREAD half: per camera, apply its saved lighting state and export the
        .vrscene. Raises on export failure (nothing has rendered yet — cheap to abort)."""
        jobs: List[Dict] = []
        export_dir = os.path.join(self._ensure_run_dir("vantage"), _stamp())
        for name in camera_names:
            on_progress(name, "applying + exporting")
            if use_saved_states:
                e = self.session.cameras.get(name)
                if e and e.state is not None:
                    self.apply_state(e.state, name)
            scene_file = vt.export_vrscene(
                os.path.join(export_dir, f"{_safe(name)}.vrscene"), name)
            if scene_file is None:
                raise RuntimeError(f"{name}: vrscene export failed "
                                   "(vrayExportVRScene missing or camera not set)")
            jobs.append({"camera": name, "scene_file": scene_file,
                         "output": os.path.join(out_dir, f"{_safe(name)}.png")})
        return jobs

    def run_vantage_jobs(self, jobs: List[Dict],
                         on_progress: Callable[[str, str], None]) -> Dict[str, str]:
        """Pure-subprocess half — NO pymxs, safe to run on a worker thread so a multi-hour
        vantage_console batch never freezes Max."""
        return vt.render_stills(jobs, self.cfg.vantage_console,
                                self.cfg.final_width, self.cfg.final_height, on_progress)

    # ------------------------------------------------------------------ dirs
    def _ensure_run_dir(self, sub: str) -> str:
        stem = _safe(os.path.splitext(os.path.basename(sc.scene_path() or "unsaved"))[0])
        d = os.path.join(cfgmod.sessions_dir(), stem, sub)
        os.makedirs(d, exist_ok=True)
        return d

    def _new_run_dir(self, camera_name: str) -> str:
        parent = self._ensure_run_dir(_safe(camera_name))
        d = os.path.join(parent, _stamp())
        os.makedirs(d, exist_ok=True)
        self._run_dir = d
        prune_old_runs(parent, keep=int(self.cfg.keep_runs))
        return d


def prune_old_runs(parent_dir: str, keep: int) -> int:
    """Delete the oldest run folders beyond ``keep`` (timestamp names sort chronologically).
    keep <= 0 disables pruning. Returns how many were removed."""
    if keep <= 0:
        return 0
    try:
        dirs = sorted(d for d in os.listdir(parent_dir)
                      if os.path.isdir(os.path.join(parent_dir, d)))
    except OSError:
        return 0
    removed = 0
    for d in dirs[:-keep]:
        import shutil

        shutil.rmtree(os.path.join(parent_dir, d), ignore_errors=True)
        removed += 1
    return removed


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "unnamed"


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
