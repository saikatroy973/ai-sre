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

python3 - "$STAGE/demo/investigator.sqlite3" "$EVIDENCE" <<'PY'
"""Stage a read-only evidence snapshot and the probe env for verify.sh.

The snapshot is converted out of WAL journal mode because a read-only SQLite
connection cannot create the -shm file it needs on an immutable mount. This
staging script is host-side only and never writes to the source store.
"""
import json
import sys
from pathlib import Path

import sqlite3

source_path, evidence_dir = Path(sys.argv[1]), Path(sys.argv[2])
snapshot = evidence_dir / "investigator.sqlite3"
if not source_path.is_file():
    sys.exit(f"GATE FAILURE: no evidence store at {source_path}")

source = sqlite3.connect(f"file:{source_path.absolute()}?mode=ro", uri=True)
target = sqlite3.connect(snapshot)
source.backup(target)
# Keep the staged snapshot shared-friendly for mode=ro opens on ro mounts.
journal = str(target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
if journal != "delete":
    sys.exit(f"GATE FAILURE: staged snapshot stayed in journal mode {journal}")

row = source.execute(
    "SELECT id, state, record FROM investigations WHERE state = ? ORDER BY rowid DESC LIMIT 1",
    ("reported",),
).fetchone()
target.close()
source.close()
if row is None:
    sys.exit(
        "GATE FAILURE: the staging demo produced no reported investigation; "
        "the evidence probe refuses to guess"
    )
snapshot.chmod(0o444)

record = json.loads(row[2])
scope = record["scope"]
report = record["report"] or {}
diagnosis = report.get("diagnosis")
principal = record.get("principal_id")
if principal != "local-operator" or not diagnosis:
    sys.exit(
        f"GATE FAILURE: staged investigation has principal={principal!r} "
        f"diagnosis={diagnosis!r}; the probe refuses it"
    )

env_file = evidence_dir.parent / "probe.env"
allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ@._:-"
values = {
    "INVESTIGATOR_EVIDENCE_PROBE_ID": str(row[0]),
    "INVESTIGATOR_EVIDENCE_PROBE_DIAGNOSIS": str(diagnosis),
    "INVESTIGATOR_TENANT": str(scope["tenant"]),
    "INVESTIGATOR_CLUSTER": str(scope["cluster"]),
    "INVESTIGATOR_NAMESPACES": str(scope["namespace"]),
}
for key, value in values.items():
    if not value or any(char not in allowed for char in value):
        sys.exit(f"GATE FAILURE: refusing unsafe staged env value for {key}")
env_file.write_text("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
PY

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
