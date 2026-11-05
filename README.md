# Kubernetes Investigator

An evidence-backed Kubernetes investigation team running in **Switch rooms**:
`incident-commander`, `cluster-investigator`, `telemetry-analyst`, `change-analyst`,
`memory-researcher`, and `evidence-verifier`.

This is a hackathon implementation of the Day-2 plan, **not a production-ready deployment**.
Switch is mandatory for investigation runs: each incident gets its own Switch room, every
handoff is a room message from the real sender identity, and evidence/report documents live in
that room. The system either supports a narrowly defined diagnosis with cited evidence or
explicitly abstains. Fixture mode still needs no cluster, model credentials, or live telemetry.

Except for the hardened MCP *server* container profile, local agents and credentials run under one
operating-system account. Actor names, signed tokens, and Switch agent identities in this README
are protocol/credential identities, not same-uid isolation boundaries; a compromised same-uid agent
can read the others' credentials and forge that attribution.

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

## Switch Console quick start (mandatory live path)

Live investigations run through **Switch Console** on a Console-managed local server. The six
agents are Console-managed OpenCode agents; Switch supplies their room tools, and this repository
supplies the read-only Kubernetes MCP tools. The deterministic Python coordinator is an offline
test harness, not the live agent runtime.

Prerequisites:

- Switch Console is installed and running with a local server and an online machine.
- The OpenCode provider is installed and signed in on that machine.
- The repository venv exists (`.venv/bin/python -m kube_investigator.mcp_server`).
- Console admin credentials are available through `INVESTIGATOR_CONSOLE_SERVER_URL`,
  `INVESTIGATOR_CONSOLE_EMAIL`, and `INVESTIGATOR_CONSOLE_PASSWORD`, or through the local
  Console server env file.

```sh
# Show the Console server, machine, agents, and rooms.
kube-investigator switch console status

# Create or verify the six Console-managed agents and the four core rooms.
kube-investigator switch console setup

# Create one incident room from the template and post the commander kickoff.
kube-investigator switch console incident --scenario selector-mismatch

# Inspect the transcript inside Switch Console. The commander dispatches the
# specialists, the verifier checks the cited evidence, and the final-report
# room document is published.
```

What the Console path creates:

- Six Console-managed agents: `incident-commander`, `cluster-investigator`,
  `telemetry-analyst`, `change-analyst`, `memory-researcher`, and `evidence-verifier`.
- The four core rooms: `k8s-incidents`, `k8s-change`, `k8s-memory`, and `k8s-governance`.
- One incident room named `incident-<id>-<namespace>-<service>`, with the template documents
  `incident-brief`, `evidence-standard`, `report-template`, and `memory-policy`.
- A kickoff addressed to `incident-commander`. Switch Console starts the session; the agent uses
  the Switch tools `read_context`, `load_internal_documents`, `send_targeted_message`,
  `post_message`, and `create_room_document`.
- The `k8s-incident-kit` package attached to the room, containing evidence-standard,
  report-template, and memory-policy library documents.
- Directed links from the incident room to `k8s-memory`, `k8s-change`, and `k8s-governance`.
- `evidence-summary` from the cluster investigator, `verification` from the evidence verifier, and
  `final-report` from the incident commander. `memory-context` is created only when approved
  history exists.

The MCP server is declared in `opencode.json` and is scoped to read-only evidence. It has no
Switch write access and no cluster mutation path. Switch Console agents see the room and its
history in Console, not only in a terminal.

The local Console demo runs the OpenCode agents with Auto mode on, because there is no separate
human approval channel in this setup. OpenCode Auto mode approves every request; the MCP tools are
read-only, but that is not an OS sandbox for the agent's provider tools. Use
`switch console setup --no-auto-mode` when a person can approve requests in Console. For a shared
or production deployment, run each agent under a dedicated OS account or container with read-only
mounts and review Console's Auto mode setting.

### Deterministic protocol-level smoke test (optional)

The self-hosted stack is useful for testing the agent-protocol client without Console. It is
**not** the live Console runtime. Docker is required; the pinned images are
`ghcr.io/sandbox-quantum/switch-core:0.31.0` and `gateway:0.31.0`.

```sh
# Start Postgres, switch-core, and the Gateway (API :18000, Gateway :13000).
kube-investigator switch up

# Wait for the public health endpoint, then register the six protocol identities.
curl -fsS http://127.0.0.1:18000/health
export INVESTIGATOR_SWITCH_REGISTRATION_TOKEN="$(
  sed -n 's/^AGENT_REGISTRATION_TOKEN=//p' .data/switch/.env
)"
kube-investigator switch bootstrap

# Run the deterministic fixture investigation through a real Switch room.
kube-investigator switch-demo

# Inspect liveness and the core room IDs.
kube-investigator switch status
kube-investigator switch down          # add --volumes to discard local state
```

`switch up` writes a mode-0600 environment file at `.data/switch/.env` with generated database,
registration, Gateway, and Mattermost credentials. Re-running it reuses that file. Use
`--no-gateway` when only the API and rooms are needed, `--collab` to add the Mattermost profile,
or `--api-port`, `--gateway-port`, and `--postgres-port` to change the published ports.
`--collab` starts Mattermost on port 8065 and the setup seeder; it is still an internal-only
protocol harness, not the Console live path.

The protocol harness creates six Switch agent identities with one API key each, stored locally in
`.data/switch/agents.json` (mode 0600), one incident room per run, and targeted handoffs with
trace/span IDs. It is covered by offline tests and is useful for CI, but it is not a substitute
for Console-managed sessions. Those identities are protocol credentials: all six local processes
run as the same OS user and can read the shared credential file, so this is not a same-uid
isolation boundary either.

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

Verified live: a supported investigation was approved by reviewer `saikat` as record
`957b036c-b8bb-4fbc-9a5b-d3a7c472276e`, published to the self-hosted `k8s-memory` room as document
`300b744b-02d8-4e48-9956-2bd7eee241e9`, and retrieved by the Console memory researcher in room
`50e0f612-61e9-451c-b715-db1ba01445cf`. The researcher created `memory-context`, cited the record,
reviewer, approval date, expiry, and the fixture-vs-live difference, and marked it a historical
lead, never evidence.

That first record had no source Switch room because its investigation was offline. It was then
superseded by record `e984ecff-bc6f-40df-ad9d-0e0be75f10b6`, produced by the live Switch protocol
investigation `26b9521d-6cef-43a4-a737-7852124c14f7` in room
`e155b2fb-9076-420f-afc1-0fa70d1fb61a` and published as document
`f448ca1f-c7bf-4c17-9958-4524c9be3d02`. The new record carries the source room, trace, five
evidence IDs, eleven message IDs, and the content hash. The live memory run also exposed that the
MCP lead payload omitted evidence/message IDs; `HistoricalLead` now returns all of them and the
rendered context includes them.

The MCP `kube_investigator_memory` tool takes an optional nullable tenant. Omitted or `null` uses
the configured server tenant; an explicit tenant is trimmed and must match that server tenant, so
the tool cannot search another tenant. Empty/blank tenants and queries, overlong queries, invalid
scope values, and limits outside 1–20 fail closed with sanitized errors. Results include
`has_more` and `next_cursor`; the cursor references a short-lived ordered snapshot, so approvals
inserted between pages do not shift or duplicate the remaining pages. A direct stdio MCP client
test covers the schema, pagination, rejection cases, and audit output.

Memory reads are audited to hash-chained `memory-audit*.jsonl` files with the actor, effective
tenant, scope, query hash (not the raw query), cursor, outcome, error, and result record IDs.
Entries rotate at a configurable size and include `prev_sha256`/`entry_sha256`. Rejected reads and
CLI `switch memory search` reads are audited too. Verify the chain with:

```sh
kube-investigator switch memory audit --limit 20
kube-investigator switch memory audit --verify
```

The other MCP tools write the same kind of hash-chained, actor-attributed entry to
`tool-audit*.jsonl` in the writable data directory. `run` records the scope, question hash/length,
investigation ID, and trace ID; `get`/`verify` record the investigation and evidence metadata; and
`source_status` records the source and whether it is configured. Each row also records `auth_mode`
(`token` or `dev`) so a development-mode label is distinguishable from a verified token subject.
The evidence SQLite store stays read-only, so serving a staged snapshot still produces an audit
row. A failed audit write fails the tool closed, and the actor is the verified token subject rather
than a fixed operator label. The hash chain detects edits and mid-chain breaks, but without an
external anchor it cannot detect tail truncation or whole-file deletion, and a same-uid writer can
rewrite it: treat it as tamper-evident, not tamper-proof. Inspect or verify the chain with:

```sh
kube-investigator audit --limit 20
kube-investigator audit --verify
```

Set `INVESTIGATOR_MCP_ACTOR` to label the caller in development mode. For a signed credential
check, set `INVESTIGATOR_MCP_PUBLIC_KEY` and pass a signed Ed25519 token in
`INVESTIGATOR_MCP_ACTOR_TOKEN`; **every tool call except `health`** (run/get/verify/memory/
source-status) then rejects missing, expired, wrong-tenant, and wrong-actor tokens:

```sh
python scripts/mcp_actor_token.py keygen --out-dir .data/mcp-auth
python scripts/mcp_actor_token.py sign \
  --private-key .data/mcp-auth/mcp-actor-private.pem \
  --actor memory-researcher --tenant local --ttl 3600
```

The private key stays with the operator; the MCP server only receives the public key. This is a
credential check, not a replacement for OS/process isolation. **Same-uid limit:** in the local
Console profile all six agents run as the same operating-system user, and the mode-0600 configs plus
the shared signing key under `.data/mcp-auth` are readable by that whole account. A compromised
agent can therefore read another agent's token or mint one for any actor and forge the audit
attribution. File modes protect against other users, not against a same-uid compromise. A signing
service with an independent approval/identity check, or a separate uid/container per agent, is
required before these tokens are an isolation boundary. The local Console profile does neither. The
hardened MCP container profile isolates the server process; Console still launches the MCP server
outside it, and the parent agents still share the operator uid.

Approved records can be published directly to the Console `k8s-memory` room:

```sh
kube-investigator switch memory approve ... --console
kube-investigator switch memory publish --id <record-id> --console
```

The live record `e984ecff-…` is attached to the Console memory room as document
`ec18bbac-ebe4-46b3-98d4-f41618ac81da` (`3f13df47-4d6b-4707-99e8-c90a28ca9842`).

An optional local semantic index is available with the `semantic` extra (`model2vec`). Rebuild it
and search by meaning, with keyword matches still contributing to the score:

```sh
python -m pip install -e '.[semantic]'
export INVESTIGATOR_MEMORY_SEMANTIC_MODEL='minishlab/potion-base-8M'
kube-investigator switch memory index
kube-investigator switch memory search --query 'routing failure'
```

The live index returned the checkout selector record for `routing failure`, while unrelated
queries (`email delivery failure`, `weather forecast`, `database latency`) returned no records.

### Switch Trust and ROI

The read-only governance client is separate from the room server. It reads the API key's
tenant/workspace from `GET /api/v1/auth/api-key/identity`, then summarizes evaluation health,
guardrail policies and OK/blocked/redacted outcome totals, and ROI cost data. It never sends
prompts, evidence, or room content to Trust. The client uses the base URL from the official
Switch Trust OpenAPI document: `https://{host}/api/v1`.

```sh
export INVESTIGATOR_TRUST_API_KEY='sk_...'
export INVESTIGATOR_TRUST_INSTANCE='https://app.switchagents.ai'

kube-investigator switch trust status
kube-investigator switch trust publish            # writes a snapshot to k8s-governance
kube-investigator switch trust publish --console  # writes it to Console's k8s-governance room
kube-investigator switch roi --agent-id <trust-agent-id>
```

`switch trust publish` requires a configured Trust key and, by default, a live self-hosted Switch
runtime; it creates a `trust-summary-<timestamp>` room document as `incident-commander` and posts
a governance message. `--console` uses the Console management API to create the document and
attach it to the Console-managed `k8s-governance` room instead. The summary is evidence of what
Trust returned at that moment, not proof that a gate passed. ROI caps are advisory and do not stop
spending; infrastructure cost attribution remains external to Switch ROI.

Verified live against the provisioned Trust workspace: the key identity resolved to one
tenant/workspace, month-to-date cost was 0, and the workspace had no ROI agents at the first read.
`switch trust publish` created `trust-summary-20261104T144158Z` in the self-hosted
`k8s-governance` room, and `switch trust publish --console` created
`trust-summary-20261104T165739Z` in the Console-managed `k8s-governance` room
(`61fd91f6-d5f1-410b-a4f9-d2a6da34f0db`). After live traffic, a status read showed 3 Trust
agents, 3 models, 6 discovered MCP servers, 0 tools, 5 issues, and `$0.0001339` month-to-date
cost.

The pinned CodeScan action's scanner image was downloaded through the Trust API and run against a
clean checkout. It completed its static inventory upload: 2 libraries, 2 organizations, 0 agents,
5 MCP servers, and 0 secrets at the time. A follow-up `switch trust status` read those MCP servers
from the live workspace. The same pinned action then ran on a private GitHub-hosted runner:
[run 41855283512](https://github.com/saikatroy973/kube-investigator/actions/runs/41855283512).
The job completed successfully in 5m50s and uploaded the same static inventory. The action warned
that the MCP AI scanner did not complete because the model provider was unavailable or rate
limiting; the uploaded inventory may be incomplete for AI-only findings. The repository's test
workflow also passed. That static inventory is separate from runtime ROI.

The deterministic evaluation harness (`kube_investigator.eval_harness`) was published through a
temporary Cloudflare tunnel and registered in Trust as a `generic_http` agent endpoint for Trust
agent `agn_0b9ae0a09ef8cf6df36a96d572ae0c9a`. Three agent evaluations were assigned: LLM01 prompt
injection, LLM02 PII leakage, and LLM02 secret leakage. The PII and secret runs finished with 100
results each and every result scored `1`; Trust reported an overall agent score of `1`. The LLM01
fixed-prompt run stalled near completion, was canceled, and was not counted as a pass; a rerun
also stalled and was canceled.

Trust Live traffic now works through OpenTelemetry for the LangChain/OpenAI path. The Python OTLP
exporter needs the full signal URL
(`https://ingestion.switchagents.ai/agent/otel/v1/traces`); passing only the documented base URL
posts to `/agent/otel` and returns `403 Forbidden`. Six instrumented LangChain calls produced
Trust agent `agn_1937c5178825ca80cb70982085181f68` with six interactions (three on
`gpt-6-luna` and three on `claude-haiku-5-5`), prompts, token counts, and latency. Trust still
reports `$0.0001339` MTD cost; the new model was not priced when checked. That is a small but
nonzero ROI result, not a recommendation.

The live guardrail policy **Kubernetes Investigation Enforcement**
(`705263a3-93b3-4e31-aa51-a50cc736800d`) blocks jailbreaks and secrets and redacts email
addresses on user and assistant traffic. The SDK guardrail path (`switch_trust.wrap` with the
policy ID and an agent ID) returns `GUARDRAIL_BLOCKED` for a jailbreak prompt with the policy ID,
policy name, and a High finding. Live outcomes were `ok=7, blocked=2, redacted=1, errored=2,
total=12`. The redaction outcome came from an email prompt; the subsequent provider call failed
because the available provider keys have no credits, so a successful redacted model response was
not demonstrated. Guardrails are enforced by the SDK gateway, not by OTel ingestion; OTel is
telemetry only.

Trust initially reported 5 issues, including two **Critical** model-evaluation risks for
`gpt-6-luna` (model robustness and model security). The review in
[`docs/TRUST_MODEL_REVIEW.md`](docs/TRUST_MODEL_REVIEW.md) found robustness `0.0` and security
`0.113`, and the findings gate correctly failed at the default `high` threshold. The live OTel
profile was changed to `claude-haiku-5-5`; `gpt-6-luna` then left Trust's 3-model inventory and
the current issues endpoint returns only an Informational missing-model-evaluation finding. The
gate therefore passes at `high` again, but this is **not** a remediation claim: the historical
Critical finding remains documented, the gate was not suppressed, and `gpt-6-luna` must not be
reintroduced without a fresh Trust evaluation. `claude-haiku-5-5` has no documented Trust
evaluation yet, so it is provisional.


The local Switch Trust CLI can scan the agent code without the Trust API client:

```sh
python -m pip install switch-trust-cli
kube-investigator switch trust scan --path src/kube_investigator/trust_harness.py
```

`switch trust scan` runs `switch-trust scan` as a subprocess and passes through its exit code.
The AI reasoning/triage stages need a configured provider key; without one, the CLI still emits
the static findings and says the model stages were skipped. The scan is local; it does not upload
room content or evidence.

A local scan of this repository with Switch Trust CLI `1.7.2` scanned one supported framework
file (`trust_harness.py`), reported **0 findings**, and wrote SARIF. Static analysis completed;
AI reasoning attempts against Gemini returned `503 UNAVAILABLE` (high demand) or `429`
(free-tier quota), so this is a static scan of that scannable file, not a completed AI scan or a
tenant CodeScan of every Python module.

Trust evaluations use the deterministic harness at `src/kube_investigator/eval_harness.py`. It does
not call an LLM; it refuses mutation/secret requests and answers the fixture investigation with
cited evidence. Expose it over a URL that Trust can reach:

```sh
uvicorn kube_investigator.eval_harness:app --host 127.0.0.1 --port 8099
# For local testing, expose port 8099 with a tunnel and register that URL as a
# generic_http agent endpoint (input_path=input, output_path=output).
```

The live registration used a temporary Cloudflare tunnel, Trust agent
`agn_0b9ae0a09ef8cf6df36a96d572ae0c9a`, and a `generic_http` endpoint. Temporary tunnels are not a
production hosting path.

`src/kube_investigator/trust_harness.py` is a separate OpenAI Agents SDK harness used as the
CodeScan target. It also exposes `POST /run` with input/output guardrails and the read-only
investigation tool:

```sh
python -m pip install -e '.[trust-eval]'
uvicorn kube_investigator.trust_harness:app --host 127.0.0.1 --port 8099
```

### Switch Trust admin prerequisites

Based on the Switch Trust admin documentation:

- Open **Settings → API keys**. The page requires the **Editor** role or higher; a **Viewer**
  cannot create or see keys.
- Create a key with the least role that works: **Viewer** for read-only `switch trust status`
  and `switch roi`, **Editor** for evaluation/SDK workflows. API keys are shown once and cannot
  be recovered; revoke and replace a lost key.
- Keys can expire after up to 365 days. Lowering a member's role immediately revokes keys
  scoped above the new role.
- The CodeScan action uses a separate `SWITCH_TRUST_TOKEN`, typically an **Ingest only** or
  **Editor** key, plus `SWITCH_TRUST_LLM_API_KEY` for its scanner model. A GitHub personal
  access token is not a Switch Trust API key and is rejected by the Trust API.

This repository never creates, stores, or prints a Trust API key. Set it only in the environment
or the CI secret store.

CI also includes `.github/workflows/switch-trust.yml`, which runs the pinned Switch Trust
CodeScan action when `SWITCH_TRUST_TOKEN` and `SWITCH_TRUST_LLM_API_KEY` are configured. Before
scanning, `scripts/trust_llm_preflight.py` makes a minimal provider call; a missing key, exhausted
quota, or unavailable model fails the job instead of letting a static-only scan look complete.
After scanning, `scripts/trust_gate.py` reads the workspace issues and fails on any finding at or
above `TRUST_GATE_SEVERITY` (default `high`). It normalizes the API's tabular issue rows, maps
`Informational` to `info`, and treats an unknown severity as failing. The gate runs even when the
preflight or scan fails, so a provider outage cannot hide existing findings. Without the Trust
secrets the workflow records an explicit skip; it never reports a skipped scan as a pass. The
CodeScan action and the local CLI scan are separate artifacts.

The live gate currently passes at `high` with 0 failing findings, but only because the reviewed
`gpt-6-luna` model left Trust's 3-model inventory; the historical Critical finding is retained in
`docs/TRUST_MODEL_REVIEW.md`, and no allowlist or gate suppression was added. The supplied OpenAI
key returns `429 credit_balance_exhausted`, so the provider preflight blocks that pinned-action
path rather than reporting a static-only run as complete.

The pinned action also needs a directly billable provider key. For OpenCode Go,
`.github/workflows/switch-trust-opencode.yml` runs the same scanner image through a local
OpenAI-compatible proxy (`scripts/opencode_proxy.py`) that adds the OpenCode session header and
uses `deepseek-v4.1-flash`. GitHub Actions
[run 41893473483](https://github.com/saikatroy973/kube-investigator/actions/runs/41893473483)
completed the AI scan in 4m16s: 2 libraries, 2 organizations, 6 MCP servers, 0 secrets, and no
secret findings; the findings gate passed. The workflow is manual because it downloads a large
scanner image and uses a paid provider. The PR-triggered pinned-action workflow still needs a
directly billable provider key; until then it fails closed at the provider preflight.


### Switch environment

`switch console` reads Console admin credentials from `INVESTIGATOR_CONSOLE_SERVER_URL`,
`INVESTIGATOR_CONSOLE_EMAIL`, and `INVESTIGATOR_CONSOLE_PASSWORD`, or from the local Console
server env file named by `INVESTIGATOR_CONSOLE_ENV_FILE`. The password is never printed.

`Settings.from_env()` defaults to `INVESTIGATOR_SWITCH_MODE=required` for the protocol/API path,
so `investigate` and the HTTP API fail closed unless Switch is reachable and the six protocol
agents are registered. The direct `Settings()` constructor defaults to `off`, which keeps offline
tests and `demo` isolated. `switch-demo` always uses fixture Kubernetes data but requires a real
Switch runtime; it is the deterministic protocol smoke test, not the Console-managed agent path.

See `.env.example` for every Switch and Console setting.

### Durable local incident queue

The CLI can persist requests in SQLite and run them with local workers. This is a durable
queue across process restarts, not a distributed workflow engine:

```sh
kube-investigator queue add \
  --question 'Why is checkout failing?' \
  --cluster demo-cluster --namespace shop --service checkout --deployment checkout
kube-investigator queue list
kube-investigator queue run          # process pending jobs; --once for one
kube-investigator queue retry --id <queue-id>
```

A failed job is visible as `failed` with its sanitized error; the worker does not silently retry
it. `queue retry` returns it to pending. Each worker claims with a generated worker ID and a
lease; on startup it recovers jobs whose lease expired, and completion/failure is guarded by the
claiming worker ID so a stale worker cannot overwrite a recovered job. A concurrency test runs two
workers against one SQLite queue and asserts that no job is claimed twice; a second test runs four
workers over 30 jobs and completes every job exactly once. This is still a single-host SQLite
queue: it is not a distributed, cross-host task ledger. The queue uses the same configured
adapters and the same fail-closed Switch policy as `investigate`.


### Optional OpenTelemetry

The coordinator emits `investigation.collect`, `investigation.reason`, and
`investigation.verify` spans. Tracing is off unless an OTLP endpoint is configured:

```sh
python -m pip install -e '.[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT='http://127.0.0.1:4318'
export OTEL_SERVICE_NAME='kube-investigator'
```

The MCP server also configures the same exporter when it starts. A missing SDK or endpoint is a
no-op, never a reason for an investigation to fail. Switch Trust Live traffic remains a separate
Trust data source; these spans are not a substitute for it.

To send live traffic to Trust, use Trust's OpenTelemetry data source with a LangChain/OpenAI
agent and an **Ingest only** or Editor key. Pass the full trace signal URL to the Python exporter:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT='https://ingestion.switchagents.ai/agent/otel/v1/traces'
OTEL_EXPORTER_OTLP_HEADERS='Authorization=ApiKey sk-...'
OTEL_SERVICE_NAME='kube-investigator'
OTEL_RESOURCE_ATTRIBUTES='gen_ai.agent.name=kube-investigator-otel'
```

The Python exporter uses an explicit `endpoint` as-is; it does not append `/v1/traces`. Passing
only the documented base URL posts to `/agent/otel` and gets `403 Forbidden`. Trust OTel is
telemetry only: prompts, tokens, latency, cost, and sessions. Guardrail enforcement requires the
Switch Trust SDK gateway with a policy ID and an agent identity.

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

Verified live against the local `kind-imesh` cluster, namespace `imesh-demo`, service and
Deployment `backend`: the read-only Kubernetes backend collected Service, Deployment, ReplicaSet,
Pod, and EndpointSlice evidence, found two ready backend pods and matching selectors, and returned
`insufficient_evidence` / `undetermined`. That is the intended abstention: the supported diagnosis
is a selector mismatch, and this service does not have one.


## Switch integration status and honesty boundaries

Implemented and tested offline:

- Switch Console management API client and `switch console status|setup|incident` commands.
- Six Console-managed agent definitions with role-specific instructions, created on an online
  Console machine and registered into Switch rooms.
- A read-only MCP server (`python -m kube_investigator.mcp_server`) with evidence collection,
  persisted-investigation readback, independent verification, scoped memory lookup, and explicit
  source-abstention tools.
- The `k8s-incident-kit` Switch package, automatically attached to new Console incident rooms,
  plus room links to `k8s-memory`, `k8s-change`, and `k8s-governance`.
- A durable local SQLite incident queue (`queue add|list|run`) with worker IDs, leases, expired-lease
  recovery, and a two-worker claim test.
- Optional OTel spans for collection, reasoning, and verification.
- Human-approved incident memory with provenance, scope, expiry, superseding, integrity hashes,
  and prompt-injection-resistant rendering.
- A read-only Switch Trust/ROI client with sanitized errors, key-identity scope discovery,
  evaluation health, guardrail-policy, and cost summaries; `switch trust publish` records a
  snapshot in the self-hosted or Console `k8s-governance` room. A pinned CodeScan CI workflow plus
  a provider preflight and a severity findings gate are included.
- A deterministic HTTP evaluation harness registered as a Trust `generic_http` agent, with LLM01,
  PII, and secret evaluation assignments. A live guardrail policy is created.
- A deterministic agent-protocol harness and a local Docker Compose definition pinned to Switch
  `0.31.0` for offline and protocol-level tests.

Verified live through Switch Console:

- `switch console setup` created or found all six Console-managed OpenCode agents on the online
  Console machine and created `k8s-incidents`, `k8s-change`, `k8s-memory`, and `k8s-governance`.
- `switch console incident` created a template incident room and the kickoff started the
  Console-managed commander session.
- The room transcript shows the commander dispatching `cluster-investigator`,
  `telemetry-analyst`, `change-analyst`, and `memory-researcher` with Switch
  `send_targeted_message`; each specialist used the `kube-investigator` MCP server.
- The cluster investigator created `evidence-summary`; the telemetry and change analysts
  explicitly abstained; the memory researcher found no approved records at that time; the evidence
  verifier re-ran `kube_investigator_verify` against the five persisted evidence IDs and created
  `verification`; the incident commander created `final-report`.
- The selector-mismatch run ended `supported` with evidence IDs and digests cited, and the report
  states its fixture and read-only limitations.
- A second live run with the healthy fixture ended `insufficient_evidence` / `undetermined`. The
  verifier reported matching selectors and the commander explicitly refused to call checkout
  healthy. This is the intended abstention behavior, not a silent failure.
- A third Console run (`50e0f612-61e9-451c-b715-db1ba01445cf`) used the approved memory record
  `957b036c-b8bb-4fbc-9a5b-d3a7c472276e` from reviewer `saikat`, published to the self-hosted
  `k8s-memory` room as document `300b744b-02d8-4e48-9956-2bd7eee241e9`. The memory researcher found
  it, created `memory-context`, cited the record, reviewer, approval date, expiry, and the
  fixture-vs-live difference, and explicitly marked it a historical lead, never evidence. The run
  ended with `evidence-summary`, a passing `verification`, and `final-report`.
- That live memory run exposed a provenance gap: the MCP lead payload omitted source evidence and
  message IDs. `HistoricalLead` now includes the source investigation, trace, evidence IDs, message
  IDs, and content hash, and the rendered historical context includes them.
- The first record was superseded by `e984ecff-bc6f-40df-ad9d-0e0be75f10b6` from the live Switch
  protocol investigation `26b9521d-…` in room `e155b2fb-9076-420f-afc1-0fa70d1fb61a`, published as
  document `f448ca1f-c7bf-4c17-9958-4524c9be3d02`. The superseding record carries the source room,
  trace, five evidence IDs, and eleven message IDs; the old record is no longer returned by default.
- A Console Mattermost bridge (`0907b3e3-371b-4bf6-ae11-08170b2b7975`) was created and a new
  incident room (`e6f091c0-66ad-4ba9-a2b4-8cfa0ebe4f1f`) was bridged to it. Console reports the
  room's external channel URL and six agents plus the human member; the Mattermost channel
  `incident-5fd81254-shop-checkout` contained 10 mirrored investigation messages when checked.
- `switch trust publish --console` attached the Trust snapshot document to the Console-managed
  `k8s-governance` room.

Verified live against the optional self-hosted Switch 0.31.0 protocol stack:

- `switch up` started Postgres, switch-core, and the Gateway; `/health` returned `{"status":"ok"}`.
- `switch bootstrap` registered six protocol identities; `switch-demo` completed two consecutive
  investigations with targeted handoffs and specialist-owned documents.
- The exclusive commander and verifier roles were assumed before handoff and released on session
  close.
- `switch up --collab` started Mattermost on `:8065`; the setup container created the admin,
  team, and user, and registered the default Mattermost bridge.

Not yet implemented or not yet verified:

- Trust Live traffic works via OTel, and the SDK gateway records live guardrail outcomes, but no
  successful redacted model response was demonstrated because the available provider keys have no
  credits. OTel traffic is telemetry only; it does not enforce guardrails. The LLM01 fixed-prompt
  evaluation stalled twice and was canceled both times.
- The reviewed `gpt-6-luna` Critical model-evaluation findings left Trust's current 3-model
  inventory after the live profile changed to `claude-haiku-5-5`; this is an inventory-retention
  effect, not remediation. `claude-haiku-5-5` has no Trust evaluation yet, and there is no
  owner/expiry exception workflow. The pinned action's default provider path did not complete its
  MCP AI stage; the OpenCode Go path did, and the supplied OpenAI key is out of credits.
- The incident queue is multi-worker on one host with worker IDs, leases, stale-worker guards, and
  expired-lease recovery, but it is not a distributed cross-host queue. The 10-concurrent-incident
  and crash/restart production gate has not been run.
- The semantic memory index is optional and disabled by default. Live telemetry/change sources,
  Scale Agentex hosting, and a remediation executor are still absent. Roles are room briefings,
  not Kubernetes authorization or a security boundary.

## Test

```sh
pytest
ruff check .
ruff format --check .
```

Tests use fixtures, an in-memory Switch runtime, and mock HTTP transports; they do not access a
cluster, Scale, or a live Switch server. The GitHub Actions `Tests` workflow
[run 41967118414](https://github.com/saikatroy973/kube-investigator/actions/runs/41967118414)
passed on Python 3.11, 3.13, and 3.14 for commit `bfe1c1d`; the local suite is 259 tests. See
`docs/BUILD_PLAN.md` for the implementation sequence and `docs/SWITCH_NATIVE_PLAN.md` for the gap
audit (2–11) and remaining production gates.

## Reliability drill and hardening

```sh
# Live drill: 10 rooms through leased workers, SIGKILL of the labeled worker
# holding a running job, replacement-worker lease recovery, Switch API and
# Console agent restart legs, and 10 concurrent Console rooms.
.venv/bin/python scripts/reliability_drill.py protocol --rooms 10 --workers 3 --lease 20
.venv/bin/python scripts/reliability_drill.py switch-restart
.venv/bin/python scripts/reliability_drill.py console-agent --console-kickoff
.venv/bin/python scripts/reliability_drill.py console-rooms --rooms 10
# Re-gate an existing drill directory without running anything.
.venv/bin/python scripts/reliability_drill.py verify-protocol --data-dir <drill-dir>
```

Hardening you should wire before showing the agent to anyone else:

- **Per-agent actor tokens.** `kube-investigator switch console setup --directory . \
  --actor-tokens` gives every Console agent its own directory and an Ed25519-signed
  `INVESTIGATOR_MCP_ACTOR_TOKEN` (14-day TTL by default); every evidence tool except `health`
  then authenticates against the signed credential and fails closed on missing, foreign, or
  expired tokens (`scripts/mcp_token_probe.py` proves `ok`/`missing`/`foreign` over a real
  stdio spawn). Every authenticated call is actor-attributed in a hash-chained audit:
  memory searches in `memory-audit*.jsonl`, and `run`/`get`/`verify`/`source_status` in
  `tool-audit*.jsonl`. Audit rows go to the writable data directory, never the read-only
  evidence store; `kube-investigator audit --verify` checks the tool chain. These are
  per-agent credentials, **not a same-uid security boundary**: all six agents run as the
  operator account and can read each other's configs and the shared signing key, so a
  compromised agent can mint or replay another actor's token and forge those audit rows. Use a
  signing service with independent approval/identity checks, or a separate uid/container per
  agent, before treating actor names as identity isolation. A setup rerun re-signs tokens;
  `--rotate-key` replaces the shared signing keypair, so restarted runtimes reject previously
  issued tokens. `setup --refresh` without `--actor-tokens` refuses to downgrade wired agents
  unless `--allow-shared-directory` is passed on purpose.
- **Read-only evidence store.** The MCP `get`/`verify` tools open the SQLite evidence store in
  URI read-only mode: they cannot create, migrate, or write it, and a missing store is a clean
  refusal. `INVESTIGATOR_EVIDENCE_STORE_DIR` points them at a staged snapshot in isolation.
- **OS isolation.** `bash deploy/mcp-container/verify.sh` builds a non-root, read-only,
  capability-less, network-less MCP container, stages a real completed investigation as a
  0444 read-only snapshot, and gates five probes (`uid-isolation`, `filesystem-isolation`,
  `network-isolation`, `mcp-stdio-handshake`, `evidence-store`); details and limits:
  `docs/OS_ISOLATION.md`. The same gate runs as the `isolation` CI job on every push.
