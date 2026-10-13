#!/usr/bin/env bash
# P2 extension check: mock-ExtensionAPI harness over the real Python CLI, both terminal paths. CPU only.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
EX="$REPO/tools/g03_conductor/examples"
for pair in done:good stop:overfit; do
  scen=${pair%%:*}; method=${pair##*:}
  W=$(mktemp -d /tmp/g03-ext-XXXX)
  python3 -I "$EX/dry_run.py" --scenario "$scen" --workdir "$W" --init-only >/dev/null
  node "$REPO/extensions/g03-conductor/test/harness.mjs" "$W/private" "$W/protocol.yaml" "$EX/fake_task/methods" "$method" > "$W/out.json"
  python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]));t=d['terminal'];print(sys.argv[2], t['status'], t['reason'], d['checks'])" "$W/out.json" "$scen"
  chmod -R u+w "$W" && rm -rf "$W"   # frozen/ is read-only by design
done
