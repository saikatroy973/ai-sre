from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from kube_investigator.auth import Principal
from kube_investigator.config import Settings
from kube_investigator.coordinator import build_coordinator
from kube_investigator.errors import InvestigatorError, NotFoundError, SwitchError
from kube_investigator.models import Investigation, InvestigationRequest, utc_now
from kube_investigator.storage import Store
from kube_investigator.switch.memory import MemoryStore
from kube_investigator.switch.runtime import HttpSwitchRuntime, build_switch_runtime
from kube_investigator.switch.trust import (
    TrustClient,
    TrustSettings,
    render_governance_summary,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Read-only, evidence-backed Kubernetes investigations"
    )
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run a credential-free, offline fixture investigation")
    demo.add_argument(
        "--scenario",
        choices=("selector-mismatch", "healthy", "ambiguous"),
        default="selector-mismatch",
    )
    investigate = commands.add_parser("investigate", help="Investigate using configured adapters")
    for field in ("cluster", "namespace", "service", "deployment", "question"):
        investigate.add_argument(f"--{field}", required=True)
    show = commands.add_parser("show", help="Read one of this local operator's persisted reports")
    show.add_argument("id", type=UUID)
    for command in (demo, investigate, show):
        command.add_argument("--data-dir", type=Path)
        command.add_argument("--json", action="store_true", help="Print the investigation as JSON")

    switch_demo = commands.add_parser(
        "switch-demo",
        help="Run a fixture investigation in a real Switch room",
    )
    switch_demo.add_argument(
        "--scenario",
        choices=("selector-mismatch", "healthy", "ambiguous"),
        default="selector-mismatch",
    )
    switch_demo.add_argument("--data-dir", type=Path)
    switch_demo.add_argument("--state-dir", type=Path)
    switch_demo.add_argument("--json", action="store_true")

    switch = commands.add_parser("switch", help="Manage the Switch runtime")
    switch_commands = switch.add_subparsers(dest="switch_command", required=True)
    up = switch_commands.add_parser("up", help="Start the local Switch stack with Docker Compose")
    up.add_argument("--state-dir", type=Path)
    up.add_argument("--compose-file", type=Path)
    up.add_argument("--version", default="0.31.0")
    up.add_argument("--api-port", type=int, default=18000)
    up.add_argument("--gateway-port", type=int, default=13000)
    up.add_argument("--postgres-port", type=int, default=15432)
    up.add_argument("--no-gateway", action="store_true")
    down = switch_commands.add_parser("down", help="Stop the local Switch stack")
    down.add_argument("--state-dir", type=Path)
    down.add_argument("--compose-file", type=Path)
    down.add_argument("--volumes", action="store_true")
    status = switch_commands.add_parser("status", help="Check Switch and print room ids")
    status.add_argument("--state-dir", type=Path)
    status.add_argument("--json", action="store_true")
    bootstrap = switch_commands.add_parser(
        "bootstrap", help="Register the six agent identities and core rooms"
    )
    bootstrap.add_argument("--state-dir", type=Path)
    bootstrap.add_argument("--json", action="store_true")

    memory = switch_commands.add_parser("memory", help="Manage approved incident memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    approve = memory_commands.add_parser("approve", help="Approve a finished investigation")
    approve.add_argument("--investigation", type=UUID, required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--title", required=True)
    approve.add_argument("--summary")
    approve.add_argument("--expires-days", type=int)
    approve.add_argument("--supersedes", type=UUID)
    approve.add_argument("--allow-unsupported", action="store_true")
    approve.add_argument("--data-dir", type=Path)
    approve.add_argument("--state-dir", type=Path)
    approve.add_argument("--json", action="store_true")
    list_memory = memory_commands.add_parser("list", help="List approved records")
    list_memory.add_argument("--tenant")
    list_memory.add_argument("--cluster")
    list_memory.add_argument("--namespace")
    list_memory.add_argument("--include-superseded", action="store_true")
    list_memory.add_argument("--data-dir", type=Path)
    list_memory.add_argument("--json", action="store_true")
    search = memory_commands.add_parser("search", help="Search approved historical leads")
    search.add_argument("--query", required=True)
    search.add_argument("--tenant")
    search.add_argument("--cluster")
    search.add_argument("--namespace")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--data-dir", type=Path)
    search.add_argument("--json", action="store_true")

    trust = switch_commands.add_parser("trust", help="Read Switch Trust governance status")
    trust_commands = trust.add_subparsers(dest="trust_command", required=True)
    trust_status = trust_commands.add_parser(
        "status", help="Show Trust identity, guardrails, and ROI"
    )
    trust_status.add_argument("--agent-id", action="append", default=[])
    trust_status.add_argument("--json", action="store_true")
    trust_publish = trust_commands.add_parser(
        "publish", help="Write a Trust summary to the k8s-governance room"
    )
    trust_publish.add_argument("--agent-id", action="append", default=[])
    trust_publish.add_argument("--state-dir", type=Path)
    trust_publish.add_argument("--json", action="store_true")
    roi = switch_commands.add_parser("roi", help="Read Switch ROI cost summaries")
    roi.add_argument("--agent-id")
    roi.add_argument("--json", action="store_true")
    return root


def render(investigation: Investigation) -> str:
    report = investigation.report
    lines = [
        f"Investigation: {investigation.id}",
        f"Trace: {investigation.trace_id}",
        f"State: {investigation.state}",
    ]
    if investigation.switch_room_id:
        lines.append(
            f"Switch room: {investigation.switch_room_id} ({investigation.switch_room_name})"
        )
    if report is None:
        return "\n".join(lines)
    lines.extend(
        [f"Outcome: {report.outcome}", f"Diagnosis: {report.diagnosis}", "", report.summary]
    )
    for claim in report.verification.claims:
        lines.extend([f"- {claim.text}", f"  Evidence: {', '.join(map(str, claim.evidence_ids))}"])
    lines.extend(["", "Verification: " + ", ".join(report.verification.checks), "", "Next steps:"])
    lines.extend(f"- {step}" for step in report.next_steps)
    lines.extend(["", "Limitations:", *(f"- {item}" for item in report.limitations)])
    if report.historical_context:
        lines.extend(["", "Historical context (leads, not proof):"])
        for lead in report.historical_context:
            lines.append(
                f"- {lead.title} ({lead.record_id}), approved by {lead.approved_by}, "
                f"expires {lead.expires_at.date().isoformat()}"
            )
    if report.usage:
        lines.extend(["", f"Scale model usage: {report.usage.model_dump_json()}"])
    lines.extend(["", "Read-only: no cluster changes performed."])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    service = None
    runtime = None
    try:
        if args.command == "switch":
            return _switch_command(args)
        if args.command == "switch-demo":
            settings = switch_demo_settings(args)
            request = InvestigationRequest(
                question="Why is checkout failing?",
                cluster="demo-cluster",
                namespace="shop",
                service="checkout",
                deployment="checkout",
            )
        elif args.command == "demo":
            # A demo is always offline, even if live/Scale settings exist in the shell.
            settings = Settings(
                scenario=args.scenario,
                data_dir=args.data_dir or Path(os.getenv("INVESTIGATOR_DATA_DIR", ".data")),
            )
            request = InvestigationRequest(
                question="Why is checkout failing?",
                cluster="demo-cluster",
                namespace="shop",
                service="checkout",
                deployment="checkout",
            )
        else:
            settings = _settings_from_args(args)
            request = (
                InvestigationRequest(
                    **{
                        field: getattr(args, field)
                        for field in ("question", "cluster", "namespace", "service", "deployment")
                    }
                )
                if args.command == "investigate"
                else None
            )
        principal = Principal.operator(settings)
        if args.command == "show":
            # Reading an old report must not initialize Kubernetes auth plugins or models.
            store = Store(settings.data_dir)
            investigation = store.get(args.id)
            if not principal.can_read(investigation):
                raise NotFoundError("Investigation not found")
        else:
            service = build_coordinator(settings)
            store = service.store
            investigation = service.run(request, principal)
        if args.json:
            print(investigation.model_dump_json(indent=2))
        else:
            backend = "fixture" if args.command in {"demo", "switch-demo"} else settings.backend
            reasoner = (
                "deterministic" if args.command in {"demo", "switch-demo"} else settings.reasoner
            )
            print(f"Backend: {backend}; reasoner: {reasoner}")
            print("Agent handoffs:")
            for message in store.messages(investigation.id):
                print(f"  {message.sender} -> {message.recipient}: {message.type}")
            print()
            print(render(investigation))
        return 0
    except InvestigatorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValidationError:
        print("Error: invalid investigation parameters", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()
        if runtime is not None:
            runtime.close()


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "data_dir", None):
        settings = replace(settings, data_dir=args.data_dir)
    if getattr(args, "state_dir", None):
        settings = replace(settings, switch_state_dir=args.state_dir)
    return settings


def switch_demo_settings(args: argparse.Namespace) -> Settings:
    data_dir = args.data_dir or Path(os.getenv("INVESTIGATOR_DATA_DIR", ".data"))
    state_dir = args.state_dir or Path(
        os.getenv("INVESTIGATOR_SWITCH_STATE_DIR", str(data_dir / "switch"))
    )
    return Settings(
        backend="fixture",
        scenario=args.scenario,
        data_dir=data_dir,
        switch_mode="required",
        switch_base_url=os.getenv("INVESTIGATOR_SWITCH_BASE_URL", "http://127.0.0.1:18000"),
        switch_registration_token=os.getenv("INVESTIGATOR_SWITCH_REGISTRATION_TOKEN") or None,
        switch_state_dir=state_dir,
    )


def _switch_command(args: argparse.Namespace) -> int:
    if args.switch_command == "up":
        return _switch_up(args)
    if args.switch_command == "down":
        return _switch_down(args)
    if args.switch_command == "status":
        return _switch_status(args)
    if args.switch_command == "bootstrap":
        return _switch_bootstrap(args)
    if args.switch_command == "memory":
        return _switch_memory(args)
    if args.switch_command == "trust":
        return _switch_trust(args)
    if args.switch_command == "roi":
        return _switch_roi(args)
    raise AssertionError("unreachable")


def _switch_up(args: argparse.Namespace) -> int:
    compose = _compose_file(args)
    state_dir = _state_dir(args)
    env_file = _ensure_switch_env(
        state_dir,
        version=args.version,
        api_port=args.api_port,
        gateway_port=args.gateway_port,
        postgres_port=args.postgres_port,
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose),
    ]
    if not args.no_gateway:
        command.extend(["--profile", "gateway"])
    command.extend(["up", "-d"])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print("Error: docker compose could not start the Switch stack", file=sys.stderr)
        return result.returncode
    print(f"Switch stack requested. Environment: {env_file}")
    print(f"API: http://127.0.0.1:{args.api_port}")
    if not args.no_gateway:
        print(f"Gateway: http://127.0.0.1:{args.gateway_port}")
    print("Next: kube-investigator switch bootstrap")
    return 0


def _switch_down(args: argparse.Namespace) -> int:
    compose = _compose_file(args)
    state_dir = _state_dir(args)
    env_file = state_dir / ".env"
    if not env_file.exists():
        raise SwitchError("Switch environment file does not exist; nothing to stop")
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose),
        "--profile",
        "gateway",
        "down",
    ]
    if args.volumes:
        command.append("--volumes")
    return subprocess.run(command, check=False).returncode


def _switch_status(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    runtime = build_switch_runtime(settings)
    if runtime is None:
        payload = {"mode": "off"}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Switch mode: off")
        return 0
    try:
        healthy = runtime.health()
        payload = {
            "mode": runtime.mode,
            "base_url": settings.switch_base_url,
            "healthy": healthy,
            "agents": sorted(runtime.credentials),
            "rooms": runtime.room_ids,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Switch mode: {runtime.mode}")
            print(f"Base URL: {settings.switch_base_url}")
            print(f"Healthy: {healthy}")
            print("Agents: " + ", ".join(sorted(runtime.credentials)))
            for key, room_id in sorted(runtime.room_ids.items()):
                print(f"Room {key}: {room_id}")
        return 0 if healthy else 1
    finally:
        runtime.close()


def _switch_bootstrap(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if settings.switch_mode != "required":
        raise SwitchError("Switch is off; set INVESTIGATOR_SWITCH_MODE=required to bootstrap")
    runtime = HttpSwitchRuntime(settings, {})
    try:
        result = runtime.bootstrap()
        payload = {"agents": list(result.agents), "rooms": result.rooms}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("Registered agents: " + ", ".join(result.agents))
            for key, room_id in sorted(result.rooms.items()):
                print(f"Room {key}: {room_id}")
        return 0
    finally:
        runtime.close()


def _switch_trust(args: argparse.Namespace) -> int:
    trust_settings = TrustSettings.from_env(required=True)
    client = TrustClient(trust_settings)
    try:
        summary = client.governance_summary(agent_ids=tuple(args.agent_id))
        if args.trust_command == "status":
            if args.json:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                print(render_governance_summary(summary), end="")
            return 0
        runtime = build_switch_runtime(_settings_from_args(args))
        if runtime is None:
            raise SwitchError("Switch is off; a governance summary requires a Switch room")
        try:
            runtime.ensure_core_rooms()
            room_id = runtime.room_ids.get("governance")
            if not room_id:
                raise SwitchError("Switch governance room is not available")
            document = render_governance_summary(summary)
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            with runtime.open_session("incident-commander", room_id) as session:
                document_id = session.create_document(
                    name=f"trust-summary-{stamp}",
                    description="Read-only Switch Trust and ROI summary.",
                    instructions=(
                        "Governance evidence only. Re-read Trust before making a gate "
                        "decision; this snapshot does not prove a gate passed."
                    ),
                    content=document,
                )
                event_id = session.post(
                    f"[governance] Switch Trust summary published as trust-summary-{stamp}. "
                    "Read-only; ROI caps are advisory."
                )
        finally:
            runtime.close()
        payload = {
            "summary": summary,
            "switch_document_id": document_id,
            "switch_event_id": event_id,
            "switch_room_id": room_id,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(document, end="")
            print(f"\nSwitch governance room: {room_id}")
            print(f"Document: {document_id}")
        return 0
    finally:
        client.close()


def _switch_roi(args: argparse.Namespace) -> int:
    trust_settings = TrustSettings.from_env(required=True)
    client = TrustClient(trust_settings)
    try:
        summary = client.roi_summary(agent_id=args.agent_id)
    finally:
        client.close()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Month-to-date cost: {summary['total_mtd_cost']}")
        print(f"Cost cap: {summary['total_cost_cap']}")
        print(f"Agents with activity: {summary['agent_count']}")
        for agent in summary["agents"]:
            print(
                f"- {agent.get('agent_id')}: {agent.get('interaction_count')} interaction(s), "
                f"latest {agent.get('latest_interaction_time')}"
            )
        if args.agent_id:
            print(f"\nLatest analyses for {args.agent_id}:")
            print(json.dumps(summary["latest_analyses"], indent=2, sort_keys=True))
        print(f"\n{summary['advisory']}")
    return 0


def _switch_memory(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = Store(settings.data_dir)
    memory = MemoryStore(store)
    if args.memory_command == "approve":
        investigation = _authorized_investigation(store, settings, args.investigation)
        report = investigation.report
        if report is None:
            raise SwitchError("Investigation has no report to approve")
        summary = args.summary or _default_summary(investigation)
        days = args.expires_days or settings.switch_memory_default_expiry_days
        runtime = None
        published: dict[str, str] = {}
        if settings.switch_mode == "required":
            runtime = build_switch_runtime(settings)
            if runtime is None:
                raise SwitchError("Switch is required to approve memory")

        def publish(record):
            assert runtime is not None
            published["document_id"] = runtime.post_memory(record)

        try:
            record = memory.approve(
                investigation,
                report,
                reviewer=args.reviewer,
                title=args.title,
                summary=summary,
                expires_at=utc_now() + timedelta(days=days),
                supersedes=args.supersedes,
                allow_unsupported=args.allow_unsupported,
                publisher=publish if runtime is not None else None,
            )
        finally:
            if runtime is not None:
                runtime.close()
        document_id = published.get("document_id")
        payload = {
            "record": record.model_dump(mode="json"),
            "switch_document_id": document_id,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Approved memory record: {record.id}")
            print(f"Scope: {record.scope.tenant}/{record.scope.cluster}/{record.scope.namespace}")
            print(f"Expires: {record.expires_at.isoformat()}")
            if document_id:
                print(f"Switch memory document: {document_id}")
        return 0
    if args.memory_command == "list":
        scope = _scope_or_none(settings, args)
        records = memory.list(scope=scope, include_superseded=args.include_superseded)
        if args.json:
            print(
                json.dumps(
                    [record.model_dump(mode="json") for record in records],
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for record in records:
                print(
                    f"{record.id} [{record.status}] {record.title} "
                    f"({record.reviewer}, {record.approved_at.date().isoformat()})"
                )
        return 0
    if args.memory_command == "search":
        scope = _scope_or_none(settings, args)
        if scope is None:
            raise SwitchError("Memory search requires tenant, cluster, and namespace")
        leads = memory.search(scope=scope, query=args.query, limit=args.limit)
        if args.json:
            print(
                json.dumps(
                    [lead.model_dump(mode="json") for lead in leads],
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(memory.render_leads(leads))
        return 0
    raise AssertionError("unreachable")


def _authorized_investigation(
    store: Store, settings: Settings, investigation_id: UUID
) -> Investigation:
    investigation = store.get(investigation_id)
    principal = Principal.operator(settings)
    if not principal.can_read(investigation):
        raise NotFoundError("Investigation not found")
    return investigation


def _default_summary(investigation: Investigation) -> str:
    report = investigation.report
    assert report is not None
    claims = "\n".join(
        f"- {claim.text} Evidence: {', '.join(map(str, claim.evidence_ids))}"
        for claim in report.verification.claims
    )
    return f"{report.summary}\n\nClaims:\n{claims}\n\nLimitations:\n" + "\n".join(
        f"- {item}" for item in report.limitations
    )


def _scope_or_none(settings: Settings, args: argparse.Namespace):
    """The requested scope, defaulting to the configured single-namespace scope.

    Memory retrieval is always scope-bound; there is no cross-tenant listing
    mode, even for a local operator.
    """
    from kube_investigator.models import Scope

    namespace = args.namespace
    if namespace is None and len(settings.namespaces) == 1:
        namespace = settings.namespaces[0]
    if not (args.tenant or args.cluster or namespace):
        return None
    if not (namespace and (args.tenant or settings.tenant) and (args.cluster or settings.cluster)):
        return None
    return Scope(
        tenant=args.tenant or settings.tenant,
        cluster=args.cluster or settings.cluster,
        namespace=namespace,
    )


def _state_dir(args: argparse.Namespace) -> Path:
    return args.state_dir or Path(os.getenv("INVESTIGATOR_SWITCH_STATE_DIR", ".data/switch"))


def _compose_file(args: argparse.Namespace) -> Path:
    if args.compose_file:
        return args.compose_file
    candidate = Path(__file__).resolve().parents[2] / "deploy" / "switch" / "compose.yaml"
    if candidate.exists():
        return candidate
    return Path("deploy/switch/compose.yaml")


def _ensure_switch_env(
    state_dir: Path,
    *,
    version: str,
    api_port: int,
    gateway_port: int,
    postgres_port: int,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_file = state_dir / ".env"
    if env_file.exists():
        return env_file
    values = {
        "SWITCH_VERSION": version,
        "SWITCH_BIND_ADDR": "127.0.0.1",
        "API_HOST_PORT": str(api_port),
        "GATEWAY_HOST_PORT": str(gateway_port),
        "POSTGRES_HOST_PORT": str(postgres_port),
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_USER": "switch_app",
        "DB_PASSWORD": secrets.token_hex(24),
        "DB_OWNER_USER": "postgres",
        "DB_OWNER_PASSWORD": secrets.token_hex(24),
        "DB_NAME": "switch",
        "ID_SERVER_NAME": "localhost",
        "AGENT_REGISTRATION_TOKEN": secrets.token_hex(32),
        "SECRET_KEYS": f"local:{secrets.token_hex(32)}",
        "GATEWAY_ADMIN_EMAIL": "admin@switch.local",
        "GATEWAY_ADMIN_PASSWORD": secrets.token_hex(24),
        "FRONTEND_BASE_URL": f"http://localhost:{gateway_port}",
        "GATEWAY_PUBLIC_URL": f"http://localhost:{api_port}",
        "GATEWAY_COOKIE_SECURE": "false",
        "TELEMETRY_ENABLED": "false",
    }
    temporary = env_file.with_name(env_file.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    os.replace(temporary, env_file)
    os.chmod(env_file, 0o600)
    return env_file
