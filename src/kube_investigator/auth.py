from __future__ import annotations

from dataclasses import dataclass

from kube_investigator.config import Settings
from kube_investigator.errors import AuthorizationError
from kube_investigator.models import Investigation, InvestigationRequest, Scope


@dataclass(frozen=True)
class Principal:
    id: str
    tenant: str
    cluster: str
    namespaces: frozenset[str]

    @classmethod
    def operator(cls, settings: Settings, *, subject: str = "local-operator") -> Principal:
        return cls(subject, settings.tenant, settings.cluster, frozenset(settings.namespaces))

    def authorize(self, request: InvestigationRequest) -> Scope:
        if request.cluster != self.cluster or request.namespace not in self.namespaces:
            raise AuthorizationError("Target is outside the operator's configured scope")
        return Scope(tenant=self.tenant, cluster=request.cluster, namespace=request.namespace)

    def can_read(self, investigation: Investigation) -> bool:
        return (
            investigation.principal_id == self.id
            and investigation.scope.tenant == self.tenant
            and investigation.scope.cluster == self.cluster
            and investigation.scope.namespace in self.namespaces
        )
