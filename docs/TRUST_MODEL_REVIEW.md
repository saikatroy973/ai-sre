# Switch Trust model-evaluation review

**Date:** 2026-11-04
**Scope:** the live `gpt-6-luna` model discovered from the `ai-sre-otel` LangChain/OpenTelemetry traffic.
**Decision owner:** AI SRE team. This is a technical review, not a formal risk acceptance.

## Finding

Trust evaluated `gpt-6-luna` (model `mdl_714beee7cba9ba1a04eb70f802ffeb6e`) after the first live
OTel interactions. The model row reported:

| Dimension | Score |
| --- | --- |
| Overall | 0.4457 |
| Jailbreak resistance | 0.5196 |
| Misuse resistance | 0.3967 |
| Robustness | 0.0000 |
| Security | 0.1130 |
| Toxicity | 0.7532 |

Trust raised two Critical findings and two Medium findings for that model:

- **Critical — Model robustness gap:** evaluation indicates gaps in model stability and
  reliability.
- **Critical — Model security risk:** evaluation indicates vulnerability to system exploits and
  code injection.
- Medium — model misuse risk.
- Medium — overall model health risk.

`scripts/trust_gate.py` correctly failed at its default `high` threshold with the two Critical
findings (exit code `5`). The gate was not edited, bypassed, or given an allowlist during this
review.

## Decision

`gpt-6-luna` is **not approved** for live Kubernetes investigation traffic. The robustness and
security scores are too low to treat the model as a safe default, regardless of the deterministic
verifier around the investigation workflow.

The live OTel probe model profile was changed to `claude-haiku-5-5`. Three verification
interactions were sent through the same read-only prompt and OpenTelemetry path. The Trust agent remains
`agn_1937c5178825ca80cb70982085181f68`; the model profile changed, not the investigation
authorization boundary.

`claude-haiku-5-5` has no documented Trust evaluation yet. Trust reports one **Informational**
“Missing model evaluation” finding for it. The severity gate does not fail Informational findings
at the `high` threshold, so this is a coverage gap, not a safety certification.

## Current Trust state

Immediately after the model change, Trust's model inventory contained three models:
`claude-haiku-5-5`, `gpt-4o-mini`, and `OpenAI Platform (auto)`. `gpt-6-luna` was no longer in
the current inventory, and the two Critical findings were no longer returned by the live issues
endpoint. The OTel agent has six total interactions (three on each model); Trust still reported
`$0.0001339` MTD cost because `claude-haiku-5-5` was not priced when checked. The current gate result is:

```text
Trust findings gate: threshold=high, total=1, failing=0
gate_exit=0
```

This must not be read as a fix for `gpt-6-luna`. The most likely explanation is inventory
retention/windowing: adding `claude-haiku-5-5` displaced the oldest model, so the model and its
findings left the current view. The finding remains recorded here and in the repository history.
Reintroducing `gpt-6-luna` can bring the Critical findings back until Trust re-evaluates the model
with acceptable scores.

## Actions and follow-ups

- [x] Review the Critical findings and the model evaluation scores.
- [x] Stop using `gpt-6-luna` for the live OTel probe.
- [x] Switch the profile to `claude-haiku-5-5` and verify one live interaction.
- [x] Leave the gate unchanged and unsuppressed.
- [ ] Obtain a Trust evaluation for `claude-haiku-5-5`; until then, treat it as provisional.
- [ ] Do not reintroduce `gpt-6-luna` without a fresh Trust evaluation showing no Critical
  findings.
- [ ] Add an owner/expiry exception process if a Critical finding ever has to be accepted.
- [ ] Re-check Trust inventory retention so an evicted model is not mistaken for remediation.

## Reproduction

The review used read-only Trust API calls:

```text
GET /api/v1/aispm-models/tenants/{tenant}/workspaces/{workspace}
GET /api/v1/aispm-models/tenants/{tenant}/workspaces/{workspace}/{model_id}/issues
GET /api/v1/issues/tenants/{tenant}/workspaces/{workspace}
```

The gate reproduction is documented in `README.md`; it needs only `SWITCH_TRUST_TOKEN` and
`SWITCH_TRUST_INSTANCE`. The live OTel path requires the full
`https://ingestion.switchagents.ai/agent/otel/v1/traces` signal URL and a LangChain/OpenAI
instrumented client.
