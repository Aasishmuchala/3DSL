"""The match loop — a sans-IO state machine. Hooks do the touching; this does the thinking.

Iteration shape (one render per iteration — renders are the expensive resource):
    apply(state) → render → stats → score → [converged? stop]
    → analytic solve (EV/WB, deterministic) + LLM deltas (geometry/mood, bounded)
    → merge into next state → repeat

Reliability guards, in the MaxDirector tradition:
  * keep-best — the best-scoring state is tracked and ALWAYS re-applied at the end, so an
    exploratory move that made things worse can never be the final answer;
  * revert-on-slump — two consecutive scores meaningfully below best snap the loop back to
    the best state before asking the LLM again (one exploratory move is allowed, a slide
    is not);
  * every LLM proposal passes genome validation (unknown → dropped, locked → refused,
    bounds → clamped, per-iteration step → limited);
  * metrics missing (no stats engine / no scores) degrades to LLM-visual-only mode with the
    analytic solver off — the loop still runs, the log says so loudly.

All hooks are injected, so the whole loop is unit-tested off-Max with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import critic, solver
from .genome import LightingState, apply_changes, state_table
from .parse import ParseError


@dataclass
class MatchConfig:
    max_iterations: int = 5
    target_score: float = 82.0
    stall_delta: float = 1.5      # min improvement over best to count as progress
    stall_patience: int = 2       # iterations without progress before stopping
    slump_tolerance: float = 3.0  # how far below best counts as a slump
    analytic: bool = True         # run the EV/WB histogram solver each iteration
    max_changes: int = 4
    weights: Optional[Dict[str, float]] = None
    # analytic LEASH — total movement from the run's start state. The solver matches
    # histograms of DIFFERENT scenes, so scene-vs-reference albedo mismatch (white room
    # matched to a walnut library) biases it systematically; the leash bounds the damage
    # and hitting it is reported as a diagnosis, not silently absorbed.
    ev_leash: float = 4.0
    wb_leash: float = 3000.0
    # when the solver had to move EV by more than this in one iteration, the render the
    # LLM just saw was badly mis-exposed — its absolute-brightness judgments (intensities,
    # group levels) are contaminated and get dropped for that iteration
    contaminated_ev_step: float = 1.5


@dataclass
class Hooks:
    """The loop's only contact with the world. ``apply``/``render`` run on Max's main thread
    (the UI layer guarantees that); llm/stats may be slow and are wrapped by the caller."""
    apply: Callable[[LightingState], None]
    render: Callable[[str], Optional[str]]            # tag → image path (None = failed)
    stats: Callable[[str], Optional[Dict]]            # image path → stats dict
    llm_deltas: Callable[[Dict], str]                 # context → raw reply text
    log: Callable[[str], None] = lambda msg: None
    should_cancel: Callable[[], bool] = lambda: False


@dataclass
class IterationRecord:
    index: int
    state: Dict
    render_path: Optional[str] = None
    score: Optional[float] = None
    components: Dict[str, float] = field(default_factory=dict)
    analytic_changes: Dict[str, float] = field(default_factory=dict)
    llm_accepted: Dict[str, float] = field(default_factory=dict)
    llm_rejected: List[str] = field(default_factory=list)
    assessment: str = ""
    reverted_to_best: bool = False


@dataclass
class MatchResult:
    best_state: LightingState
    best_score: Optional[float]
    best_render: Optional[str]
    stop_reason: str
    iterations: List[IterationRecord] = field(default_factory=list)


def run_match(
    start_state: LightingState,
    ref_stats: Optional[Dict],
    semantics: Dict,
    hooks: Hooks,
    cfg: Optional[MatchConfig] = None,
    locks: Optional[Set[str]] = None,
    rig_notes: str = "",
) -> MatchResult:
    cfg = cfg or MatchConfig()
    locks = locks or set()
    metrics_ok = ref_stats is not None
    if not metrics_ok:
        hooks.log("⚠ metrics unavailable — LLM-visual mode (no analytic solve, no scores)")

    state = start_state.copy()
    best_state = state.copy()
    best_score: Optional[float] = None
    best_render: Optional[str] = None
    records: List[IterationRecord] = []
    score_history: List[Tuple[int, float]] = []
    slump_count = 0
    stall_count = 0
    stop_reason = "max_iterations"
    leash_ev_lo = start_state.get("exposure.ev", 10.0) - cfg.ev_leash
    leash_ev_hi = start_state.get("exposure.ev", 10.0) + cfg.ev_leash
    leash_wb_lo = start_state.get("exposure.wb_kelvin", 6500.0) - cfg.wb_leash
    leash_wb_hi = start_state.get("exposure.wb_kelvin", 6500.0) + cfg.wb_leash
    leash_hits = 0

    for i in range(cfg.max_iterations):
        if hooks.should_cancel():
            stop_reason = "cancelled"
            break
        rec = IterationRecord(index=i, state=state.to_dict())
        hooks.apply(state)
        path = hooks.render(f"iter{i:02d}")
        rec.render_path = path
        if path is None:
            hooks.log(f"iter {i}: render failed — stopping")
            stop_reason = "render_failed"
            records.append(rec)
            break

        cur_stats = hooks.stats(path) if metrics_ok else None
        if cur_stats is not None and ref_stats is not None:
            verdict = critic.score(ref_stats, cur_stats, cfg.weights)
            rec.score, rec.components = verdict.score, verdict.components
            score_history.append((i, verdict.score))
            hooks.log(f"iter {i}: score {verdict.summary()}")

            improved = best_score is None or verdict.score > best_score + 1e-9
            if improved:
                if best_score is not None and verdict.score < best_score + cfg.stall_delta:
                    stall_count += 1
                else:
                    stall_count = 0
                best_score, best_state, best_render = verdict.score, state.copy(), path
                slump_count = 0
            else:
                stall_count += 1
                if verdict.score < (best_score or 0) - cfg.slump_tolerance:
                    slump_count += 1
                    if slump_count >= 2:
                        hooks.log(f"iter {i}: slumping — reverting to best "
                                  f"({best_score:.1f})")
                        state = best_state.copy()
                        rec.reverted_to_best = True
                        slump_count = 0
                else:
                    slump_count = 0

            if verdict.score >= cfg.target_score:
                stop_reason = "target_reached"
                records.append(rec)
                break
            if stall_count >= cfg.stall_patience and i >= 1:
                stop_reason = "stalled"
                records.append(rec)
                break
        else:
            if best_render is None:
                best_state, best_render = state.copy(), path

        if rec.reverted_to_best:
            # the stats in hand describe the state we just ABANDONED — solving or asking
            # the LLM from them would tweak the restored state on stale evidence. Re-render
            # first; next iteration reasons from coherent measurements.
            hooks.log(f"iter {i}: reverted — re-measuring before further changes")
            records.append(rec)
            continue

        if i == cfg.max_iterations - 1:  # last render measured; no point proposing more
            records.append(rec)
            break

        # ---- analytic solve (deterministic, before/independent of the LLM)
        analytic: Dict[str, float] = {}
        ev_at_solve = state.get("exposure.ev") if "exposure.ev" in state.values else None
        if cfg.analytic and cur_stats is not None and ref_stats is not None:
            analytic = solver.analytic_pass(state, ref_stats, cur_stats, locks)
            if "exposure.ev" in analytic:
                leashed = min(leash_ev_hi, max(leash_ev_lo, analytic["exposure.ev"]))
                if abs(leashed - analytic["exposure.ev"]) > 1e-6:
                    leash_hits += 1
                    hooks.log(f"iter {i}: EV solve hit its leash "
                              f"({leashed:+.1f} vs wanted {analytic['exposure.ev']:+.1f})")
                analytic["exposure.ev"] = leashed
            if "exposure.wb_kelvin" in analytic:
                leashed = min(leash_wb_hi, max(leash_wb_lo, analytic["exposure.wb_kelvin"]))
                if abs(leashed - analytic["exposure.wb_kelvin"]) > 1e-6:
                    leash_hits += 1
                    hooks.log(f"iter {i}: WB solve hit its leash ({leashed:.0f}K)")
                analytic["exposure.wb_kelvin"] = leashed
            if analytic:
                state, accepted, _ = apply_changes(state, analytic, locks, limit=False)
                rec.analytic_changes = accepted
                hooks.log("iter %d: analytic %s" % (
                    i, ", ".join(f"{k}={v:.2f}" for k, v in accepted.items())))

        # ---- LLM deltas
        if hooks.should_cancel():
            stop_reason = "cancelled"
            records.append(rec)
            break
        ctx = {
            "iteration": i,
            "max_iterations": cfg.max_iterations,
            "state_table": state_table(state, locks),
            "semantics": semantics,
            "score_history": score_history,
            "analytic_applied": rec.analytic_changes,
            "render_path": path,
            "rig_notes": rig_notes,
            "max_changes": cfg.max_changes,
        }
        try:
            from .parse import validate_deltas

            proposal = validate_deltas(hooks.llm_deltas(ctx), cfg.max_changes)
        except ParseError as e:
            hooks.log(f"iter {i}: LLM reply unusable ({e}) — keeping analytic-only step")
            proposal = {"assessment": "", "changes": {}, "reasons": {}, "stop": False}
        rec.assessment = proposal["assessment"]
        if proposal["assessment"]:
            hooks.log(f"iter {i}: gaffer: {proposal['assessment']}")
        # structural, not just prompted: while the analytic solver is running, ANALYTIC
        # params are the solver's alone — live fire showed the model overriding a perfect
        # EV solve and costing two iterations of re-correction (sim_match, 2026-07-16)
        if cfg.analytic and metrics_ok:
            from .genome import spec_for

            for k in [k for k in proposal["changes"]
                      if (spec_for(k) is not None and spec_for(k).analytic)]:
                proposal["changes"].pop(k, None)
                rec.llm_rejected.append(f"{k}: analytic — the solver owns it")
                hooks.log(f"iter {i}: refused {k} (analytic — solver owns it)")
        # contaminated-iteration guard: if the solver just moved EV substantially, the
        # render the LLM critiqued was mis-exposed — drop its absolute-brightness moves.
        # Measured against the EV at solve time (NOT the iteration-start snapshot, which
        # goes stale when a slump-revert swapped the state mid-iteration).
        ev_after = rec.analytic_changes.get("exposure.ev")
        ev_moved = (abs(ev_after - ev_at_solve)
                    if (ev_at_solve is not None and ev_after is not None) else 0.0)
        if ev_moved >= cfg.contaminated_ev_step:
            dropped = [k for k in proposal["changes"]
                       if k.endswith(".intensity") or k.startswith("group.")]
            for k in dropped:
                proposal["changes"].pop(k, None)
                rec.llm_rejected.append(
                    f"{k}: dropped — render was {ev_moved:.1f} stops mis-exposed, "
                    "brightness judgment contaminated")
            if dropped:
                hooks.log(f"iter {i}: dropped {len(dropped)} intensity change(s) — "
                          "the model judged a mis-exposed frame")
        state, accepted, rejected = apply_changes(state, proposal["changes"], locks, limit=True)
        rec.llm_accepted = accepted
        rec.llm_rejected.extend(rejected)   # extend — the contamination guard logged here too
        for r in rejected:
            hooks.log(f"iter {i}: rejected {r}")
        for k, v in accepted.items():
            hooks.log(f"iter {i}: Δ {k} → {v:.2f}  ({proposal['reasons'].get(k, '')})")
        records.append(rec)
        if proposal["stop"] and not accepted and not rec.analytic_changes:
            stop_reason = "llm_satisfied"
            break

    if leash_hits >= 2:
        hooks.log("⚠ the exposure/WB solver kept hitting its leash — the reference and "
                  "this scene likely disagree in albedo (e.g. white room vs dark wood). "
                  "Consider locking exposure.ev / exposure.wb_kelvin and setting them by eye.")

    # ---- always land on the best known state
    if best_score is not None:
        hooks.apply(best_state)
    else:
        best_state = state
        hooks.apply(best_state)
    return MatchResult(
        best_state=best_state,
        best_score=best_score,
        best_render=best_render,
        stop_reason=stop_reason,
        iterations=records,
    )


def run_sun_sweep(
    state: LightingState,
    azimuths: List[float],
    hooks: Hooks,
    llm_pick: Callable[[List[str], List[float]], str],
) -> Tuple[Optional[float], str, str]:
    """Grid-solve the sun direction: render one low-res frame per azimuth, let the LLM do
    multiple-choice (estimation is hard, comparison is easy).
    Returns (azimuth | None, altitude_hint, why) — the hint comes from the same comparison
    (the model sees real renders of THIS scene against the reference) so the caller should
    prefer it over the ANALYZE pass's band when they disagree."""
    from .parse import validate_sweep

    paths: List[str] = []
    kept: List[float] = []
    for az in azimuths:
        if hooks.should_cancel():
            return None, "na", "cancelled"
        probe = state.copy()
        probe.set("sun.azimuth_deg", az)
        hooks.apply(probe)
        path = hooks.render(f"sweep{az:03.0f}")
        if path:
            paths.append(path)
            kept.append(az)
        else:
            hooks.log(f"sweep: render failed at azimuth {az:.0f}° — skipping")
    if len(paths) < 2:
        return None, "na", "not enough sweep renders"
    try:
        picked = validate_sweep(llm_pick(paths, kept), len(paths))
    except ParseError as e:
        return None, "na", f"sweep reply unusable: {e}"
    az = kept[picked["best_index"]]
    hooks.log(f"sweep: azimuth {az:.0f}° — {picked['why']}")
    return az, picked["altitude_hint"], picked["why"]
