"""These folders hold reference *solutions* overlaid onto a yardstick fixture by
tests/test_yardstick_seeds.py -- never tests pytest should collect and run on its own (several
share a basename like test_orders.py, which also collides across step1/step2)."""
from __future__ import annotations

collect_ignore_glob = ["*"]
