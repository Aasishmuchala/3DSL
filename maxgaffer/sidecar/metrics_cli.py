"""Optional stats sidecar — run core.metrics under a *system* Python that has Pillow.

Max's Python computes stats fine for its own PNG renders via the stdlib reader, but JPEG/WEBP
reference images need Pillow. If the bridge's Max-side transcode isn't available either, the
UI shells out here:  ``python -m maxgaffer.sidecar.metrics_cli IMAGE [IMAGE2]``
→ one JSON object per image on stdout (``null`` for unreadable), plus a ``b64`` downscaled
JPEG when ``--b64`` is passed (for LLM payload slimming).
"""

from __future__ import annotations

import base64
import io
import json
import sys


def _b64_jpeg(path: str, max_dim: int = 768) -> str:
    from PIL import Image  # sidecar python must have Pillow

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    want_b64 = "--b64" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(json.dumps({"error": "usage: metrics_cli IMAGE [IMAGE...] [--b64]"}))
        return 2
    sys.path.insert(0, __file__.rsplit("maxgaffer", 1)[0])
    from maxgaffer.core import metrics

    out = []
    for p in paths:
        entry = {"path": p, "stats": metrics.compute_stats(p)}
        if want_b64:
            try:
                entry["b64"] = _b64_jpeg(p)
                entry["media_type"] = "image/jpeg"
            except Exception as e:  # noqa: BLE001
                entry["b64_error"] = str(e)
        out.append(entry)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
