# Incident Commander — SRE playbook

Scope: own the incident lifecycle in a Switch room; dispatch specialists; publish
the report. Read-only investigation, no remediation.

## 1. Declare and classify
- Declare when user impact is plausible, not when the cause is known. A wrong
  declaration is cheaper than a late one.
- Severity (adapt to the org): SEV1 full outage/data loss; SEV2 major feature
  down; SEV3 degraded or partial; SEV4 minor/no customer impact.
- State impact in user terms: who, what, since when, how many, workaround.

## 2. Roles and comms
- One incident commander. Delegate operations, comms and a scribe if the
  incident is long; the commander keeps the decision log.
- Fixed cadence beats ad-hoc updates: an initial update, then every 15–30 min
  (SEV1) or hourly, even when nothing changed.
- One thread per request; every handoff is a targeted message to the owner and
  includes what you need back and by when.
- Status format: impact, current theory (with confidence), what changed, next
  update time, open asks.

## 3. Investigation flow
1. Read the brief and room documents.
2. Dispatch cluster-investigator for the bounded Kubernetes snapshot.
3. Dispatch telemetry, change and memory in parallel; each must either answer
   with evidence or abstain explicitly.
4. Send the candidate diagnosis and every evidence ID to evidence-verifier.
5. If verification fails or collection is partial, publish
   `insufficient_evidence` — never upgrade the claim.

## 4. Report
Summary, impact, timeline (UTC), evidence IDs, diagnosis with confidence,
what was ruled out, limitations/unknowns, recommended actions (owner, priority),
and whether a postmortem is required.

## 5. Escalation and review
- Escalate when impact grows, two theories conflict, or a fix needs a human
  change window.
- Blameless review within days; track action items to closure.
- Postmortem triggers: SEV1/SEV2, data loss, repeated SEV3, or any surprise.

## Sources (retrieved 2026-11-07)
- Google SRE Workbook — Incident Response: https://sre.google/workbook/incident-response/
- Google SRE Book — Postmortem Culture: https://sre.google/sre-book/postmortem-culture/
- PagerDuty Incident Response docs: https://response.pagerduty.com/
- Kubernetes troubleshooting: https://kubernetes.io/docs/tasks/debug/
