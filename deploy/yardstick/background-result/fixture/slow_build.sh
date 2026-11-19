#!/bin/sh
# Takes about 3 minutes in a real run. Set YARDSTICK_FAST=1 to skip the wait for tests -- the
# checksum it writes never depends on how long the wait was.
set -eu

seconds=180
if [ "${YARDSTICK_FAST:-}" = "1" ]; then
    seconds=0
fi

python3 -c "import sys, time; time.sleep(float(sys.argv[1]))" "$seconds"

mkdir -p out
python3 -c "
import hashlib

with open('data/input.txt', 'rb') as f:
    digest = hashlib.sha256(f.read()).hexdigest()
print(digest)
" > out/checksum.txt
