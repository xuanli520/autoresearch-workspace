"""Synthetic locked scorer: metric = 0.5 + gain + N(0, 0.005) noise determined only by the seed."""
import argparse
import hashlib
import json
import math
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--checkpoint", required=True)
ap.add_argument("--data", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
model = json.loads(Path(a.checkpoint).read_text())
assert Path(a.data).read_text().startswith("synthetic eval set")
h = hashlib.sha256(str(model["seed"]).encode()).digest()
u1 = (int.from_bytes(h[:8], "big") + 1) / (2**64 + 1)
u2 = int.from_bytes(h[8:16], "big") / 2**64
noise = 0.005 * math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
metric = 0.5 + model["gain"] + noise
Path(a.out).write_text(json.dumps({"metric": metric, "psnr_db": 30.0 + model["gain"], "ssim": 0.9}))
print(f"metric={metric}")
