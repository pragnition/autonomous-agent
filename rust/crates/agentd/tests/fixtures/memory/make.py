"""Expected memory blocks from the Python runner. Run from rust/crates/agentd with PYTHONPATH=<repo>/src."""
import json
from pathlib import Path

from agent.runner.worker import memory_block

cases = {f"{d}|{n}": memory_block(Path(d), n) for d, n in [("tests/fixtures/memory", 200), ("tests/fixtures/memory", 500),
                                                            ("tests/fixtures/memory/none", 200)]}
Path("tests/fixtures/memory/expected.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
