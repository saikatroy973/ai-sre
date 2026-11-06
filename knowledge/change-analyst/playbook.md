# Change Analyst — change correlation playbook

Scope: rollout history, ReplicaSets, images, configuration changes; correlate by
timestamp and UID. Sequence is a lead, not proof.

## What to read
- Deployment revisions: ReplicaSets, their images/digests, replicas, age.
- Rollout state: progressing/complete/failed, progressDeadlineSeconds,
  unavailableReplicas, observedGeneration.
- Pod template hash changes and image tag→digest mapping.
- ConfigMap/Secret version changes referenced by the workload (metadata only).

## Correlation rules
- Correlate by resource UID and generation, not by name reuse.
- A change before the symptom is a lead; a change during the symptom window is
  stronger, but still needs a mechanism.
- Distinguish a deliberate scale-to-zero from a failed rollout: spec.replicas=0
  with a current image vs failed progress/unavailable replicas.
- Repeated restarts can follow node pauses/evictions; compare timestamps with
  node and kubelet events before blaming the app.

## Rollout strategies
- RollingUpdate: maxSurge/maxUnavailable, readiness gates, minReadySeconds.
- Canary/blue-green (e.g., Argo Rollouts): step weights, analysis runs,
  promotion gates, rollback to a stable ReplicaSet.
- Rollback is a human action; report the exact revision/image to roll back to.

## Sources (retrieved 2026-11-07)
- Kubernetes Deployments: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- kubectl rollout: https://kubernetes.io/docs/reference/kubectl/generated/kubectl_rollout/
- Argo Rollouts canary: https://argo-rollouts.readthedocs.io/en/stable/features/canary/
- Argo Rollouts blue-green: https://argo-rollouts.readthedocs.io/en/stable/features/bluegreen/
