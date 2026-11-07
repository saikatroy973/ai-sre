from __future__ import annotations

from threading import BoundedSemaphore
from typing import Any
from uuid import UUID, uuid4

from ai_sre.auth import Principal
from ai_sre.collectors import Collector, FixtureCollector, KubernetesCollector
from ai_sre.config import Settings
from ai_sre.errors import (
    AuthorizationError,
    BusyError,
    InvestigatorError,
    NotFoundError,
    ReasoningError,
)
from ai_sre.messaging import MessageBus
from ai_sre.models import (
    AgentMessage,
    AgentRole,
    Candidate,
    HistoricalLead,
    Investigation,
    InvestigationRequest,
    InvestigationState,
    Proposal,
    Report,
)
from ai_sre.reasoners import DeterministicReasoner, Reasoner, ScaleReasoner
from ai_sre.storage import Store
from ai_sre.switch.memory import MemoryStore
from ai_sre.switch.room import SwitchRoomRecorder
from ai_sre.switch.runtime import (
    SwitchRuntime,
    build_switch_runtime,
)
from ai_sre.telemetry import configure_tracing, span
from ai_sre.verification import EvidenceVerifier


class Coordinator:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        collector: Collector,
        reasoner: Reasoner,
        *,
        switch: SwitchRuntime | None = None,
        memory: MemoryStore | None = None,
    ):
        self.settings = settings
        self.store = store
        self.collector = collector
        self.reasoner = reasoner
        self.switch = switch
        self.memory = memory
        self.verifier = EvidenceVerifier(settings)
        self.bus = MessageBus(store)
        self.slots = BoundedSemaphore(settings.max_concurrent_investigations)

    def run(self, request: InvestigationRequest, principal: Principal) -> Investigation:
        scope = principal.authorize(request)
        if (
            scope.tenant != self.settings.tenant
            or scope.cluster != self.settings.cluster
            or scope.namespace not in self.settings.namespaces
        ):
            raise AuthorizationError("Target is outside the server's configured scope")
        if not self.slots.acquire(blocking=False):
            raise BusyError("Investigation capacity reached; retry later")
        try:
            return self._run(request, principal)
        finally:
            self.slots.release()

    def _run(self, request: InvestigationRequest, principal: Principal) -> Investigation:
        investigation = Investigation(
            principal_id=principal.id, scope=principal.authorize(request), request=request
        )
        self.store.create(investigation)
        recorder: SwitchRoomRecorder | None = None
        try:
            investigation = self.store.transition(investigation, InvestigationState.AUTHORIZED)
            investigation, recorder = self._open_switch_room(investigation)
            investigation = self.store.transition(investigation, InvestigationState.COLLECTING)
            investigator_task = uuid4()
            dispatched = self._send(
                investigation,
                recorder,
                sender=AgentRole.COORDINATOR,
                recipient=AgentRole.INVESTIGATOR,
                task_id=investigator_task,
                parent_task_id=investigation.id,
                parent_span_id=None,
                payload={"request": request.model_dump(mode="json"), "max_tool_calls": 5},
            )
            leads = self._retrieve_memory(investigation, recorder)
            self._specialist_abstentions(investigation, recorder)
            with span(
                "investigation.collect",
                investigation_id=str(investigation.id),
                trace_id=investigation.trace_id,
                cluster=investigation.scope.cluster,
                namespace=investigation.scope.namespace,
            ):
                for evidence in self.collector.collect(investigation):
                    self.store.append_evidence(evidence)
            evidence = self.store.evidence(investigation.id)
            if recorder is not None:
                recorder.evidence_summary(evidence)
            investigation = self.store.transition(investigation, InvestigationState.ANALYZING)
            reasoning_error = None
            collection_incomplete = any(
                item.status != "ok" or not item.query.complete for item in evidence
            )
            try:
                # Do not pay for reasoning over a known-incomplete collection.
                with span(
                    "investigation.reason",
                    investigation_id=str(investigation.id),
                    trace_id=investigation.trace_id,
                    reasoner=self.settings.reasoner,
                ):
                    proposal = (
                        Proposal(candidate=Candidate(category="undetermined", evidence_ids=[]))
                        if collection_incomplete
                        else self.reasoner.propose(request, evidence)
                    )
            except ReasoningError as exc:
                reasoning_error = exc.code
                proposal = Proposal(
                    candidate=Candidate(category="undetermined", evidence_ids=[]), usage=exc.usage
                )
            response = self._send(
                investigation,
                recorder,
                sender=AgentRole.INVESTIGATOR,
                recipient=AgentRole.COORDINATOR,
                task_id=investigator_task,
                parent_task_id=investigation.id,
                parent_span_id=dispatched.span_id,
                payload={
                    **proposal.model_dump(mode="json"),
                    "reasoning_error": reasoning_error,
                    "reasoning_skipped": "incomplete_collection" if collection_incomplete else None,
                    "collected_evidence_ids": [str(item.id) for item in evidence],
                },
            )
            investigation = self.store.transition(investigation, InvestigationState.VERIFYING)
            verification_task = uuid4()
            verification_request = self._send(
                investigation,
                recorder,
                sender=AgentRole.COORDINATOR,
                recipient=AgentRole.VERIFIER,
                task_id=verification_task,
                parent_task_id=investigation.id,
                parent_span_id=response.span_id,
                payload={"candidate": proposal.candidate.model_dump(mode="json")},
            )
            # Reload persisted evidence, not an agent's reconstructed description of it.
            with span(
                "investigation.verify",
                investigation_id=str(investigation.id),
                trace_id=investigation.trace_id,
                candidate=proposal.candidate.category,
            ):
                verification = self.verifier.verify(
                    investigation, proposal.candidate, self.store.evidence(investigation.id)
                )
            verification_response = self._send(
                investigation,
                recorder,
                sender=AgentRole.VERIFIER,
                recipient=AgentRole.COORDINATOR,
                task_id=verification_task,
                parent_task_id=investigation.id,
                parent_span_id=verification_request.span_id,
                payload=verification.model_dump(mode="json"),
            )
            if recorder is not None:
                recorder.verification(verification)
            incomplete = reasoning_error is not None or collection_incomplete
            supported = verification.passed and not incomplete
            limitations = [
                "Fixture data; no cluster was contacted."
                if self.settings.backend == "fixture"
                else "Cluster reads are time-bound observations, not an atomic snapshot.",
                "Only selector mismatches affecting the named Deployment are supported.",
                "This does not establish rollout timing or exclude other outage causes.",
            ]
            if self.switch is not None:
                limitations.append(
                    "Telemetry and change-correlation identities are present in the Switch "
                    "room, but no live logs, metrics, or repository-change source is "
                    "configured; they abstained."
                )
            if leads:
                limitations.append(
                    "Historical memory was retrieved as leads only and was not used as "
                    "evidence or passed to the reasoner."
                )
            report = Report(
                outcome="partial"
                if incomplete
                else "supported"
                if supported
                else "insufficient_evidence",
                diagnosis="service_selector_mismatch" if supported else "undetermined",
                summary=(
                    "Evidence supports a Service selector mismatch excluding the specified "
                    "ready workload; no ready Service endpoints were observed."
                    if supported
                    else "Some collection or reasoning steps did not complete. No root cause "
                    "was established. Review the handoffs and evidence errors."
                    if incomplete
                    else "Insufficient evidence to establish the supported selector-mismatch "
                    "diagnosis. This does not mean the service is healthy."
                ),
                verification=verification,
                next_steps=(
                    [
                        "Review the Service selector against the intended Deployment labels.",
                        "Have an authorized operator review any correction through the normal "
                        "change process, then investigate again. No changes were performed.",
                    ]
                    if supported
                    else [
                        "Review collection and verification checks; confirm the explicit "
                        "Service/Deployment target and read permissions.",
                        "Gather additional logs, events, metrics, or deployment history with "
                        "authorized tools. Do not infer a memory leak or rollout cause.",
                    ]
                ),
                limitations=limitations,
                usage=proposal.usage,
                historical_context=leads,
            )
            self._send(
                investigation,
                recorder,
                sender=AgentRole.COORDINATOR,
                recipient=AgentRole.OPERATOR,
                task_id=investigation.id,
                parent_task_id=None,
                parent_span_id=verification_response.span_id,
                payload={"outcome": report.outcome, "diagnosis": report.diagnosis},
            )
            if recorder is not None:
                recorder.final_report(report)
                recorder.close()
                recorder = None
            return self.store.transition(investigation, InvestigationState.REPORTED, report=report)
        except InvestigatorError:
            self._mark_failed(investigation)
            raise
        except Exception:
            self._mark_failed(investigation)
            raise InvestigatorError("Investigation could not complete") from None
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass

    def _open_switch_room(
        self, investigation: Investigation
    ) -> tuple[Investigation, SwitchRoomRecorder | None]:
        if self.switch is None:
            return investigation, None
        room = self.switch.create_incident_room(investigation)
        investigation = self.store.bind_switch_room(
            investigation,
            room_id=room.id,
            room_name=room.name,
            base_url=room.base_url,
        )
        recorder = SwitchRoomRecorder(
            self.switch,
            investigation,
            room,
            history_limit=self.settings.switch_room_history_limit,
        )
        recorder.brief()
        return investigation, recorder

    def _retrieve_memory(
        self, investigation: Investigation, recorder: SwitchRoomRecorder | None
    ) -> list[HistoricalLead]:
        if self.memory is None or self.switch is None:
            return []
        request = self._send(
            investigation,
            recorder,
            sender=AgentRole.COORDINATOR,
            recipient=AgentRole.MEMORY_RESEARCHER,
            task_id=uuid4(),
            parent_task_id=investigation.id,
            parent_span_id=None,
            payload={
                "scope": investigation.scope.model_dump(mode="json"),
                "query": investigation.request.question,
                "max_records": self.settings.switch_memory_max_records,
                "policy": "historical_leads_are_not_evidence",
            },
        )
        leads = self.memory.search(
            scope=investigation.scope,
            query=(
                f"{investigation.request.question} {investigation.request.service} "
                f"{investigation.request.deployment}"
            ),
            limit=self.settings.switch_memory_max_records,
        )
        self._send(
            investigation,
            recorder,
            sender=AgentRole.MEMORY_RESEARCHER,
            recipient=AgentRole.COORDINATOR,
            task_id=request.task_id,
            parent_task_id=investigation.id,
            parent_span_id=request.span_id,
            payload={
                "record_ids": [str(lead.record_id) for lead in leads],
                "count": len(leads),
                "used_as_evidence": False,
                "policy": "historical_leads_are_not_evidence",
            },
        )
        if recorder is not None:
            recorder.memory_context(leads, self.memory.render_leads(leads))
        return leads

    def _specialist_abstentions(
        self, investigation: Investigation, recorder: SwitchRoomRecorder | None
    ) -> None:
        if self.switch is None:
            return
        for role, reason in (
            (
                AgentRole.TELEMETRY_ANALYST,
                "no live logs, events, or metrics source is configured",
            ),
            (
                AgentRole.CHANGE_ANALYST,
                "no live rollout-history or repository-change source is configured",
            ),
        ):
            request = self._send(
                investigation,
                recorder,
                sender=AgentRole.COORDINATOR,
                recipient=role,
                task_id=uuid4(),
                parent_task_id=investigation.id,
                parent_span_id=None,
                payload={
                    "task": "correlate current incident",
                    "sources_configured": False,
                    "reason_expected": reason,
                },
            )
            self._send(
                investigation,
                recorder,
                sender=role,
                recipient=AgentRole.COORDINATOR,
                task_id=request.task_id,
                parent_task_id=investigation.id,
                parent_span_id=request.span_id,
                payload={
                    "status": "abstained",
                    "reason": reason,
                    "evidence_ids": [],
                },
            )

    def _send(
        self,
        investigation: Investigation,
        recorder: SwitchRoomRecorder | None,
        *,
        sender: AgentRole,
        recipient: AgentRole,
        task_id: UUID,
        parent_task_id: UUID | None,
        parent_span_id: str | None,
        payload: dict[str, Any],
    ) -> AgentMessage:
        message = self.bus.send(
            investigation,
            sender=sender,
            recipient=recipient,
            task_id=task_id,
            parent_task_id=parent_task_id,
            parent_span_id=parent_span_id,
            payload=payload,
        )
        if recorder is not None:
            recorder.handoff(message)
        return message

    def _mark_failed(self, investigation: Investigation) -> None:
        current = self.store.get(investigation.id)
        if current.state not in {InvestigationState.REPORTED, InvestigationState.FAILED}:
            self.store.transition(current, InvestigationState.FAILED, error_code="internal_error")

    def get(self, investigation_id: UUID, principal: Principal) -> Investigation:
        investigation = self.store.get(investigation_id)
        if not principal.can_read(investigation):
            # Do not disclose another principal's investigation IDs.
            raise NotFoundError("Investigation not found")
        return investigation

    def close(self) -> None:
        try:
            if self.switch is not None:
                self.switch.close()
        finally:
            try:
                self.collector.close()
            finally:
                self.reasoner.close()


def build_coordinator(settings: Settings) -> Coordinator:
    configure_tracing("ai-sre")
    store = Store(settings.data_dir)
    switch = build_switch_runtime(settings)
    memory = MemoryStore(store) if switch is not None else None
    collector = (
        FixtureCollector(settings)
        if settings.backend == "fixture"
        else KubernetesCollector(settings)
    )
    reasoner = ScaleReasoner(settings) if settings.reasoner == "scale" else DeterministicReasoner()
    return Coordinator(
        settings,
        store,
        collector,
        reasoner,
        switch=switch,
        memory=memory,
    )
