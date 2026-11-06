# Cluster Investigator — Kubernetes triage playbook

Scope: read-only Kubernetes state: nodes, pods, Deployments, ReplicaSets,
Services, EndpointSlices, events. Cite object name/namespace and timestamps.

## Triage order
1. Nodes: Ready, MemoryPressure/DiskPressure/PIDPressure, kubelet version.
2. Pods: phase, Ready condition, restart count, waiting reason, last
   termination (OOMKilled, Error, Completed).
3. Workloads: Deployment desired vs ready vs available, progressDeadline,
   ReplicaSet revisions, images (prefer digests).
4. Services and endpoints: selector, EndpointSlices/Endpoints, port/targetPort.
5. Events: Warning reasons, lastTimestamp, involved object.

## Common signatures
- Pending: unschedulable — requests vs allocatable, taints/tolerations,
  nodeSelector/affinity, PVC unbound, quota.
- CrashLoopBackOff: app exits or probes fail — read previous logs, exit code,
  config/secret mounts, dependency readiness.
- ImagePullBackOff / ErrImagePull: image name/tag/digest, registry auth.
- OOMKilled: limits vs working set; check sidecars and JVM heap.
- Evicted: node pressure/ephemeral storage; check priority and disruption.
- Service has no endpoints: selector matches no ready pod (labels, namespace,
  readiness probe, port name mismatch), or EndpointSlice not created.
- DNS failures: CoreDNS pods/Service, NetworkPolicy, ndots/search domains.

## Evidence rules
- One claim = one object read plus timestamp; include resource kind/name/ns.
- Distinguish current state from historical restarts (restart counts and
  lastState are history, not current impact).
- Never infer a cause from a single metric; state what is not observed.

## Sources (retrieved 2026-11-07)
- Debug running Pods: https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/
- Pod lifecycle: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- Service: https://kubernetes.io/docs/concepts/services-networking/service/
- EndpointSlices: https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/
- Probes: https://kubernetes.io/docs/concepts/workloads/pods/probes/
- Deployments: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
