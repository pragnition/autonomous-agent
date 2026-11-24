"""The tool catalogue as core registers it (names, descriptions, input schemas, tiers), for agentd's src/tools.json.
Run from rust/crates/agentd with PYTHONPATH=<repo>/src. The device-code login tools are made per CLI at runtime, so
they aren't in it."""
import json
from pathlib import Path

from agent import db
from agent.config import load_agent_config, load_packs
from agent.core.agent_tools import AgentToolRouter
from agent.core.kpis import register_kpi_tools
from agent.core.linear_app import register_linear_tools
from agent.core.parking import Parking, register_parking_tools
from agent.core.projects import register_project_tools
from agent.core.tracker_tools import register_tracker_tools

config_dir = Path("../../../examples/config")
config = load_agent_config(config_dir / "agent.toml")
conn = db.connect(":memory:")
db.create_schema(conn)
router = AgentToolRouter(conn, config=config, packs=load_packs(config_dir, config), queue=None, slack=None,
                         notifier=None, secret_values=[])
register_tracker_tools(router, reader=None)
register_kpi_tools(router)
register_project_tools(router)
register_linear_tools(router, auth=None)
register_parking_tools(router, Parking(conn, None, resume=None))
tools = [{"name": s.name, "description": s.description, "input_schema": s.input_schema, "tier": s.tier}
         for s, _ in router._tools.values()]
Path("src/tools.json").write_text(json.dumps(tools, indent=1, ensure_ascii=False) + "\n")
