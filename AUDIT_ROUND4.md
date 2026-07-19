# Audit Round 4 — adversarial swarm (July 2026) · v0.9.3 → v0.9.4

Round 4 put the whole codebase under an adversarial audit swarm: **13 auditors**, one per
area, filed findings into `audit_briefs/*.txt`; **12 parallel fixers** resolved them. The
pytest suite grew from 148 tests to **3154 collected** (`pytest tests/ --collect-only`),
all pure core, still runnable on any OS with no Max install.

## Findings by severity (as retained in `audit_briefs/`)

| Severity | Count | Meaning |
|---|---|---|
| P0 | 7 | crash / corruption class — fixed first |
| P1 | 32 | wrong-result / guarantee-breaking class |
| P2 | 74 | robustness, hygiene, docs |
| **Total** | **113** | across 13 area briefs (A–M) |

## What was fixed, per area

- **A · session/genome (11)** — corrupt sidecar/preset JSON no longer crashes session
  load; genome/session validation hardened.
- **B · codecs/metrics (7)** — P0: `png_min` unbounded `zlib.decompress` is now
  dimension/size-gated before inflate; stats codecs hardened against hostile files.
- **C · solver/critic/rules/feedback (10)** — malformed inputs raise typed errors instead
  of uncaught TypeError/IndexError across the math leg.
- **D · domeseed/scenarios (3)** — cached semantics validated: NaN/Infinity can no longer
  reach a scenario state.
- **E · wire/parse/planner (5)** — `omega.extract_text` survives malformed 200 payloads;
  plan parsing/validation tightened.
- **F · director (7)** — a hook exception mid-loop can no longer abandon the keep-best
  guarantee.
- **G · scene/digest/config (10)** — P0: non-dict-but-valid JSON in `config.json` no
  longer crashes; scene/digest introspection hardened.
- **H · controller/api (17)** — seed-only flow is restorable again (`restore_pre_match`
  gating fixed); largest brief of the round.
- **I · apply/exposure/draft (7)** — P0-class ordering fix: draft sampler writes the
  crash-safe snapshot **before** mutating renderer props.
- **J · render/vantage/execute (10)** — stale output from a previous batch is rejected,
  not accepted as a fresh render/export.
- **K · dock/startup (13)** — 2 P0s in UI/startup paths; workers catch `BaseException`,
  not just `Exception`.
- **L · scripts (9)** — installer/preflight robustness (e.g. missing `errorlevel`
  checks).
- **M · docs (4)** — this round's documentation alignment: version drift, stale test
  counts, SPEC §3 module tree, Phase-B benchmark numbers.

## Environmental caveat — on-box verification status

Live on-box verification was blocked by a **pre-existing 3ds Max startup crash on the
audit box**: StackHash crashes inside Windows modules during Max's *own* startup-script
phase, reproducing with **zero user scripts** installed and predating the audit per
Windows Error Reporting records from 2026-07-18. This is a box/Max-install fault, not a
MaxGaffer fault. Crash-safety was therefore verified by:

1. **static audit** of every pymxs-touching path (the swarm's P0/P1 crash-class fixes),
2. a **hostile-mock pymxs harness** exercising failure injection off-box, and
3. the full **pytest suite under Max's real Python 3.11**.

`scripts/kimi_onbox_test.py` ships as the on-box harness — run it on a healthy Max 2026
box to complete live bring-up (see SPEC §9 C0 and the on-box checklist in README).

**Update 2026-07-19 — on-box run completed.** The startup crash is intermittent: a
headless `3dsmaxbatch scripts/kimi_onbox_runner.ms` run on the same box completed
cleanly and the harness reported **13/13 steps PASS** under Max's real Python 3.11 +
pymxs (`scripts/kimi_onbox_report.json`). The harness's vantage step now skips
`start_*`/`launch_*` entry points — on a V-Ray-equipped box the old reflection loop
would have genuinely toggled the Vantage live link. The same pass folded the measured
2026-07-16 on-box fixes into this tree (they had been stranded, uncommitted, in the
sibling clone at `C:\Users\user\3DSL`): Physical-Camera exposure-control host
preferred for native Physical cameras (V-Ray honors camera EV/WB only through it) ·
VRay EC **mode-106 enforcement** on create and on every `write_ev` (its `.ev` is
silently ignored in the default mode 107) · viewport-camera fallback for camera-less
`ExposureHost` callers · alpha-flattened loop renders (`pngio.setAlpha false` — the
LLM was seeing transparent skies) · untargeted/dead-target VRaySun rig note ·
underscore-tolerant preflight renderer check. Porting also fixed the audit's pinned
`_BUG4` (unguarded `lt.name` in duplicate-sun/dome notes — now `_node_name`-guarded;
the xfail converted to a passing regression test).
