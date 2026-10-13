"""Synthetic CPU 'training': writes a model whose quality is method gain (no GPU, deterministic)."""
import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--method-dir", required=True)
ap.add_argument("--seed", type=int, required=True)
ap.add_argument("--kind", required=True)
ap.add_argument("--steps", type=int, required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
m = json.loads((Path(a.method_dir) / "method.json").read_text())
gain = float(m.get("gain", 0.0))
if m.get("dev_screen_only") and a.kind not in ("dev", "screen"):
    gain = 0.0  # overfits to exploration seeds: no real improvement on formal seeds
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
(out / "model.json").write_text(json.dumps({"gain": gain, "seed": a.seed, "steps": a.steps}))
print(f"trained seed={a.seed} kind={a.kind} steps={a.steps} gain={gain}")
