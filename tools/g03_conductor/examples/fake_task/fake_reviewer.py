"""Fake reviewer for dry runs: echoes PASS (or $G03_FAKE_VERDICT) in pi --mode json event format."""
import json
import os
import re
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text()
m = re.search(r'G03_VERDICT: \{"gate": "([^"]+)", "subject": "([^"]+)"', prompt)
verdict = os.environ.get("G03_FAKE_VERDICT", "PASS")
defect = os.environ.get("G03_FAKE_DEFECT", "none")
line = json.dumps({"gate": m.group(1), "subject": m.group(2), "verdict": verdict, "defect": defect}, ensure_ascii=False)
text = f"## Verdict\n{verdict}\n## Evidence Checked\n(dry run)\nG03_VERDICT: {line}"
print(json.dumps({"type": "message_end", "message": {"role": "assistant", "model": "fake/reviewer",
                                                     "content": [{"type": "text", "text": text}]}}))
