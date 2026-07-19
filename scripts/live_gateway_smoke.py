"""LIVE gateway smoke — fire MaxGaffer's three real prompts at the real Omega gateway.

Run off-Max (dev Mac or the box):  python scripts/live_gateway_smoke.py [oc_key]
Key discovery order: argv → MAXGAFFER_KEY env → MaxGaffer config → MaxDirector config →
~/.hermes/config.yaml. The key is never printed.

What a full PASS retires: wire format, auth, vision blocks, ANALYZE/DELTAS/SWEEP prompt
contracts, and strict JSON parsing — the entire LLM leg of the pipeline, leaving only
pymxs property names for the on-box spikes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maxgaffer.core import omega, parse, prompts  # noqa: E402
from maxgaffer.core.genome import LightingState, state_table  # noqa: E402


def discover_key() -> str:
    if len(sys.argv) > 1 and sys.argv[1].startswith("oc_"):
        return sys.argv[1]
    if os.environ.get("MAXGAFFER_KEY", "").startswith("oc_"):
        return os.environ["MAXGAFFER_KEY"]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    for app in ("MaxGaffer", "MaxDirector"):
        try:
            with open(os.path.join(base, app, "config.json"), encoding="utf-8") as f:
                k = json.load(f).get("api_key") or ""
            if k.startswith("oc_"):
                return k
        except (OSError, ValueError):
            pass
    try:
        text = open(os.path.expanduser("~/.hermes/config.yaml"), encoding="utf-8").read()
        m = re.search(r"(?:api_key|key)\s*:\s*[\"']?(oc_[A-Za-z0-9_\-\.]+)", text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return ""


def make_images(tmp: str):
    """Synthetic but legible test frames: a golden-hour 'reference' (warm, sun lobe low
    right), a cool under-exposed 'render', and three sweep candidates (lobe left/center/
    right). PIL required (dev machines have it; on the box use Max transcode paths)."""
    from PIL import Image, ImageDraw

    def frame(path, base, sun_xy=None, sun_r=28, warm=0):
        im = Image.new("RGB", (320, 180))
        px = im.load()
        for y in range(180):
            t = y / 179.0
            r = int(min(255, base[0] * (1 - 0.55 * t) + warm * (1 - t)))
            g = int(min(255, base[1] * (1 - 0.6 * t) + warm * 0.55 * (1 - t)))
            b = int(min(255, base[2] * (1 - 0.65 * t)))
            for x in range(320):
                px[x, y] = (r, g, b)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 130, 320, 180], fill=(int(base[0] * 0.25), int(base[1] * 0.22),
                                              int(base[2] * 0.2)))          # ground
        d.rectangle([200, 70, 265, 130], fill=(int(base[0] * 0.35), int(base[1] * 0.3),
                                               int(base[2] * 0.28)))        # building
        if sun_xy:
            for rr, a in ((sun_r * 3, 30), (sun_r * 2, 70), (sun_r, 255)):
                d.ellipse([sun_xy[0] - rr, sun_xy[1] - rr, sun_xy[0] + rr, sun_xy[1] + rr],
                          fill=None, outline=None, width=0)
            d.ellipse([sun_xy[0] - sun_r, sun_xy[1] - sun_r,
                       sun_xy[0] + sun_r, sun_xy[1] + sun_r], fill=(255, 236, 200))
        im.save(path)
        return path

    ref = frame(os.path.join(tmp, "ref.png"), (235, 175, 110), sun_xy=(60, 108), warm=40)
    cur = frame(os.path.join(tmp, "cur.png"), (70, 82, 110), sun_xy=(250, 40))
    sweeps = [frame(os.path.join(tmp, f"sw{i}.png"), (200, 170, 130), sun_xy=xy)
              for i, xy in enumerate(((50, 100), (160, 45), (280, 100)))]
    return ref, cur, sweeps


def block(path):
    b = omega.image_block_from_file(path)
    if b is None:
        raise RuntimeError(f"could not build image block for {path}")
    return b


def main() -> int:
    key = discover_key()
    if not key:
        print("[!!] no oc_ key found (argv/env/configs/hermes) — cannot smoke the gateway")
        return 2
    print(f"key: oc_…(redacted) · model: {omega.DEFAULT_MODEL} · {omega.GATEWAY_URL}")
    results = {}

    # ---- 0 ping
    try:
        print("\n[ping]", omega.ping(key))
        results["ping"] = "PASS"
    except omega.OmegaError as e:
        print(f"[ping] FAIL — {e} (kind={e.kind})")
        return 1

    tmp = tempfile.mkdtemp(prefix="maxgaffer_smoke_")
    ref, cur, sweeps = make_images(tmp)
    print(f"synthetic frames → {tmp}")

    # ---- 1 ANALYZE
    try:
        reply = omega.call(key, prompts.ANALYZE_SYSTEM,
                           [{"role": "user", "content": [
                               block(ref), omega.text_block(prompts.analyze_user_text())]}],
                           max_tokens=2048)
        sem = parse.validate_analysis(reply)
        print("\n[analyze] PASS —",
              json.dumps({k: sem[k] for k in ("time_of_day", "sky", "sun_bearing_deg",
                                              "sun_altitude_band", "wb_kelvin_estimate",
                                              "confidence")}))
        results["analyze"] = "PASS"
    except (omega.OmegaError, parse.ParseError) as e:
        print(f"\n[analyze] FAIL — {e}")
        results["analyze"] = f"FAIL {e}"
        sem = {"time_of_day": "golden_hour"}

    # ---- 2 DELTAS
    st = LightingState()
    for k, v in {"sun.enabled": 1, "sun.azimuth_deg": 40.0, "sun.altitude_deg": 55.0,
                 "sun.intensity": 1.0, "sun.size": 1.0, "sun.turbidity": 3.0,
                 "exposure.ev": 14.0, "exposure.wb_kelvin": 7500.0}.items():
        st.set(k, v)
    try:
        text = prompts.deltas_user_text(state_table(st), sem, [(0, 41.0)],
                                        {"exposure.ev": 12.6}, 1, 5)
        reply = omega.call(key, prompts.DELTAS_SYSTEM,
                           [{"role": "user", "content": [
                               block(ref), block(cur), omega.text_block(text)]}],
                           max_tokens=2048)
        deltas = parse.validate_deltas(reply)
        print("[deltas ] PASS —", json.dumps(deltas["changes"]),
              f"stop={deltas['stop']} · “{deltas['assessment'][:80]}”")
        analytic_touched = [k for k in deltas["changes"] if k.startswith("exposure.")]
        if analytic_touched:
            print(f"          note: model proposed ANALYTIC params {analytic_touched} — "
                  "genome would refuse these (guard working as designed)")
        results["deltas"] = "PASS"
    except (omega.OmegaError, parse.ParseError) as e:
        print(f"[deltas ] FAIL — {e}")
        results["deltas"] = f"FAIL {e}"

    # ---- 3 SWEEP (ref sun is LEFT-low → correct pick is candidate 0)
    try:
        content = [block(ref)] + [block(p) for p in sweeps]
        content.append(omega.text_block(prompts.sweep_user_text([90.0, 180.0, 270.0])))
        reply = omega.call(key, prompts.SWEEP_SYSTEM,
                           [{"role": "user", "content": content}], max_tokens=1024)
        pick = parse.validate_sweep(reply, 3)
        verdict = "correct" if pick["best_index"] == 0 else f"index {pick['best_index']}"
        print(f"[sweep  ] PASS — picked candidate {pick['best_index']} ({verdict}), "
              f"altitude '{pick['altitude_hint']}' — “{pick['why']}”")
        results["sweep"] = f"PASS ({verdict})"
    except (omega.OmegaError, parse.ParseError) as e:
        print(f"[sweep  ] FAIL — {e}")
        results["sweep"] = f"FAIL {e}"

    print("\n=== smoke:", " · ".join(f"{k}={v}" for k, v in results.items()), "===")
    return 0 if all(str(v).startswith("PASS") for v in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
