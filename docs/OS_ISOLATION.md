# OS isolation for the local MCP server

The Switch agent runs with model instructions and, in Console Auto mode,
without a human approval gate on every action. instructions mitigate, but
they do not constrain a misbehaving stack. This document describes the last
layer: the MCP server runs as a credentialless, network-less, immutable
container that the agent process cannot escape by prompting.

Status: **prototype proven on this machine** (`deploy/mcp-container/`).
Console migration: point each Console agent's MCP command at the hardened
container invocation below.

## Threat model

Without OS isolation an agent can:

- read `.data/` evidence and reports and alter them silently;
- call Switch agents directly with the operator's registered credentials;
- reach the local Kubernetes reader RBAC profile (read) and anything the
  operator can reach (write) if it shells out;
- persist files for later exfiltration.

With the isolation profile (`deploy/mcp-container/verify.sh`), the MCP
process can only:

- execute its own code (read-only bind), and
- answer MCP tool calls over stdin/stdout.

It cannot write outside two small tmpfs mounts (`/tmp`, `/data`, both
`noexec,nosuid`, sized in megabytes, both wiped when the container exits),
cannot open a socket, holds no Linux capabilities, and its uid (10001) owns
nothing on the host. Crash/restart orchestration and room-spawning stay
Console-owned; in this profile there is nothing to restart because the
container is stateless by construction.

## Hardened invocation

```sh
docker build --quiet -f deploy/mcp-container/Dockerfile \
  -t kube-investigator-mcp:isolated .

docker run --rm \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --network none \
  --pids-limit 64 \
  --memory 512m \
  --tmpfs /tmp:rw,size=16m,noexec,nosuid \
  --tmpfs /data:rw,size=64m,noexec,nosuid \
  kube-investigator-mcp:isolated python -m kube_investigator.mcp_server
```

| Flag | What it buys |
| --- | --- |
| `--read-only` | program code, site-packages, and `/etc` are immutable |
| `--cap-drop ALL` | zero Linux capabilities: no chown, no raw sockets, no mount |
| `--security-opt no-new-privileges` | setuid-style escape is dead |
| `--network none` | no cluster, no Switch API, no internet, no DNS |
| `--pids-limit 64` | fork bombs die long before the machine notices |
| `--memory 512m` | allocator abuse is bounded |
| `--tmpfs /tmp,/data (noexec,nosuid)` | scratch space with no code execution |
| `USER 10001` (Dockerfile) | non-root, no shell login, no supplementary groups |

## Verify (fail closed)

```sh
bash deploy/mcp-container/verify.sh
```

The gate builds the image and runs four probes inside the hardened
container; each must print `PASS`, or the script exits nonzero:

```
PASS uid-isolation          # os.getuid() != 0
PASS filesystem-isolation   # /app, /usr/local/lib, /etc, /root reject writes
PASS network-isolation      # TCP connect + DNS/HTTP fail from inside
PASS mcp-stdio-handshake    # initialize -> tools/list -> memory call answers
```

Recorded evidence (2026-11-05, Docker Desktop 29.6.2, image
`sha256:d12eaf56…`):

```
PASS uid-isolation
PASS filesystem-isolation
PASS network-isolation
PASS mcp-stdio-handshake
ISOLATION-OK all probes passed
```

`pytest tests/test_container_isolation.py` runs the same gate when a docker
server is reachable and skips cleanly elsewhere (CI has no docker).

## Combining with actor tokens

OS isolation bounds *what the process can do*; Ed25519 actor tokens
(`docs` in `src/kube_investigator/mcp_auth.py`, wiring via
`switch console setup --actor-tokens`) bound *who is being seen by the
audit log*. The layers compose: Console agents keep their signed
`INVESTIGATOR_MCP_ACTOR_TOKEN` environment; the containerized profile keeps
`.data` on a private tmpfs, so audited attribution follows the same
principle of least exposure.

## Known limits (honest)

- The prototype is for **local Docker on this machine**. Console migration
  needs the agent definition's MCP command to match the run flags, or an
  agent-side supervisor to start it; neither is wired yet.
- `--network none` means the container's `kube_investigator_run` cannot
  reach a live Switch or cluster: in this profile it fails closed by
  design. Evidence-based tools (`get`, `verify`, `memory`,
  `source_status`, `health`) work.
- Docker Desktop on macOS enforces the namespace controls inside the VM;
  the host-level filesystem bounds are Docker's, not a macOS sandbox. A
  Linux deployment with a real gVisor/Kata runtime would raise the floor.

## Live evidence ledger

| Probe | Evidence |
| --- | --- |
| uid | `PASS uid-isolation` (uid=10001, euid=uid) |
| filesystem | `PASS filesystem-isolation` (write probes rejected on 4 paths) |
| network | `PASS network-isolation` (2 TCP probes + HTTP probe rejected) |
| stdio | `PASS mcp-stdio-handshake` (initialize, tools/list, memory ok=true) |
| gate | `ISOLATION-OK all probes passed` and `ISOLATION-OK` from verify.sh |
