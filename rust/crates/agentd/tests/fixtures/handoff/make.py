"""Expected handoff notes from the Python runner. Run from rust/crates/agentd with PYTHONPATH=<repo>/src."""
import json
from pathlib import Path

from agent.runner.handoff import handoff_note

base = "tests/fixtures/handoff"
cases = [["S1", f"{base}/claude", f"{base}/transcript.jsonl"], ["S2", f"{base}/claude", None],
         ["S2", f"{base}/claude", f"{base}/transcript.jsonl"], ["S3", f"{base}/claude", None], ["S4", None, None]]
out = [{"case": c, "note": handoff_note(*c)} for c in cases]
Path(f"{base}/expected.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
