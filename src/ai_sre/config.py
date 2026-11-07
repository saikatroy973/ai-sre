from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from ai_sre.errors import ConfigurationError
from ai_sre.models import ClusterName, ResourceName


@dataclass(frozen=True)
class Settings:
    backend: str = "fixture"
    scenario: str = "selector-mismatch"
    data_dir: Path = Path(".data")
    evidence_store_dir_override: Path | None = None
    tenant: str = "local"
    cluster: str = "demo-cluster"
    namespaces: tuple[str, ...] = ("shop",)
    api_token: str | None = field(default=None, repr=False)
    kube_context: str | None = None
    reasoner: str = "deterministic"
    sgp_api_base_url: str | None = None
    sgp_api_key: str | None = field(default=None, repr=False)
    sgp_account_id: str | None = None
    sgp_model: str | None = None
    max_resources: int = 100
    max_concurrent_investigations: int = 4
    max_evidence_age_seconds: int = 300
    tool_timeout_seconds: int = 10
    model_timeout_seconds: int = 20
    max_model_output_tokens: int = 768
    switch_mode: str = "off"
    switch_base_url: str = "http://127.0.0.1:18000"
    switch_registration_token: str | None = field(default=None, repr=False)
    switch_state_dir: Path = Path(".data/switch")
    switch_request_timeout_seconds: float = 10.0
    switch_memory_max_records: int = 5
    switch_memory_default_expiry_days: int = 180
    switch_room_history_limit: int = 100

    @property
    def evidence_store_dir(self) -> Path:
        """Where persisted investigations live; defaults to the normal data dir.

        ``AI_SRE_EVIDENCE_STORE_DIR`` overrides it for isolated runtimes
        that mount a staged, read-only snapshot of the evidence store.
        """
        return self.evidence_store_dir_override or self.data_dir

    def __post_init__(self) -> None:
        if (
            self.evidence_store_dir_override is not None
            and not str(self.evidence_store_dir_override).strip()
        ):
            raise ConfigurationError("AI_SRE_EVIDENCE_STORE_DIR must be a nonempty path")
        if self.backend not in {"fixture", "kubernetes"}:
            raise ConfigurationError("AI_SRE_BACKEND must be fixture or kubernetes")
        if self.scenario not in {"selector-mismatch", "healthy", "ambiguous"}:
            raise ConfigurationError("Unknown fixture scenario")
        if self.reasoner not in {"deterministic", "scale"}:
            raise ConfigurationError("AI_SRE_REASONER must be deterministic or scale")
        if self.switch_mode not in {"required", "off"}:
            raise ConfigurationError("AI_SRE_SWITCH_MODE must be required or off")
        try:
            TypeAdapter(ClusterName).validate_python(self.cluster)
            for namespace in self.namespaces:
                TypeAdapter(ResourceName).validate_python(namespace)
        except ValidationError as exc:
            raise ConfigurationError("Invalid configured cluster or namespace") from exc
        if not self.namespaces or not self.tenant.strip():
            raise ConfigurationError("At least one namespace and a nonempty tenant are required")
        if self.backend == "kubernetes" and not self.kube_context:
            raise ConfigurationError("Live reads require an explicit AI_SRE_KUBE_CONTEXT")
        if (
            min(
                self.max_resources,
                self.max_concurrent_investigations,
                self.max_evidence_age_seconds,
                self.tool_timeout_seconds,
                self.model_timeout_seconds,
                self.max_model_output_tokens,
                self.switch_memory_max_records,
                self.switch_memory_default_expiry_days,
                self.switch_room_history_limit,
            )
            < 1
        ):
            raise ConfigurationError("Limits must be positive")
        if self.switch_request_timeout_seconds <= 0:
            raise ConfigurationError("Switch request timeout must be positive")
        parsed = urlsplit(self.switch_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ConfigurationError("AI_SRE_SWITCH_BASE_URL must be an HTTP(S) origin")
        if self.reasoner == "scale":
            if not all(
                (self.sgp_api_base_url, self.sgp_api_key, self.sgp_account_id, self.sgp_model)
            ):
                raise ConfigurationError(
                    "Scale reasoning requires SGP_API_BASE_URL, SGP_API_KEY, "
                    "SGP_ACCOUNT_ID, and SGP_MODEL"
                )
            parsed = urlsplit(self.sgp_api_base_url or "")
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ConfigurationError("SGP_API_BASE_URL must be an HTTPS API URL")
            if "/" not in (self.sgp_model or ""):
                raise ConfigurationError("SGP_MODEL must be an enabled vendor/model ID")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=os.getenv("AI_SRE_BACKEND", "fixture"),
            scenario=os.getenv("AI_SRE_SCENARIO", "selector-mismatch"),
            data_dir=Path(os.getenv("AI_SRE_DATA_DIR", ".data")),
            evidence_store_dir_override=(
                Path(os.getenv("AI_SRE_EVIDENCE_STORE_DIR"))
                if (os.getenv("AI_SRE_EVIDENCE_STORE_DIR") or "").strip()
                else None
            ),
            tenant=os.getenv("AI_SRE_TENANT", "local"),
            cluster=os.getenv("AI_SRE_CLUSTER", "demo-cluster"),
            namespaces=tuple(
                part.strip()
                for part in os.getenv("AI_SRE_NAMESPACES", "shop").split(",")
                if part.strip()
            ),
            api_token=os.getenv("AI_SRE_API_TOKEN") or None,
            kube_context=os.getenv("AI_SRE_KUBE_CONTEXT") or None,
            reasoner=os.getenv("AI_SRE_REASONER", "deterministic"),
            sgp_api_base_url=os.getenv("SGP_API_BASE_URL") or None,
            sgp_api_key=os.getenv("SGP_API_KEY") or None,
            sgp_account_id=os.getenv("SGP_ACCOUNT_ID") or None,
            sgp_model=os.getenv("SGP_MODEL") or None,
            switch_mode=os.getenv("AI_SRE_SWITCH_MODE", "required"),
            switch_base_url=os.getenv("AI_SRE_SWITCH_BASE_URL", "http://127.0.0.1:18000"),
            switch_registration_token=os.getenv("AI_SRE_SWITCH_REGISTRATION_TOKEN") or None,
            switch_state_dir=Path(
                os.getenv(
                    "AI_SRE_SWITCH_STATE_DIR",
                    str(Path(os.getenv("AI_SRE_DATA_DIR", ".data")) / "switch"),
                )
            ),
            switch_request_timeout_seconds=_env_float(
                "AI_SRE_SWITCH_REQUEST_TIMEOUT_SECONDS", 10.0
            ),
            switch_memory_max_records=_env_int("AI_SRE_SWITCH_MEMORY_MAX_RECORDS", 5),
            switch_memory_default_expiry_days=_env_int(
                "AI_SRE_SWITCH_MEMORY_EXPIRY_DAYS", 180
            ),
            switch_room_history_limit=_env_int("AI_SRE_SWITCH_ROOM_HISTORY_LIMIT", 100),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
