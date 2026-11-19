#!/bin/sh
# Applies the reference split-change solution as two separate commits, run with the run dir
# (the already-built fixture's git repo) as the current directory.
set -eu
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

cp "$here/step1/orders.py" orders.py
cp "$here/step1/test_orders.py" test_orders.py
git add -A
git commit -qm "rename calc_total to order_total"

cp "$here/step2/orders.py" orders.py
cp "$here/step2/test_orders.py" test_orders.py
git add -A
git commit -qm "round discount() to the nearest cent"
