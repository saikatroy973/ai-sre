# Showcase: the Kubernetes Investigator on Switch Console

Video walkthrough: [`docs/media/showcase.mp4`](media/showcase.mp4) (2:15, narrated) —
Console, the self-hosted Switch stack, memory, the Trust CLI, Trust/ROI, audit,
credential contract, least privilege and the isolation gate.
Regenerate it with `pip install -e '.[video]'` then
`python scripts/make_showcase_video.py` (macOS `say` provides the narration).

For the full surface tour — Switch Trust, ROI, the Trust CLI/CodeScan, memory,
the self-hosted room stack and the MCP tool surface — see
[`SWITCH_TOUR.md`](SWITCH_TOUR.md).

A complete, ~8-minute live demo. Everything runs through **Switch Console**:
six Console-managed agents, one Switch room per incident, read-only evidence over
MCP, and a hash-chained, actor-attributed audit trail.

Verified live on 2026-11-05 (see [Verified state](#verified-state-2026-11-05)).

---

## 0. Preconditions

| Requirement | Check |
|---|---|
| Switch Console running with a local server and an **online machine** | `kube-investigator switch console status` → `Online machines: 1/1` |
| OpenCode provider installed and signed in on that machine | the six agents start (see step 3) |
| Repo venv installed (`pip install -e '.[dev,kubernetes]'`) | `kube-investigator --help` |
| Console admin session | auto-discovered from `~/Library/Application Support/switchdash/local-switch-server/native/run/switch-core.env`, or set `INVESTIGATOR_CONSOLE_SERVER_URL` / `INVESTIGATOR_CONSOLE_EMAIL` / `INVESTIGATOR_CONSOLE_PASSWORD` |

One-time sanity check (read-only):

```sh
source .venv/bin/activate
kube-investigator switch console status
kube-investigator switch status          # self-hosted Switch stack, if used
kube-investigator audit --verify         # tool-audit chain
```

`switch console status` prints, per agent: isolation mode, provider, token wiring,
actor, tool claim, and expiry. With the hardened defaults you should see
`isolation=isolated` and a role-specific `tools=` list.

---

## 1. Adopt the hardened defaults (once, or before the demo)

```sh
kube-investigator switch console setup --directory .
```

What it does (all idempotent):

- writes `.data/console-agents/<role>/opencode.json` with a signed Ed25519 token
  carrying a role-specific `tools` claim (`0600` config, `0700/0600` keypair);
- creates/updates the six Console agents with `isolation=isolated` and
  auto-approve off;
- ensures the four core rooms (`k8s-incidents`, `k8s-change`, `k8s-memory`,
  `k8s-governance`) and the `k8s-incident-kit` document package.

Expected tail: `updated_agents` lists all six, `console_isolation: isolated`.

> If the installed Console/provider cannot honor isolated agents, use
> `--isolation shared` (an explicit, warned downgrade). Never use
> `--no-actor-tokens` for the demo; it prints a warning and drops attribution.

Then confirm the wiring:

```sh
kube-investigator switch console status
```

Every agent should show `auth=token actor=<role> expires in 13d ... tools=<role tools>`.
The claim is what the MCP server enforces per call.

---

## 2. Set the scene (talk track, ~30 s)

> "Six agents, one room per incident. The commander dispatches specialists; every
> specialist holds a signed credential that only allows its own tools. Every
> evidence read is attributed in a hash-chained audit outside the read-only
> evidence database. The team either cites evidence for a diagnosis or abstains."

---

## 3. Run an incident (live)

```sh
kube-investigator switch console incident --scenario selector-mismatch --json
```

This creates a room named `incident-<id>-<namespace>-<service>`, attaches the
incident-kit documents (`incident-brief`, `evidence-standard`,
`report-template`, `memory-policy`), links the three core rooms, and posts the
kickoff message addressed to `@incident-commander`.

Open the new room in Switch Console. The audience watches:

1. the commander reads the brief and dispatches the specialists as room messages;
2. `cluster-investigator` / `telemetry-analyst` / `change-analyst` report what
   they can and cannot see (`source_status` honestly reports unconfigured live
   sources);
3. `run` produces the deterministic fixture investigation, `verify` re-checks the
   selector proof, `memory` searches approved history;
4. the commander publishes the report document with cited evidence IDs and
   `next_steps`, limited to read-only action.

Healthy and ambiguous scenarios are also available and should end in an honest
`insufficient_evidence` rather than an invented root cause:

```sh
kube-investigator switch console incident --scenario healthy --json
kube-investigator switch console incident --scenario ambiguous --json
```

---

## 4. Prove the guardrails (live, ~2 min)

All of these are read-only and run in a second terminal while the room is open.

```sh
# actor-attributed, hash-chained audit rows (tool + memory logs)
kube-investigator audit --limit 5 --json
kube-investigator switch memory audit --limit 5
kube-investigator audit --verify
kube-investigator switch memory audit --verify
```

Expected: `Tool audit chain is valid.` Each row carries the verified actor and
`auth_mode` (`token` when a public key is configured). A failed audit write
fails the call closed.

Least privilege, proven over a real stdio spawn (the same way the Console agents
launch the server):

```sh
python scripts/mcp_token_probe.py \
  --config .data/console-agents/memory-researcher/opencode.json --expect ok
python scripts/mcp_token_probe.py \
  --config .data/console-agents/memory-researcher/opencode.json --expect missing --drop-token
python scripts/mcp_token_probe.py \
  --config .data/console-agents/memory-researcher/opencode.json \
  --expect foreign --override-actor incident-commander
```

Then show the claim boundary (memory researcher cannot read evidence):

```sh
python scripts/mcp_token_probe.py --help   # or a one-liner call to kube_investigator_get
```

The memory researcher's token is rejected for `get`; the commander's is accepted
(it reaches the store and returns "Investigation not found" for a random ID).

Optional OS-isolation evidence (needs Docker, ~2 s with a warm image):

```sh
bash deploy/mcp-container/verify.sh
```

Expected: five `PASS` lines and `ISOLATION-OK all probes passed`.

---

## 5. Credential discipline (optional, ~1 min)

```sh
kube-investigator switch console status --json | jq '.agent_auth'
```

Shows `valid`, `signature_checked`, `tools`, `expires_at`, `expires_in_seconds`,
and `mode` per agent. Status also reports `EXPIRED`, `INVALID`,
actor/tenant-`MISMATCH`, and `INSECURE` (config permissions wider than `0600`).

Rotation (only if the demo plan includes it — this replaces the shared signing key):

```sh
kube-investigator switch console setup --directory . --rotate-key
kube-investigator switch console status     # new expiry, same actors
# Restart each agent runtime (a Console kickoff does this) so it loads the new key.
```

---

## 6. Cleanup / reset

- Rooms accumulate; archive or delete demo rooms from Console as needed.
- `.data` holds the evidence store, audit logs, and agent configs. **Never**
  delete `.data/mcp-auth` casually: it holds the signing key that still verifies
  any outstanding token.
- Retention is explicit, never automatic:

```sh
kube-investigator maintenance prune --investigation-days 90 --audit-days 30 --dry-run
kube-investigator maintenance prune --investigation-days 90 --audit-days 30 --vacuum
```

It keeps the current audit file, only prunes terminal investigations, verifies an
archive chain before removing it, and reports any hidden `.pruning` leftovers.

---

## Fallback demos (if the live agent runtime misbehaves)

| Fallback | Command | What it proves |
|---|---|---|
| Deterministic offline | `kube-investigator demo --json` | Evidence-backed diagnosis, no cluster/model/Switch |
| Switch room, deterministic coordinator | `kube-investigator switch-demo --scenario selector-mismatch --json` | Room-per-incident + report document without agent runtimes |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Switch Console is not configured` | Set the three `INVESTIGATOR_CONSOLE_*` vars or point `INVESTIGATOR_CONSOLE_ENV_FILE` at the Console server env file |
| `No online Switch Console machine is available` | The Console machine is offline; start it, or pass `--controller-id` |
| Agent shows `auth=token INVALID/EXPIRED` | Re-run `switch console setup` (idempotent) and restart the runtime |
| Agent shows `auth=token MISMATCH` | Token actor/tenant does not match the config env; re-run setup |
| `isolation=shared` after setup | Passed `--isolation shared`; re-run with the default |
| Incident room created, no agent replies | OpenCode provider not signed in on the machine, or the runtime is stopped in Console; check the machine and agent state |
| `tool audit failed` on a call | The audit sink (data dir or anchor dir) is not writable; fix permissions, then retry |
| `audit --verify` reports an empty log | No tool calls yet (fail-closed by design); run an incident first |

---

## Verified state (2026-11-05)

- Console reachable at `http://localhost:63398`, `Online machines: 1/1`.
- `switch console setup --directory .` updated all six agents to
  `isolation=isolated` and signed role claims:

  | agent | tools |
  |---|---|
  | incident-commander | get, memory, run, source_status, verify |
  | cluster-investigator | get, source_status, verify |
  | telemetry-analyst | get, source_status |
  | change-analyst | get, source_status |
  | memory-researcher | memory |
  | evidence-verifier | get, verify |

- Over real stdio: memory researcher `get` → `MCP actor is not authorized for
  this tool`; commander `get` → `Investigation not found` (authorized).
- `audit --verify` → `Tool audit chain is valid.`
- `bash deploy/mcp-container/verify.sh` → 5/5 PASS, `ISOLATION-OK all probes passed`.
