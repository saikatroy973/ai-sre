# Evidence Verifier — verification playbook

Scope: independent re-check of provenance, freshness, scope and the proof.
Never trust the proposed diagnosis.

## Checklist
1. Scope: tenant/cluster/namespace match the request and the stored record.
2. Provenance: every evidence ID exists in the store and belongs to this
   investigation; hashes match the stored digests.
3. Freshness: collection timestamps are within the incident window; mark stale
   or truncated snapshots as limitations.
4. Completeness: selectors, endpoints, pod readiness and ReplicaSet state are
   present; absent data is not zero.
5. Mechanism: the proposed cause explains the observed state (e.g., selector
   mismatch explains empty EndpointSlices; replicas=0 explains no ready pods).
6. Counter-evidence: look for a conflicting signal before passing.

## Abstention
- Abstain when evidence is missing, contradictory, stale, or only from memory.
- Passing means “the evidence supports this claim”, not “no other cause exists”.
- Report checks performed, evidence counted, and the decision with reasons.

## Sources (retrieved 2026-11-07)
- Kubernetes resource metadata/generations:
  https://kubernetes.io/docs/reference/using-api/api-concepts/
- Kubernetes EndpointSlices: https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/
- Project evidence store contract: docs/SWITCH_NATIVE_PLAN.md
