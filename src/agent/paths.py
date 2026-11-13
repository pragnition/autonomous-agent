"""Where everything lives inside the containers. Overridable through the environment for tests and dev."""

from __future__ import annotations

import os
from pathlib import Path

DATA = Path(os.environ.get("AGENT_DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("AGENT_CONFIG_DIR", DATA / "config"))
DB_DIR = DATA / "db"
SECRETS_FILE = Path(os.environ.get("AGENT_SECRETS_FILE", "/run/agent/secrets.env"))
