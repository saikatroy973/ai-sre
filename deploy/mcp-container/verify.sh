#!/usr/bin/env bash
# Build the isolated MCP image, run it under full hardening, and gate on the
# five in-container probes. The fifth probe reads a real completed
# investigation from a staged read-only snapshot, proving evidence can be
# served in isolation. Fails closed: every probe must print PASS.
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE="${1:-kube-investigator-mcp:isolated}"
DOCKERFILE="${DOCKERFILE:-deploy/mcp-container/Dockerfile}"

# The staging demo needs the installed console script; CI installs it with
# pip -e, local runs usually have .venv next to the repo.
DEMO_BIN=""
for candidate in kube-investigator "$PWD/.venv/bin/kube-investigator"; do
  resolved="$(command -v "$candidate" 2>/dev/null || true)"
  if [ -n "$resolved" ]; then
    DEMO_BIN="$resolved"
    break
  fi
done
if [ -z "$DEMO_BIN" ]; then
  echo "GATE FAILURE: install the package (pip install -e '.[dev,kubernetes]') so the staging demo can run" >&2
  exit 1
fi

# Staging lives under the repo (Docker Desktop shares /Users; it does not
# share /var/folders) and inside the gitignored .data/ tree.
STAGE="$(pwd)/.data/isolation-evidence"
EVIDENCE="$STAGE/evidence"
trap 'rm -rf "$STAGE"' EXIT
rm -rf "$STAGE"
mkdir -p "$STAGE/demo" "$EVIDENCE"

# A deterministic credential-free investigation becomes the staged evidence.
INVESTIGATOR_DATA_DIR="$STAGE/demo" INVESTIGATOR_SWITCH_MODE=off \
  "$DEMO_BIN" demo --json >/dev/null || {
  echo "GATE FAILURE: the staging demo investigation failed" >&2
  exit 1
}

python3 deploy/mcp-container/stage_evidence.py \
  "$STAGE/demo/investigator.sqlite3" "$EVIDENCE"

docker build --quiet -f "$DOCKERFILE" -t "$IMAGE" .

PROBES=$(docker run --rm \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --network none \
  --pids-limit 64 \
  --memory 512m \
  --tmpfs /tmp:rw,size=16m,noexec,nosuid \
  --tmpfs /data:rw,size=64m,noexec,nosuid \
  --env-file "$STAGE/probe.env" \
  -v "$EVIDENCE":/evidence:ro \
  "$IMAGE" python /app/verify_runtime.py)

echo "$PROBES"

for probe in uid-isolation filesystem-isolation network-isolation mcp-stdio-handshake evidence-store; do
  echo "$PROBES" | grep -q "^PASS ${probe}$" || {
    echo "GATE FAILURE: probe ${probe} did not pass" >&2
    exit 1
  }
done

echo "ISOLATION-OK all probes passed"
