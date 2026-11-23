"""Expected transcript records: each input frame through the Python SDK's parser and the runner's _append."""
import dataclasses
import json
from pathlib import Path

from claude_agent_sdk._internal.message_parser import parse_message

here = Path(__file__).parent
out = []
for line in (here / "input.jsonl").read_text().splitlines():
    msg = parse_message(json.loads(line))
    if msg is None:
        out.append("null")
        continue
    record = {"type": type(msg).__name__, **(dataclasses.asdict(msg) if dataclasses.is_dataclass(msg) else {"repr": repr(msg)})}
    out.append(json.dumps(json.loads(json.dumps(record, default=str)), sort_keys=True))
(here / "expected.jsonl").write_text("\n".join(out) + "\n")
