from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from kube_investigator.auth import Principal
from kube_investigator.config import Settings
from kube_investigator.coordinator import build_coordinator
from kube_investigator.errors import (
    ConfigurationError,
    InvestigatorError,
    NotFoundError,
    SwitchError,
)
from kube_investigator.models import Investigation, InvestigationRequest, utc_now
from kube_investigator.storage import Store
from kube_investigator.switch.console import ConsoleClient, ConsoleSettings
from kube_investigator.switch.memory import MemoryStore
from kube_investigator.switch.runtime import HttpSwitchRuntime, build_switch_runtime
from kube_investigator.switch.specs import (
    AGENTS,
    CORE_ROOMS,
    INCIDENT_KIT_NAME,
    console_instructions,
    core_room_template,
    core_room_yaml,
    incident_kit_documents,
    incident_room_template,
)
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

    queue = commands.add_parser("queue", help="Durable local incident queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_add = queue_commands.add_parser("add", help="Persist an incident request")
    for field in ("question", "cluster", "namespace", "service", "deployment"):
        queue_add.add_argument(f"--{field}", required=True)
    queue_add.add_argument("--data-dir", type=Path)
    queue_add.add_argument("--json", action="store_true")
    queue_list = queue_commands.add_parser("list", help="List queued incidents")
    queue_list.add_argument(
        "--status",
        choices=("pending", "running", "complete", "failed"),
    )
    queue_list.add_argument("--data-dir", type=Path)
    queue_list.add_argument("--json", action="store_true")
    queue_retry = queue_commands.add_parser("retry", help="Return a failed job to pending")
    queue_retry.add_argument("--id", type=UUID, required=True)
    queue_retry.add_argument("--data-dir", type=Path)
    queue_retry.add_argument("--json", action="store_true")
    queue_run = queue_commands.add_parser(
        "run", help="Run pending incidents with the configured adapters"
    )
    queue_run.add_argument("--once", action="store_true", help="Run at most one job")
    queue_run.add_argument("--data-dir", type=Path)
    queue_run.add_argument("--json", action="store_true")

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
    up.add_argument(
        "--collab", action="store_true", help="Start the Mattermost collaboration profile"
    )
    down = switch_commands.add_parser("down", help="Stop the local Switch stack")
    down.add_argument("--state-dir", type=Path)
    down.add_argument("--compose-file", type=Path)
    down.add_argument("--collab", action="store_true")
    down.add_argument("--volumes", action="store_true")
    status = switch_commands.add_parser("status", help="Check Switch and print room ids")
    status.add_argument("--state-dir", type=Path)
    status.add_argument("--json", action="store_true")
    bootstrap = switch_commands.add_parser(
        "bootstrap", help="Register the six agent identities and core rooms"
    )
    bootstrap.add_argument("--state-dir", type=Path)
    bootstrap.add_argument("--json", action="store_true")

    console = switch_commands.add_parser(
        "console", help="Use the Switch Console-managed local server and agents"
    )
    console_commands = console.add_subparsers(dest="console_command", required=True)
    console_status = console_commands.add_parser(
        "status", help="Show Console controllers, agents, and rooms"
    )
    console_status.add_argument("--json", action="store_true")
    console_setup = console_commands.add_parser(
        "setup", help="Create or verify the six Console-managed agents and core rooms"
    )
    console_setup.add_argument("--directory", type=Path, default=Path.cwd())
    console_setup.add_argument("--provider", default="opencode")
    console_setup.add_argument("--controller-id")
    console_setup.add_argument(
        "--refresh", action="store_true", help="Update existing agent instructions"
    )
    console_setup.add_argument(
        "--auto-mode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="OpenCode Auto mode; use --no-auto-mode to require approvals in Console",
    )
    console_setup.add_argument("--json", action="store_true")
    console_incident = console_commands.add_parser(
        "incident", help="Create a Console incident room and kick off the commander"
    )
    console_incident.add_argument("--namespace", default="shop")
    console_incident.add_argument("--service", default="checkout")
    console_incident.add_argument("--deployment", default="checkout")
    console_incident.add_argument("--cluster", default="demo-cluster")
    console_incident.add_argument("--question", default="Why is checkout failing?")
    console_incident.add_argument(
        "--scenario",
        choices=("selector-mismatch", "healthy", "ambiguous"),
        default="selector-mismatch",
    )
    console_incident.add_argument("--json", action="store_true")

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
    trust_publish.add_argument(
        "--console",
        action="store_true",
        help="Publish into the Console-managed k8s-governance room",
    )
    trust_publish.add_argument("--json", action="store_true")
    trust_scan = trust_commands.add_parser(
        "scan", help="Run the Switch Trust CLI scan on agent code"
    )
    trust_scan.add_argument("--path", default=".")
    trust_scan.add_argument("--format", choices=("json", "sarif"), default="json")
    trust_scan.add_argument("--output")
    trust_scan.add_argument("--log")
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
        if args.command == "queue":
            return _queue_command(args)
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


def _queue_command(args: argparse.Namespace) -> int:
    data_dir = args.data_dir or Path(os.getenv("INVESTIGATOR_DATA_DIR", ".data"))
    store = Store(data_dir)
    if args.queue_command == "add":
        request = InvestigationRequest(
            question=args.question,
            cluster=args.cluster,
            namespace=args.namespace,
            service=args.service,
            deployment=args.deployment,
        )
        job_id = store.enqueue(request)
        if args.json:
            print(
                json.dumps(
                    {"queue_id": str(job_id), "status": "pending"},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Queued investigation: {job_id}")
        return 0
    if args.queue_command == "list":
        jobs = store.queue_jobs(status=args.status)
        if args.json:
            print(json.dumps(jobs, indent=2, sort_keys=True))
        else:
            for job in jobs:
                request = job["request"]
                print(
                    f"{job['id']} [{job['status']}] attempts={job['attempts']} "
                    f"{request['namespace']}/{request['service']}: {request['question']}"
                )
                if job["error"]:
                    print(f"  error: {job['error']}")
        return 0
    if args.queue_command == "retry":
        store.retry_job(args.id)
        if args.json:
            print(json.dumps({"queue_id": str(args.id), "status": "pending"}, indent=2))
        else:
            print(f"Re-queued investigation: {args.id}")
        return 0
    if args.queue_command == "run":
        return _run_queue(args, store)
    raise AssertionError("unreachable")


def _run_queue(args: argparse.Namespace, store: Store) -> int:
    settings = _settings_from_args(args)
    coordinator = build_coordinator(settings)
    results: list[dict[str, object]] = []
    worker_id = uuid4().hex
    try:
        store.recover_expired()
        while True:
            job = store.claim_next(worker_id=worker_id)
            if job is None:
                break
            try:
                investigation = coordinator.run(job.request, Principal.operator(settings))
                store.complete_job(job.id, investigation.id, worker_id=worker_id)
                results.append(
                    {
                        "queue_id": str(job.id),
                        "status": "complete",
                        "investigation_id": str(investigation.id),
                    }
                )
            except InvestigatorError as exc:
                store.fail_job(job.id, str(exc), worker_id=worker_id)
                results.append(
                    {
                        "queue_id": str(job.id),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            if args.once:
                break
    finally:
        coordinator.close()
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    elif results:
        for result in results:
            print(
                f"{result['queue_id']}: {result['status']}"
                + (
                    f" ({result['investigation_id']})"
                    if "investigation_id" in result
                    else f" ({result.get('error')})"
                )
            )
    else:
        print("No pending queue jobs.")
    return 1 if any(result["status"] == "failed" for result in results) else 0


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
    if args.switch_command == "console":
        return _switch_console(args)
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
    if args.collab:
        command.extend(["--profile", "collab"])
    command.extend(["up", "-d"])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print("Error: docker compose could not start the Switch stack", file=sys.stderr)
        return result.returncode
    print(f"Switch stack requested. Environment: {env_file}")
    print(f"API: http://127.0.0.1:{args.api_port}")
    if not args.no_gateway:
        print(f"Gateway: http://127.0.0.1:{args.gateway_port}")
    if args.collab:
        print("Mattermost: http://127.0.0.1:8065")
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
    ]
    if args.collab:
        command.extend(["--profile", "collab"])
    command.append("down")
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


def _switch_console(args: argparse.Namespace) -> int:
    settings = ConsoleSettings.from_env(required=True)
    client = ConsoleClient(settings)
    try:
        if args.console_command == "status":
            return _console_status(client, args)
        if args.console_command == "setup":
            return _console_setup(client, args)
        if args.console_command == "incident":
            return _console_incident(client, args)
    finally:
        client.close()
    raise AssertionError("unreachable")


def _console_status(client: ConsoleClient, args: argparse.Namespace) -> int:
    controllers = client.controllers()
    agents = client.agents()
    rooms = client.rooms()
    online = [item for item in controllers if item.get("state") == "online"]
    payload = {
        "console_url": client.settings.server_url,
        "controllers": controllers,
        "agents": agents,
        "rooms": rooms,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Switch Console: {client.settings.server_url}")
    print(f"Online machines: {len(online)}/{len(controllers)}")
    for item in controllers:
        print(f"- {item.get('name')} ({item.get('state')}) {item.get('id')}")
    print(f"Console-managed agents: {len(agents)}")
    for item in agents:
        definition = item.get("definition") or {}
        print(
            f"- {item.get('name')} provider={definition.get('provider')} "
            f"desired={item.get('desired_state')} id={item.get('agent_id')}"
        )
    print(f"Rooms: {len(rooms)}")
    for item in rooms:
        print(f"- {item.get('name')} {item.get('id') or item.get('room_id')}")
    return 0


def _ensure_incident_kit(client: ConsoleClient) -> str:
    existing_documents = {str(item.get("name")): str(item.get("id")) for item in client.documents()}
    for document in incident_kit_documents():
        if document["name"] in existing_documents:
            continue
        created = client.create_document(
            name=document["name"],
            description=document["description"],
            instructions=document["instructions"],
            content=document["content"],
        )
        existing_documents[document["name"]] = str(created["id"])

    package = next(
        (item for item in client.packages() if str(item.get("name")) == INCIDENT_KIT_NAME),
        None,
    )
    if package is None:
        package = client.create_package(
            name=INCIDENT_KIT_NAME,
            description="Shared read-only evidence, report, and memory rules.",
            instructions=(
                "Attach to every incident room. These are read-only templates and rules; "
                "they do not grant tool or cluster permissions."
            ),
        )
    package_id = str(package["id"])
    attached = {str(item.get("id")) for item in client.package_documents(package_id)}
    for document in incident_kit_documents():
        document_id = existing_documents[document["name"]]
        if document_id not in attached:
            client.add_document_to_package(package_id, document_id)
    return package_id


def _console_setup(client: ConsoleClient, args: argparse.Namespace) -> int:
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        raise ConfigurationError(f"Console agent directory does not exist: {directory}")
    controllers = client.controllers()
    if args.controller_id:
        controller = next(
            (item for item in controllers if item.get("id") == args.controller_id), None
        )
        if controller is None:
            raise SwitchError("The requested Switch Console machine was not found")
    else:
        controller = next((item for item in controllers if item.get("state") == "online"), None)
    if controller is None:
        raise SwitchError("No online Switch Console machine is available")
    if controller.get("state") != "online":
        raise SwitchError("The selected Switch Console machine is not online")

    existing = {str(item.get("name")): item for item in client.agents()}
    created: dict[str, str] = {}
    updated: list[str] = []
    for spec in AGENTS:
        instructions = console_instructions(spec.name)
        current = existing.get(spec.name)
        if current is None:
            item = client.create_agent(
                name=spec.name,
                description=spec.description,
                display_name=spec.display_name,
                controller_id=str(controller["id"]),
                provider=args.provider,
                instructions=instructions,
                directory=str(directory),
                auto_mode=args.auto_mode,
            )
            created[spec.name] = str(item["agent_id"])
            continue
        provider = (current.get("definition") or {}).get("provider")
        if args.refresh and provider == args.provider:
            client.update_agent(
                str(current["agent_id"]),
                instructions=instructions,
                directory=str(directory),
                provider=args.provider,
                auto_mode=args.auto_mode,
            )
            updated.append(spec.name)

    room_names = {
        str(item.get("name")): str(item.get("id") or item.get("room_id")) for item in client.rooms()
    }
    missing = [key for key, name in CORE_ROOMS.items() if name not in room_names]
    if missing:
        if len(missing) == len(CORE_ROOMS):
            result = client.create_rooms_from_yaml(core_room_template())
            for item in result.get("rooms", []):
                if isinstance(item, dict) and item.get("room_name") and item.get("room_id"):
                    room_names[str(item["room_name"])] = str(item["room_id"])
        else:
            for key in missing:
                result = client.create_rooms_from_yaml(core_room_yaml(key))
                room_id = result.get("room_id")
                if isinstance(room_id, str) and room_id:
                    room_names[CORE_ROOMS[key]] = room_id

    kit_package_id = _ensure_incident_kit(client)
    payload = {
        "console_url": client.settings.server_url,
        "controller": {
            "id": controller.get("id"),
            "name": controller.get("name"),
        },
        "created_agents": created,
        "updated_agents": updated,
        "existing_agents": sorted(name for name in existing if name not in created),
        "rooms": {key: room_names.get(name) for key, name in CORE_ROOMS.items()},
        "incident_kit_package_id": kit_package_id,
        "directory": str(directory),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Console machine: {controller.get('name')} ({controller.get('id')})")
    for name, agent_id in sorted(created.items()):
        print(f"Created agent {name}: {agent_id}")
    for name in updated:
        print(f"Updated agent {name}")
    for key, room_id in sorted(payload["rooms"].items()):
        print(f"Room {key}: {room_id or 'not created'}")
    print(f"Working directory: {directory}")
    return 0


def _console_incident(client: ConsoleClient, args: argparse.Namespace) -> int:
    known = {str(item.get("name")) for item in client.agents()}
    missing = [spec.name for spec in AGENTS if spec.name not in known]
    if missing:
        raise SwitchError(
            "Console agents are missing; run `kube-investigator switch console setup` first: "
            + ", ".join(missing)
        )
    core_rooms = {
        str(item.get("name")): str(item.get("id"))
        for item in client.rooms()
        if str(item.get("name")) in CORE_ROOMS.values()
    }
    missing_rooms = [name for name in CORE_ROOMS.values() if name not in core_rooms]
    if missing_rooms:
        raise SwitchError(
            "Console core rooms are missing; run `kube-investigator switch console setup` first: "
            + ", ".join(missing_rooms)
        )
    incident_id = uuid4().hex[:8]
    room_name = f"incident-{incident_id}-{args.namespace}-{args.service}"
    kickoff = (
        "@incident-commander Start the read-only investigation.\n\n"
        f"Fixture scenario: {args.scenario}\n"
        f"Target: cluster={args.cluster} namespace={args.namespace} "
        f"service={args.service} deployment={args.deployment}\n"
        f"Question: {args.question}\n\n"
        "Read the incident brief and every room document. Dispatch the specialists, "
        "use the kube-investigator MCP tools for evidence, and publish the final report. "
        "No cluster changes."
    )
    template = incident_room_template(
        room_name=room_name,
        incident_id=incident_id,
        cluster=args.cluster,
        namespace=args.namespace,
        service=args.service,
        deployment=args.deployment,
        question=args.question,
        kickoff=kickoff,
    )
    result = client.create_rooms_from_yaml(template)
    room_id = result.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise SwitchError("Switch Console did not return an incident room id")

    kit_package_id = _ensure_incident_kit(client)
    installed = {str(item.get("id")) for item in client.room_packages(room_id)}
    if kit_package_id not in installed:
        client.attach_package_to_room(room_id, kit_package_id)

    links = {str(item.get("target_room_id")) for item in client.linked_rooms(room_id)}
    for target_name, label in (
        (CORE_ROOMS["memory"], "approved incidents"),
        (CORE_ROOMS["change"], "recent changes"),
        (CORE_ROOMS["governance"], "trust and roi"),
    ):
        target_id = core_rooms[target_name]
        if target_id not in links:
            client.link_room(room_id, target_id, label)

    payload = {
        "room_id": room_id,
        "room_name": result.get("room_name") or room_name,
        "incident_id": incident_id,
        "scenario": args.scenario,
        "console_url": client.settings.server_url,
        "incident_kit_package_id": kit_package_id,
        "links": {
            "memory": core_rooms[CORE_ROOMS["memory"]],
            "change": core_rooms[CORE_ROOMS["change"]],
            "governance": core_rooms[CORE_ROOMS["governance"]],
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Switch Console incident room: {payload['room_name']} ({room_id})")
        print(f"Scenario: {args.scenario}")
        print("The kickoff addresses incident-commander; Console starts the session.")
    return 0


def _trust_scan(args: argparse.Namespace) -> int:
    binary = os.getenv("INVESTIGATOR_TRUST_CLI_BIN") or "switch-trust"
    resolved = shutil.which(binary)
    if resolved is None:
        raise ConfigurationError(
            "Switch Trust CLI is not installed; run `python -m pip install switch-trust-cli` "
            "or set INVESTIGATOR_TRUST_CLI_BIN"
        )
    command = [resolved, "scan", str(args.path), "--format", args.format]
    if args.output:
        command.extend(["--output", args.output])
    if args.log:
        command.extend(["--log", args.log])
    return subprocess.run(command, check=False).returncode


def _switch_trust(args: argparse.Namespace) -> int:
    if args.trust_command == "scan":
        return _trust_scan(args)
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
        if getattr(args, "console", False):
            console_settings = ConsoleSettings.from_env(required=True)
            console = ConsoleClient(console_settings)
            try:
                governance = next(
                    (item for item in console.rooms() if str(item.get("name")) == "k8s-governance"),
                    None,
                )
                if governance is None:
                    raise SwitchError("Console k8s-governance room is not available")
                room_id = str(governance.get("id"))
                document = render_governance_summary(summary)
                stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
                created = console.create_document(
                    name=f"trust-summary-{stamp}",
                    description="Read-only Switch Trust and ROI summary.",
                    instructions=(
                        "Governance evidence only. Re-read Trust before making a gate "
                        "decision; this snapshot does not prove a gate passed."
                    ),
                    content=document,
                )
                document_id = str(created["id"])
                console.attach_document_to_room(room_id, document_id)
            finally:
                console.close()
            payload = {
                "summary": summary,
                "switch_document_id": document_id,
                "switch_event_id": None,
                "switch_room_id": room_id,
                "switch_console_url": console_settings.server_url,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(document, end="")
                print(f"\nConsole governance room: {room_id}")
                print(f"Document: {document_id}")
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


def _mattermost_values() -> dict[str, str]:
    return {
        "MATTERMOST_HOST_PORT": "8065",
        "MATTERMOST_ADMIN_USER": "admin",
        "MATTERMOST_ADMIN_PASSWORD": secrets.token_hex(24),
        "MATTERMOST_TEAM_NAME": "switch",
        "MATTERMOST_USER": "user",
        "MATTERMOST_USER_PASSWORD": secrets.token_hex(24),
    }


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
        existing = {
            line.split("=", 1)[0]
            for line in env_file.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        additions = {
            key: value for key, value in _mattermost_values().items() if key not in existing
        }
        if additions:
            with env_file.open("a") as handle:
                if env_file.stat().st_size and not env_file.read_text().endswith("\n"):
                    handle.write("\n")
                for key, value in additions.items():
                    handle.write(f"{key}={value}\n")
            os.chmod(env_file, 0o600)
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
        **_mattermost_values(),
    }
    temporary = env_file.with_name(env_file.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    os.replace(temporary, env_file)
    os.chmod(env_file, 0o600)
    return env_file
