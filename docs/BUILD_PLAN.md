# Day-2 implementation plan

## Milestone 1: local evidence-backed vertical slice

Implemented:

- Coordinator, Cluster Investigator, and Evidence Verifier logical roles.
- Six Switch agent identities, core rooms, one internal-only Switch room per incident, targeted
  handoffs posted as room messages, and evidence/report room documents.
- Composed Switch Incident Memory with human approval, provenance, scope, expiry, superseding,
  and offline retrieval tests.
- Explicit, allowlisted, persisted handoffs with connected trace/span identifiers.
- Offline mismatch, healthy, and ambiguous fixture scenarios.
- Opt-in Kubernetes SDK adapter with five bounded get/list operations.
- Field-allowlisted evidence, scope/time/provenance checks, SHA-256 digests, append-only rows.
- Deployment → ReplicaSet → Pod ownership validation and a narrowly defined selector proof.
- Fail-closed verifier, explicit abstention, partial reports, and model-usage capture.
- Optional SGP V5 inference adapter with strict output validation; no implicit remote fallback.
- Local CLI, authenticated/scoped HTTP API, and local Switch Compose/CLI scaffolding.
- Offline tests, namespace-reader RBAC example, and CI configuration.

### Boundaries

This is not deployed to Scale or Agentex. The live path is a local Switch Console with six Console-managed OpenCode sessions. The offline deterministic harness still runs its roles in one process and is not a security boundary; it exists for tests and CI. SQLite persists records and a single-host queue with worker IDs, leases, and expired-lease recovery; it is not a distributed cross-host ledger, and admission limits are per-process. Optional OTel spans are emitted only when an OTLP exporter is configured; they do not replace Switch Trust Live traffic.

SGP inference is an opt-in adapter, not Agentex orchestration, AgentOps tracing export, a full token-cost ledger, or an assertion of live integration success. No cloud resources or permissions have been created. No remediation tool exists. The Trust/ROI client, pinned CodeScan workflow, provider preflight, and findings gate are implemented; live Trust reads, Console governance publishing, the evaluation harness, a guardrail policy, and a zero-finding gate have been verified, but Trust Live traffic, runtime guardrail enforcement, and nonzero ROI remain unverified. The local Switch Console path has created six managed agents, core rooms, incident rooms, and completed cited investigations through Switch tools and the read-only MCP server. A Console incident room is bridged to a Console Mattermost bridge with an external channel URL; the optional self-hosted `collab` profile is also active.

## Milestone 2: broaden the investigation team

1. Add Telemetry Analyst: bounded, redacted logs and metrics; never unrestricted shell access.
2. Add Change Analyst: deployment history and repository changes with evidence provenance.
3. Add Memory Researcher: scoped retrieval over human-approved Switch memory records.
4. Separate historical hypotheses from current facts. Version, review, expire, and correct memory.
5. Extend the verifier with domain-specific proof rules and held-out evaluation fixtures.

The Switch-native milestone adds the six identities and room protocol now. Telemetry and change
specialists deliberately abstain until live, permissioned sources exist. Memory retrieval is
implemented over approved Switch records; a semantic/vector index remains an optional external
read model.

## Milestone 3: Scale runtime and deployment

1. Confirm provisioned SGP account, endpoints, models, Agentex, AgentOps, and platform features.
2. Move coordinator dispatch/state into Agentex async tasks and Temporal workflows.
3. Run distinct permission roles in separate workloads/identities.
4. Put network calls in retry-safe activities; enforce idempotency at tool boundaries.
5. Add shared admission, per-incident deadlines/budgets, cancellation, and rate limiting.
6. Add Agentex manifests, environment configuration, secrets references, and CI/CD.
7. Coordinate supported autoscaling with the platform owner; load/kill/restart-test workers.
8. Keep runtime infrastructure independent from the diagnosed cluster where practical.

## Milestone 4: governance before production access

1. Enterprise authentication, authorization, tenant isolation, and workload identity.
2. SPIFFE/SPIRE integration if required; explicit policy enforcement still handles authorization.
3. Scale AgentOps grounding/accuracy evaluations, custom deterministic criteria, human review.
4. Prompt-injection tests across logs, tools, memory, and peer messages.
5. Supplementary Switch Trust CLI scans/evals; inspect result findings, not just exit code.
6. Signed or protected audit events, retention policies, data classification, and secret rotation.
7. Only later: separate allowlisted remediation executor, exact-action approvals, expiry,
   replay prevention, preconditions, rollback, and independent outcome validation.

## Milestone 5: observe and optimize

1. Export unified multi-agent traces to Scale AgentOps; add infra metrics and alerts separately.
2. Attribute usage to organization/team/incident/task/agent/model, preserving unknown usage.
3. Reconcile model, embedding, retrieval, infrastructure, and platform costs without double counting.
4. Enforce shared pre-call reservations and budgets; account for already-running calls.
5. Evaluate cheaper model routes on identical held-out data before promotion.
6. Add production SLOs, canary/rollback procedures, backup/restore drills, and ownership/runbooks.

## Switch-native plan (mandatory)

The earlier Scale-first direction is superseded. Switch is mandatory at every lifecycle layer:
Rooms for collaboration, Console/agent management for runtime, Trust for discovery/evaluation/
guardrails, ROI for model spend, and Trust CLI/MCP/API/CodeScan for developer and CI workflows.

See [SWITCH_NATIVE_PLAN.md](SWITCH_NATIVE_PLAN.md) for the full room topology, agent roster,
memory layer, Trust gates, identity model, scaling constraints, and 10-video mapping.

Important corrections:

- Public Switch docs have **no product named “Memory Bank.”** The plan composes per-agent
  Persistent memory, room documents, packages, and a curated memory room. Any semantic index is
  an external read model over approved Switch documents, not a native feature.
- Switch has no native SPIFFE identity, per-agent resource grants, autoscaling, or durable
  workflow engine. Those remain external controls; Switch roles and room access are not
  Kubernetes authorization.
- Switch Trust evaluations require a reachable HTTP endpoint. Console sessions need an
  evaluation harness; do not claim they are automatically evaluated over HTTP.
- ROI caps are advisory and do not stop spending; infrastructure cost attribution remains external.

These are implementation requirements, not optional caveats. The demo must show the Switch
surfaces directly and label composed/external capabilities honestly.

## Series alignment

| Videos | Pillar | Deliverable |
| --- | --- | --- |
| 1–3 | Build | Introduction, read-only tools, evidence-backed multi-agent investigations |
| 4–6 | Deploy/scale | Agentex deployment, durable workflows, recovery and concurrency tests |
| 7–8 | Govern | Workload identity, authorization, evaluation and approved-action boundaries |
| 9–10 | Observe | Connected traces, SLOs, costs and evaluated model-routing optimization |

Teaching order does not postpone security: basic scope enforcement and verification start in the
first slice, and deployments remain staging/read-only until production gates pass.

## Documentation

- [Scale Agentex async agents](https://docs.gp.scale.com/docs/v5/agents/agentex/types/agentic-agent.md)
- [Scale Cloud Build and Deploy](https://docs.gp.scale.com/docs/v5/agents/agentex/cloud-build-and-deploy/overview.md)
- [Scale multi-agent tracing](https://docs.gp.scale.com/docs/v5/tracing/integrations/agentex/multi-agent-tracing.md)
- [Scale custom-function evaluations](https://docs.gp.scale.com/docs/v5/next-gen-evaluation/tasks/custom-function.md)
- [Scale inference](https://docs.gp.scale.com/reference/v5/inference/generate-openai-chat-completion-from-messages.md)
- [Switch agent protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol.md)
- [Switch shared responsibility](https://docs.switchagents.ai/switch-rooms/resources/shared-responsibility.md)
