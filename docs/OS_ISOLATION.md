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

The gate stages its own evidence first: it runs a credential-free demo
investigation into a throwaway `.data/isolation-evidence/` directory, cuts a
read-only SQLite snapshot of the evidence store (`VACUUM`-safe backup,
journal mode converted out of WAL because a read-only connection cannot
create the `-shm` file it needs on an immutable mount, file mode 0444), and
binds it into the container at `/evidence:ro` with the investigation id and
scope in the environment.

It then builds the image and runs five probes inside the hardened container;
each must print `PASS`, or the script exits nonzero:

```
PASS uid-isolation          # os.getuid() != 0
PASS filesystem-isolation   # /app, /usr/local/lib, /etc, /root reject writes
PASS network-isolation      # TCP connect + DNS/HTTP fail from inside
PASS mcp-stdio-handshake    # initialize -> tools/list -> memory call answers
PASS evidence-store         # get -> real staged report; verify -> re-passes; write to /evidence rejected
```

Recorded evidence (2026-11-05, CI isolation job, image
`sha256:79c0a5a5…`; an earlier local Docker Desktop run passed the same
five probes):

```
PASS uid-isolation
PASS filesystem-isolation
PASS network-isolation
PASS mcp-stdio-handshake
PASS evidence-store
ISOLATION-OK all probes passed
```

The same gate runs as an `isolation` CI job in `.github/workflows/test.yml`
(ubuntu runners have Docker) and `pytest tests/test_container_isolation.py`
runs it again when a docker server is reachable, skipping elsewhere.

## Evidence reads are structurally read-only

The MCP evidence tools open the store with
`Store(..., readonly=True)`: URI mode `ro`, so the child process cannot
create, migrate, upgrade, or write the database it reads — a write attempt
fails with `attempt to write a readonly database`, and a missing store is a
clean, honest refusal instead of a silent reparent. `INVESTIGATOR_EVIDENCE_STORE_DIR`
points the tools at a staged snapshot when `data_dir` is not the mounted
one. Memory search and the other MCP tools keep a writable tmpfs data
directory for their rotating, hash-chained audit trails; the audit rows name
the verified actor and never go into the evidence database.

## Combining with actor tokens

OS isolation bounds *what the process can do*; Ed25519 actor tokens
(`src/kube_investigator/mcp_auth.py`, wiring via
`switch console setup --actor-tokens`) are intended to distinguish *who is
being seen*. Every MCP tool except `health` requires a valid signed token when
a public key is configured; `health` stays open because it is a liveness probe.

That distinction is only a credential, not a same-uid isolation boundary. In
the local Console profile all six agents run as the operator account, so any
one of them can read another agent's mode-0600 config and the shared signing
key under `.data/mcp-auth`, then mint or replay another actor's token and forge
the audit attribution. The containerized profile isolates the MCP *server* and
keeps its `/data` on a private tmpfs, but Console still launches the MCP server
outside that container and the parent agents still share the operator uid.
Treat actor tokens as credential hygiene until token signing moves behind a
service with independent approval/identity checks, or each agent gets a
separate uid/container.

## Known limits (honest)

- The prototype is for **local Docker on this machine**. Console migration
  needs the agent definition's MCP command to match the run flags, or an
  agent-side supervisor to start it; neither is wired yet.
- `--network none` means the container's `kube_investigator_run` cannot
  reach a live Switch or cluster: in this profile a Kubernetes-backed run
  fails closed by design (the fixture backend still works offline). Reads
  of real evidence go through the staged read-only snapshot (`evidence-store`
  probe);fresh evidence collection from this profile is not possible.
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
| evidence | `PASS evidence-store` (staged get ok, verify re-passed, mount write rejected) |
| gate | `ISOLATION-OK all probes passed` and `ISOLATION-OK` from verify.sh |
