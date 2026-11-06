# Showcase every Switch surface

The hackathon rule is "Switch is the system of record" at every layer. This is
the surface-by-surface tour with the exact commands, what the audience should
see, and the current live status. Companion docs:
[`SHOWCASE.md`](SHOWCASE.md) (8-minute Console runbook),
[`MATTERMOST.md`](MATTERMOST.md) (local Mattermost server with the six agents),
[`SWITCH_NATIVE_PLAN.md`](SWITCH_NATIVE_PLAN.md) (the mandatory matrix and the
honesty contract).

**Current status (2026-11-05):** Console, rooms, memory, MCP tool surface, audit
and the isolation gate are live and demo-ready. Switch Trust (hosted) and ROI
need `INVESTIGATOR_TRUST_API_KEY`; the Trust CLI scan works locally and reports
zero findings on this repository's OpenAI Agents harness.

---

## 0. Preflight (30 s, read-only)

```sh
source .venv/bin/activate
kube-investigator switch console status      # Console: machine, agents, tokens, rooms
kube-investigator switch status              # self-hosted Switch stack: healthy?
kube-investigator switch memory list         # approved historical records
kube-investigator audit --verify             # tool-audit chain
```

Expected today: `Online machines: 1/1`, six agents with `isolation=isolated` and
role tool claims, `healthy: true` with 6 agents / 4 core rooms, one approved
memory record, `Tool audit chain is valid.`

> The Console sidebar's "Update failed" badge is the desktop app's auto-updater;
> it does not affect agents or sessions.

---

## 1. Switch Console — collaboration, agent lifecycle, runtime

| Surface | Command | What to show |
|---|---|---|
| Agent management | `kube-investigator switch console setup --directory .` | Idempotent provisioning: one directory + signed token per agent, six Console agents, four core rooms, incident-kit package. |
| Runtime / machines | `kube-investigator switch console status` | Online machine, `isolation=isolated`, provider, desired state, token actor/claims/expiry. |
| Rooms / sessions | `kube-investigator switch console incident --scenario selector-mismatch --json` | Creates `incident-<id>-shop-checkout`, attaches `incident-kit` documents, links memory/change/governance rooms, posts the `@incident-commander` kickoff. |
| Room documents / packages / links | (same command, then open the room in Console) | `incident-brief`, `evidence-standard`, `report-template`, `memory-policy`; package attachments; linked rooms. |
| Handoffs | (watch the room) | Commander dispatches specialists; specialists report; report cites evidence IDs. |

The Console UI is where the audience watches: **Your Agents** (the six cards in
your screenshot), **Your Rooms** (core + incident), and the room timeline.

---

## 2. Switch rooms and the self-hosted protocol harness

Four layers of evidence, clearly labelled as the harness (not the live
Console runtime):

```sh
kube-investigator switch status --json          # healthy, six registered agents, core room ids
kube-investigator switch-demo --scenario selector-mismatch --json   # deterministic run in a real Switch room
```

`switch-demo` proves the protocol: one room per incident, actor-attributed
messages, and a report document inside the room. Reload/restart evidence (optional,
needs the local stack):

```sh
python scripts/reliability_drill.py --help
```

Optional lifecycle commands (they mutate the local Docker stack):

```sh
kube-investigator switch up        # start the compose stack
kube-investigator switch bootstrap # register the six agent identities + core rooms
kube-investigator switch down
```

Say the honest part: the Python protocol runtime is a **test harness**; the live
runtime is Console-managed sessions.

---

## 3. Switch memory — approved incidents as untrusted leads

```sh
kube-investigator switch memory list --json
kube-investigator switch memory search --query "selector" --cluster demo-cluster --namespace shop
kube-investigator switch memory search --query "selector" --cluster demo-cluster --namespace shop --json
```

The search output is deliberately framed as **HISTORICAL CONTEXT — UNTRUSTED
DATA, NOT EVIDENCE** and records `policy=historical_leads_are_not_evidence` and
`used_as_evidence=false`. Show the prompt-injection guard, not just the hit.

Approve and publish a finished record into the Console memory room:

```sh
kube-investigator switch memory approve --investigation <investigation-id> \
  --reviewer <name> --title "<short title>" --console --json
kube-investigator switch memory publish --id <record-id> --console --json
kube-investigator switch memory audit --limit 5 --json   # actor, query hash, record ids
kube-investigator switch memory audit --verify           # hash-chained memory audit
```

Optional semantic index: `kube-investigator switch memory index`.

---

## 4. Switch Trust (hosted) — discovery, evaluations, guardrails, ROI

Configure once (the key is pinned to one tenant/workspace; the optional IDs must
match it):

```sh
export INVESTIGATOR_TRUST_API_KEY=...          # required
export INVESTIGATOR_TRUST_INSTANCE=https://app.switchagents.ai   # default
export INVESTIGATOR_TRUST_TENANT_ID=...        # optional, must match the key
export INVESTIGATOR_TRUST_WORKSPACE_ID=...     # optional, must match the key
```

Then:

```sh
kube-investigator switch trust status --json
kube-investigator switch trust status --agent-id <agent-id> --json
kube-investigator switch roi --json
kube-investigator switch roi --agent-id <agent-id> --json
```

`trust status` reads: API identity/key scope, tenant/workspace, month-to-date
cost + cap, inventory (agents, MCP servers, models, tools, issues), guardrail
policies and outcomes (`ok`/`alerted`/`blocked`/`redacted`/`errored`), and per
agent evaluation score/coverage/trend. `roi` adds agents with activity and the
latest analyses (tool bloat, model optimizer, verbosity, context cache, retry
loops). Both print the advisory: ROI caps do not stop spending; infrastructure
cost is external.

Publish the governance snapshot into the Console governance room:

```sh
kube-investigator switch trust publish --console --json
```

That creates `trust-summary-<timestamp>` as a document in `k8s-governance` and
attaches it; the JSON returns the document id, room id, and Console URL.

**Without the key**, the commands fail closed:
`Switch Trust is not configured; set INVESTIGATOR_TRUST_API_KEY` — no invented
inventory, scores, or costs. Show that sentence as the honesty contract.

---

## 5. Developer trust — Trust CLI, CodeScan, gates

The Trust CLI scans agent code (OpenAI Agents SDK, Google ADK, LangChain,
CrewAI, etc.) and emits JSON or SARIF:

```sh
python -m pip install switch-trust-cli      # provides the `switch-trust` binary
source .venv/bin/activate                   # so the wrapper finds `switch-trust` on PATH
kube-investigator switch trust scan --path src/kube_investigator/trust_harness.py \
  --format json --output /tmp/trust-scan.json
```

Today this detects `Framework: OpenAI Agents SDK`, scans one file with
`bandit`, `opengrep`, `detect-secrets` and an AI reasoning pass, and reports
**0 findings** on this repository's harness. A deliberately small sample agent
produces findings (critical "Agent Goal Hijack", high "Rogue Agents"), which is
how you demonstrate the output format without mislabelling this repo.

CI surfaces already wired in the repo:

| File | Purpose |
|---|---|
| `.github/workflows/switch-trust.yml` | Switch Trust CodeScan action on PRs + nightly; secrets `SWITCH_TRUST_TOKEN` and `SWITCH_TRUST_LLM_API_KEY`; gate severity `TRUST_GATE_SEVERITY` (default `high`); a skipped scan is never reported as a pass |
| `.github/workflows/switch-trust-opencode.yml` | OpenCode AI CodeScan variant |
| `scripts/trust_gate.py` | Fail a job at or above a severity; needs `SWITCH_TRUST_TOKEN` |
| `scripts/trust_llm_preflight.py` | Fail a CodeScan job early when the LLM provider key/quota is missing |

The repo's own gate result is recorded in `docs/SWITCH_NATIVE_PLAN.md`
(row 6, CI gates).

---

## 6. MCP tool surface — what the Console agents call

```sh
python scripts/mcp_token_probe.py --config .data/console-agents/memory-researcher/opencode.json --expect ok
python scripts/mcp_token_probe.py --config .data/console-agents/memory-researcher/opencode.json --expect missing --drop-token
python scripts/mcp_token_probe.py --config .data/console-agents/memory-researcher/opencode.json --expect foreign --override-actor incident-commander
kube-investigator audit --limit 5 --json
kube-investigator audit --verify
bash deploy/mcp-container/verify.sh            # optional, needs Docker
```

Nine tools: `health` (liveness only), `run`, `get`, `verify`, `memory`,
`source_status`, `docs` (Kubernetes docs + the caller's own SRE playbook; scoped
by the signed actor and granted to all six roles), and the live-source tools
`logs` and `metrics` (bounded pod logs and metrics-server usage, granted only to
`telemetry-analyst`). The docs index is built from the upstream English Kubernetes documentation
(`scripts/index_kubernetes_docs.py`) and every result carries a
`kubernetes.io` URL. Least privilege is enforced from signed `tools` claims; every
authenticated call writes an actor-attributed, hash-chained row; the evidence
store stays read-only. `verify.sh` proves the hardened container gate
(uid, filesystem, network, stdio, digest-anchored staged evidence).

---

## 7. Mapping to the mandatory matrix

| Matrix layer | Shown by |
|---|---|
| Collaboration (rooms, docs, packages, links) | Console incident room + `switch-demo` |
| Agent lifecycle + runtime | Console `setup` / `status` / managed sessions |
| Trust (discovery, inventory, evaluations, guardrails, live traffic) | `switch trust status` (+ recorded results), Trust CodeScan |
| Developer trust (CLI, CodeScan, MCP, SDKs) | `switch trust scan`, CI workflows, MCP server |
| Cost (ROI) | `switch roi`, cost/cap in `trust status` |
| Governance | `k8s-governance` room + `switch trust publish --console` |
| CI/CD trust gates | workflows + `trust_gate.py` + `trust_llm_preflight.py` |

Keep the honesty contract: "composed" (semantic index, durable queue,
infrastructure cost) is not claimed as native Switch.

---

## 8. Suggested demo order (10–12 min)

1. Preflight: `switch console status`, `switch status` (1 min)
2. Console: `setup` (idempotent), `incident`, watch the room (4 min)
3. Memory: `search` (untrusted framing), `audit --verify` (1 min)
4. Trust: `trust status`, `trust publish --console`, `roi` (2 min, needs key)
5. Developer trust: `trust scan` on the harness + CI workflows (1–2 min)
6. Guardrails: stdio probes, claim denial, `audit --verify`, `verify.sh` (2 min)

---

## 9. Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `Switch Trust is not configured; set INVESTIGATOR_TRUST_API_KEY` | Hosted Trust is optional; export the key (and matching tenant/workspace) |
| `INVESTIGATOR_TRUST_TENANT_ID does not match…` | Optional ID must equal the key's pinned scope; unset it |
| `Switch Trust CLI is not installed` | `source .venv/bin/activate` (the venv has `switch-trust`), or set `INVESTIGATOR_TRUST_CLI_BIN` |
| Trust scan: `No scannable targets found` | The scanner only targets agent-framework code (OpenAI Agents, ADK, LangChain…); this repo's harness is the target |
| `trust_gate.py` says `SWITCH_TRUST_TOKEN is required` | CI gate uses a Trust API token, not the local key env |
| Console `No online … machine` | Start the Console machine; or pass `--controller-id` |
| Trust scan triage logs "No API key" | Only the optional LLM triage step needs Gemini; raw findings still produced |
