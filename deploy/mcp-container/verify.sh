#!/usr/bin/env bash
# Build the isolated MCP image, run it under full hardening, and gate on the
# four in-container probes. Fails closed: every probe must print PASS.
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE="${1:-kube-investigator-mcp:isolated}"

docker build --quiet -f deploy/mcp-container/Dockerfile -t "$IMAGE" .

PROBES=$(docker run --rm \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --network none \
  --pids-limit 64 \
  --memory 512m \
  --tmpfs /tmp:rw,size=16m,noexec,nosuid \
  --tmpfs /data:rw,size=64m,noexec,nosuid \
  "$IMAGE" python /app/verify_runtime.py)

echo "$PROBES"

for probe in uid-isolation filesystem-isolation network-isolation mcp-stdio-handshake; do
  echo "$PROBES" | grep -q "^PASS ${probe}$" || {
    echo "GATE FAILURE: probe ${probe} did not pass" >&2
    exit 1
  }
done

echo "ISOLATION-OK all probes passed"
