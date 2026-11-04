# Kubernetes Investigator

An evidence-backed Kubernetes investigation team running in **Switch rooms**:
`incident-commander`, `cluster-investigator`, `telemetry-analyst`, `change-analyst`,
`memory-researcher`, and `evidence-verifier`.

This is a hackathon implementation of the Day-2 plan, **not a production-ready deployment**.
Switch is mandatory for investigation runs: each incident gets its own Switch room, every
handoff is a room message from the real sender identity, and evidence/report documents live in
that room. The system either supports a narrowly defined diagnosis with cited evidence or
explicitly abstains. Fixture mode still needs no cluster, model credentials, or live telemetry.

**Switch-native plan:** [docs/SWITCH_NATIVE_PLAN.md](docs/SWITCH_NATIVE_PLAN.md). The offline
`demo` path remains credential-free; Switch-required flows fail closed when Switch is unavailable.

## Quick start: offline evidence demo

Requires Python 3.11 or later.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
kube-investigator demo
```

The demo investigates `checkout` in namespace `shop` on `demo-cluster`. It demonstrates ready
Deployment-owned pods, a mismatched Service selector, no ready endpoints, and a cited report.
**No Kubernetes, model, or Switch calls are made.**

```sh
kube-investigator demo --scenario healthy
kube-investigator demo --scenario ambiguous
kube-investigator demo --json
```

The healthy and ambiguous scenarios intentionally return `insufficient_evidence` rather than
inventing a root cause. CLI success means a report was produced; inspect `report.outcome` to
decide whether a diagnosis was supported.

## Switch-native quick start

Docker is required for the local Switch stack. The pinned images are
`ghcr.io/sandbox-quantum/switch-core:0.31.0` and `gateway:0.31.0`.

```sh
# Start Postgres, switch-core, and the Gateway (API :18000, Gateway :13000).
kube-investigator switch up

# Wait for the public health endpoint, then register the six agent identities.
curl -fsS http://127.0.0.1:18000/health
export INVESTIGATOR_SWITCH_REGISTRATION_TOKEN="$(
  sed -n 's/^AGENT_REGISTRATION_TOKEN=//p' .data/switch/.env
)"
kube-investigator switch bootstrap

# Run the fixture investigation through a real Switch room.
kube-investigator switch-demo

# Inspect liveness and the core room IDs.
kube-investigator switch status
kube-investigator switch down          # add --volumes to discard local state
```

`switch up` writes a mode-0600 environment file at `.data/switch/.env` with generated database,
registration, and Gateway credentials. Re-running it reuses that file. Use `--no-gateway` when
only the API and rooms are needed, or `--api-port`, `--gateway-port`, and `--postgres-port` to
change the published ports.

What the run creates:

- One internal-only incident room named `incident-<id>-<namespace>-<service>`.
- The four core rooms: `k8s-incidents`, `k8s-change`, `k8s-memory`, `k8s-governance`.
- Six Switch agent identities with one API key each, stored locally in
  `.data/switch/agents.json` (mode 0600; keys are never printed or persisted in investigations).
- Room documents: `incident-brief`, `evidence-standard`, `report-template`, `memory-policy`,
  `evidence-summary`, `verification`, and `final-report` (plus `memory-context` when approved
  history matches).
- Targeted, structured handoffs with trace/span IDs. The `telemetry-analyst` and
  `change-analyst` explicitly abstain because no live telemetry or change source is configured.

The local stack uses the upstream standalone compose definition copied to
`deploy/switch/compose.yaml`. Rooms are internal-only; no Mattermost/Slack bridge is started.

### Approved incident memory

Switch is the system of record. The composed **Switch Incident Memory** layer keeps approved
records as documents and messages in `k8s-memory`, plus a scoped SQLite search index:

```sh
kube-investigator switch memory approve \
  --investigation <investigation-id> \
  --reviewer alice \
  --title 'Checkout selector mismatch' \
  --summary 'The checkout Service selector excluded the ready Deployment.'
kube-investigator switch memory list
kube-investigator switch memory search --query 'checkout selector'
```

Approval requires a `supported` report unless `--allow-unsupported` marks the record as a
hypothesis. When Switch is required, the record is published to the memory room **before** it is
indexed locally; a refused publish leaves no locally approved record. Retrieval is scoped by
tenant, cluster, and namespace. Historical records are rendered as untrusted data, never passed
to the reasoner, and never counted as evidence. There is no native "Switch Memory Bank" product:
this is a documented composition of Switch persistent memory, room documents, linked rooms, and
human approval.

### Switch Trust and ROI

The read-only governance client is separate from the room server. It reads the API key's
tenant/workspace from `GET /auth/api-key/identity`, then summarizes evaluation health, guardrail
policies, and ROI cost data. It never sends prompts, evidence, or room content to Trust.

```sh
export INVESTIGATOR_TRUST_API_KEY='sk_...'
export INVESTIGATOR_TRUST_INSTANCE='https://app.switchagents.ai'

kube-investigator switch trust status
kube-investigator switch trust publish   # writes a snapshot to k8s-governance
kube-investigator switch roi --agent-id <trust-agent-id>
```

`switch trust publish` requires both a configured Trust key and a live Switch runtime; it creates
a `trust-summary-<timestamp>` room document as `incident-commander` and posts a governance
message. The summary is evidence of what Trust returned at that moment, not proof that a gate
passed. ROI caps are advisory and do not stop spending; infrastructure cost attribution remains
external to Switch ROI.

CI includes `.github/workflows/switch-trust.yml`, which runs the pinned Switch Trust CodeScan
action when `SWITCH_TRUST_TOKEN` and `SWITCH_TRUST_LLM_API_KEY` are configured. Without those
secrets the workflow records an explicit skip; it never reports a skipped scan as a pass.

### Switch environment

`Settings.from_env()` defaults to `INVESTIGATOR_SWITCH_MODE=required`, so `investigate` and the
HTTP API fail closed unless Switch is reachable and the six agents are registered. The direct
`Settings()` constructor defaults to `off`, which keeps offline tests and `demo` isolated.
`switch-demo` always uses fixture Kubernetes data but requires a real Switch runtime.

See `.env.example` for every Switch setting.

## HTTP API


Generate an API token and bind the server to localhost:

```sh
export INVESTIGATOR_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn kube_investigator.api:create_app --factory --host 127.0.0.1 --port 8000
```

In a terminal with the same token:

```sh
curl http://127.0.0.1:8000/v1/investigations \
  -H "Authorization: Bearer $INVESTIGATOR_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Why is checkout failing?","cluster":"demo-cluster","namespace":"shop","service":"checkout","deployment":"checkout"}'
```

Endpoints:

- `GET /health`: minimal, unauthenticated liveness response.
- `POST /v1/investigations`: execute a bounded investigation and return its record/report.
- `GET /v1/investigations/{id}`: retrieve the persisted record.
- `GET /v1/investigations/{id}/messages`: inspect structured agent handoffs.
- `GET /v1/investigations/{id}/evidence`: inspect projected evidence snapshots.

Every `/v1` endpoint requires bearer authentication. The configured server-side principal, not request text, determines tenant/cluster/namespace access. This milestone has one API principal; it is **not** an enterprise identity provider. Investigation responses are private to that principal. The API refuses to start without a token. Interactive API docs are disabled.

`Settings.from_env()` requires Switch, so API workers start only after `switch bootstrap`. This is deliberate fail-closed behavior, not a degraded local mode. Use `INVESTIGATOR_SWITCH_MODE=off` only for isolated development and tests, where investigation records have no Switch room.

The API executes synchronously with bounded admission. It is not a durable task queue; a client disconnect does not cancel work. Do not expose it publicly or run multiple API workers against this milestone's local admission controls.

## Evidence and verification

The collector reads only Services, Deployments, ReplicaSets, Pods, and EndpointSlices. It stores field-allowlisted projections: no pod environment variables, Secret bodies, logs, shell execution, or mutation tools.

Evidence includes the investigation scope, source/query, collection time, resource UID/version, and a SHA-256 digest. SQLite rejects updates/deletes to evidence rows. Hashes detect changes but do not constitute signed, tamper-proof audit storage.

The currently supported diagnosis is **Service selector mismatch affecting the explicitly named Deployment**. Verification requires complete, fresh, correctly scoped evidence; a current observed Deployment generation; ready Pods linked through ReplicaSet ownership to that Deployment; selectors excluding those Pods; and no ready Service endpoints. A historical cause, rollout regression, memory leak, or other diagnosis is never inferred by this milestone.

Source reads are not an atomic cluster snapshot. Reports describe the observations at collection time; they are not a guarantee about current state or all causes of an outage. An independent verifier agent is a logical boundary here, not a separate security-isolated workload or proof of correctness.

## Optional Scale inference

The default investigator uses a deterministic baseline, **not an LLM**. To use a Scale model to propose the diagnosis:

```sh
export INVESTIGATOR_REASONER=scale
export SGP_API_BASE_URL='https://YOUR-SCALE-API-HOST'
export SGP_API_KEY='YOUR-KEY'
export SGP_ACCOUNT_ID='YOUR-ACCOUNT'
export SGP_MODEL='YOUR-ENABLED-VENDOR/MODEL'
kube-investigator investigate --cluster demo-cluster --namespace shop \
  --service checkout --deployment checkout --question 'Why is checkout failing?'
```

The adapter calls the documented SGP V5 chat-completions endpoint once, limits output tokens, and records returned usage. Only a typed candidate and evidence IDs are accepted. The deterministic verifier remains authoritative; arbitrary model explanations are not published as facts. API/format failures result in a partial report, not a silent fallback or an invented diagnosis.

This sends your question and projected cluster evidence to the configured Scale endpoint. Approve that data flow first. Copy the URL and model ID from your provisioned account; availability is not assumed. Credentials are never accepted in requests or stored in evidence. `.env.example` documents settings; `.env` is not loaded automatically.

This is **SGP inference integration, not Agentex hosting or AgentOps export**. Those are later milestones, and have not been deployed or verified against a live Scale account.

## Optional live Kubernetes reads

```sh
python -m pip install -e '.[kubernetes]'
export INVESTIGATOR_BACKEND=kubernetes
export INVESTIGATOR_CLUSTER=my-cluster
export INVESTIGATOR_NAMESPACES=shop
export INVESTIGATOR_KUBE_CONTEXT=explicit-read-only-context
kube-investigator investigate --cluster my-cluster --namespace shop \
  --service checkout --deployment checkout --question 'Why is checkout failing?'
```

Use a dedicated, least-privilege context. `deploy/reader-rbac.yaml` is a namespace-scoped example for an administrator to review; this application never applies it. No default/current-context fallback is allowed. Kubeconfig authentication plugins may execute locally as part of client authentication, so use only a trusted kubeconfig. The operator's cluster label must accurately identify that context; this milestone does not attest cluster identity.

Reads are bounded and time-limited. Truncated lists, missing objects, authorization failures, and unavailable APIs cause abstention/partial results. API request fields cannot select a kubeconfig, credentials, or a different context. RBAC is enforced by Kubernetes, not by an agent prompt.

## Switch integration status and honesty boundaries

Implemented and tested offline:

- Six-agent registration through the Switch agent protocol, per-identity API keys, and core rooms
  created from Switch room templates.
- One incident room per investigation, targeted handoffs, room documents, and evidence/report
  publication.
- Human-approved incident memory with provenance, scope, expiry, superseding, integrity hashes,
  and prompt-injection-resistant rendering.
- A read-only Switch Trust/ROI client with sanitized errors, key-identity scope discovery,
  evaluation health, guardrail-policy, and cost summaries; `switch trust publish` records a
  snapshot in `k8s-governance`. A pinned CodeScan CI workflow is included.
- A local Docker Compose definition pinned to published Switch `0.31.0` images.

Verified live against a local Switch 0.31.0 stack:

- `switch up` started Postgres, switch-core, and the Gateway; `/health` returned `{"status":"ok"}`.
- `switch bootstrap` registered all six identities and created `k8s-incidents`, `k8s-change`,
  `k8s-memory`, and `k8s-governance`.
- `switch-demo` completed two consecutive investigations in fresh incident rooms. Handoffs were
  posted as targeted messages from the real sender identities. The four template documents were
  present, and `evidence-summary`, `verification`, and `final-report` were created by the
  specialist identities that own them.
- The exclusive `incident-commander` and `evidence-verifier` roles were assumed before handoff
  and released on session close, so a second run did not hit a stale role lease.
- Switch's admin client still posts its normal onboarding greeting/warning in a new room; the
  warning is about the initial greeting, not a missed handoff. The role is held before work is
  dispatched.

Not yet implemented or not yet verified:

- The Compose stack's optional Mattermost `collab` profile and the Gateway UI were not exercised;
  the local run used the API, rooms, and the Gateway container only. No external messaging bridge
  is configured.
- The Trust and ROI client is tested against mocked HTTP responses, not a provisioned Trust
  tenant. No claim in this README should be read as a live Trust or ROI result. Trust Live
  traffic, SDK/OTel instrumentation, and a CI gate that blocks promotion on findings are not
  implemented; the CodeScan workflow only records findings.
- No durable incident queue, semantic memory index, live telemetry/change source, Scale Agentex
  hosting, or remediation executor exists. Roles are room briefings, not Kubernetes
  authorization or a security boundary.

## Test

```sh
pytest
ruff check .
ruff format --check .
```

Tests use fixtures, an in-memory Switch runtime, and mock HTTP transports; they do not access a
cluster, Scale, or a live Switch server. See `docs/BUILD_PLAN.md` for the implementation sequence
and `docs/SWITCH_NATIVE_PLAN.md` for the remaining production gates.
