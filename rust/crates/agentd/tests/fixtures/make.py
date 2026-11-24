"""Expected outputs from the Python core, for agentd's tests. Run from rust/crates/agentd with PYTHONPATH=<repo>/src."""
import dataclasses
import json
import sqlite3
import tempfile
from pathlib import Path

from agent import db
from agent.config import ConfigError, load_agent_config, load_packs

out = {}
for case in ["../../../examples/config/agent.toml", "tests/fixtures/config/bad_fields.toml",
             "tests/fixtures/config/no_people.toml", "tests/fixtures/config/invalid.toml",
             "tests/fixtures/config/missing.toml"]:
    try:
        cfg = load_agent_config(Path(case))
        out[case] = {"config": json.loads(json.dumps(dataclasses.asdict(cfg)))}
    except ConfigError as e:
        out[case] = {"problems": e.problems}
for case in ["tests/fixtures/config/packs-bad", "../../../examples/config"]:
    try:
        agent = load_agent_config(Path(case) / "agent.toml")
        out[case + "/packs"] = {"packs": sorted(load_packs(Path(case), agent))}
    except ConfigError as e:
        out[case + "/packs"] = {"problems": e.problems}
Path("tests/fixtures/config/expected.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")

with tempfile.TemporaryDirectory() as d:
    conn = db.connect(Path(d) / "a.db")
    db.create_schema(conn)
    lines = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        for row in conn.execute(f"PRAGMA table_info({name})"):
            lines.append("|".join(["table", name, *(str(v) for v in tuple(row))]))
    for name, table in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        lines.append(f"index|{name}|{table}")
    Path("tests/fixtures/core_schema.txt").write_text("\n".join(lines) + "\n")
    Path("src/schema.sql").write_text(db.SCHEMA.strip() + "\n")
