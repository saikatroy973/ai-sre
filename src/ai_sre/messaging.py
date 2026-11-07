from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from ai_sre.models import AgentMessage, AgentRole, Investigation
from ai_sre.storage import Store

_ALLOWED = {
    (AgentRole.COORDINATOR, AgentRole.INVESTIGATOR): "task_request",
    (AgentRole.INVESTIGATOR, AgentRole.COORDINATOR): "task_result",
    (AgentRole.COORDINATOR, AgentRole.TELEMETRY_ANALYST): "task_request",
    (AgentRole.TELEMETRY_ANALYST, AgentRole.COORDINATOR): "task_result",
    (AgentRole.COORDINATOR, AgentRole.CHANGE_ANALYST): "task_request",
    (AgentRole.CHANGE_ANALYST, AgentRole.COORDINATOR): "task_result",
    (AgentRole.COORDINATOR, AgentRole.MEMORY_RESEARCHER): "task_request",
    (AgentRole.MEMORY_RESEARCHER, AgentRole.COORDINATOR): "task_result",
    (AgentRole.COORDINATOR, AgentRole.VERIFIER): "verification_request",
    (AgentRole.VERIFIER, AgentRole.COORDINATOR): "verification_result",
    (AgentRole.COORDINATOR, AgentRole.OPERATOR): "report",
}


class MessageBus:
    """Internal, allowlisted handoffs. This is not a public agent-message ingress."""

    def __init__(self, store: Store):
        self.store = store

    def send(
        self,
        investigation: Investigation,
        *,
        sender: AgentRole,
        recipient: AgentRole,
        task_id: UUID,
        parent_task_id: UUID | None,
        parent_span_id: str | None,
        payload: dict[str, Any],
    ) -> AgentMessage:
        message_type = _ALLOWED.get((sender, recipient))
        if message_type is None:
            raise ValueError("Agent handoff is not allowed")
        message = AgentMessage(
            investigation_id=investigation.id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            sender=sender,
            recipient=recipient,
            type=message_type,
            trace_id=investigation.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=parent_span_id,
            payload=payload,
        )
        self.store.append_message(message)
        return message
