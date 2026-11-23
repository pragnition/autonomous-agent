#!/usr/bin/env bash
# Spike: start N containers from the agent image on Jensen's Docker network; print start time and idle memory.
set -euo pipefail
IMG=${IMG:-autonomous-agent-agent}
N=${N:-5}
names=()
for i in $(seq 1 "$N"); do
  t0=$(date +%s%N)
  docker run -d --name "spike-ct-$i" --network container:agent-docker --entrypoint sleep "$IMG" infinity >/dev/null
  t1=$(date +%s%N)
  echo "spike-ct-$i started in $(( (t1 - t0) / 1000000 )) ms"
  names+=("spike-ct-$i")
done
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "${names[@]}"
