# Mattermost server for the six agents

Mattermost is installed as part of the self-hosted Switch stack and is running on
this Mac. The six Kubernetes Investigator agents are registered as Mattermost bot
users, are members of the core channels, and every Switch room is bridged to a
Mattermost channel in both directions.

## What is running

| Component | Image | URL |
|---|---|---|
| Mattermost Team Edition | `mattermost/mattermost-team-edition:11.9.0` (amd64, emulated) | <http://127.0.0.1:8065> |
| Switch core (rooms, agents, bridge) | `ghcr.io/sandbox-quantum/switch-core:0.31.0` | <http://127.0.0.1:18000> (`/health`) |
| Switch Gateway | `ghcr.io/sandbox-quantum/gateway:0.31.0` | <http://127.0.0.1:13000> |
| PostgreSQL 16 (Mattermost + Switch) | `postgres:16-alpine` | `127.0.0.1:15432` |

All four containers now use `--restart unless-stopped`, so they come back when
Docker Desktop restarts. Data persists in the compose volumes (`mmdata` for
media, the Postgres volume for messages and Switch state).

Start / stop the stack:

```sh
kube-investigator switch up --collab    # start (collab profile = Mattermost + seeder)
kube-investigator switch down           # stop
docker compose -f deploy/switch/compose.yaml --profile collab ps
```

Logins (the interactive `switch up` writes these to `.data/switch/.env`; never
commit that file):

| Account | Username | Password |
|---|---|---|
| Admin | `admin` | `MATTERMOST_ADMIN_PASSWORD` in `.data/switch/.env` |
| Demo user | `user` | `MATTERMOST_USER_PASSWORD` in `.data/switch/.env` |

Team: **Switch**.

## Channels and members

| Channel | Members |
|---|---|
| `k8s-incidents` | admin, user, incident-commander, cluster-investigator, telemetry-analyst, change-analyst, memory-researcher, evidence-verifier |
| `k8s-change` | admin, user, incident-commander, change-analyst |
| `k8s-memory` | admin, user, incident-commander, memory-researcher, evidence-verifier |
| `k8s-governance` | admin, user, incident-commander, evidence-verifier |

Every investigation also gets `incident-<id>-<namespace>-<service>` (currently 69
mirrored incident channels). The four core rooms were bridged on 2026-11-06 with
the instance's default Mattermost bridge:

| Room | Channel id |
|---|---|
| `k8s-incidents` | `8gnzfc61yib89cbiodbpdr1brr` |
| `k8s-change` | `ipmzeb6k7fnqu8zod7y6jspw9y` |
| `k8s-memory` | `i7p1qmu8hjfrbdrxt6xjnkybpa` |
| `k8s-governance` | `g7ioby6rpirjupit9hagyqypic` |

All six agents exist as Mattermost bots (`is_bot: true`) and post under their own
names: a room message and the Mattermost post are the same event, mirrored by
`switch-core`.

## How the bridge behaves

- **Room ↔ channel, both directions.** A Mattermost message from `admin`/`user`
  lands in the Switch room as that user; a room message from an agent lands in
  Mattermost as the agent's bot. Verified both ways on 2026-11-06.
- **Agents do not run autonomously on this stack.** The self-hosted stack is the
  protocol harness: it registers the six identities and mirrors the rooms, but a
  process must act for an agent. Use the Console-managed sessions (live runtime)
  or `kube-investigator switch-demo` (deterministic). If you mention an agent
  with no runtime holding its role, Switch replies with a warning such as
  `no agent currently holds the evidence-verifier role`.
- **Console rooms** are a separate plane; bridge a Console room to Mattermost
  from Console → Connectors if you want the OpenAI-agent runtime mirrored into a
  channel too.

## Multi-agent showcase channel

`k8s-showcase` is a clean room for presentations:
<http://localhost:8065/switch/channels/k8s-showcase>. Addressing
`@incident-commander investigate …` there runs the full handoff (commander →
specialists → verifier → final hypothesis) instead of a single reply.

## Try it

1. Open <http://127.0.0.1:8065> and sign in as `admin` (password from `.data/switch/.env`).
2. Open the **Switch** team → `k8s-incidents`; the six agents and two humans are members.
3. Post a message as a human; it appears in the Switch room history:

   ```sh
   kube-investigator switch status --json   # room ids
   ```

4. Drive agent activity deterministically and watch the mirrored channel:

   ```sh
   kube-investigator switch-demo --scenario selector-mismatch --json
   ```

5. Probe the bridge endpoints directly:

   ```sh
   .venv/bin/python - <<'PY'
   import json, httpx
   c = json.load(open(".data/switch/agents.json"))
   a = c["agents"]["incident-commander"]
   h = {"Authorization": f"Bearer {a['api_key']}"}
   print(httpx.get(c["base_url"] + f"/agents/{a['agent_id']}/moderation/bridges", headers=h).json())
   PY
   ```

## Making the agents answer (and find problems)

The standalone stack runs no model, so an addressed agent with no session replies
`I'm not in an active session right now`. `scripts/switch_agent_responder.py`
keeps a room session for each agent in the four core rooms, watches the room
timelines, and answers mentions with a **live read-only audit** of the Kubernetes
context (`kind-imesh` by default).

It is installed as a per-user launchd service, so it survives crashes and login:

```sh
# status
launchctl print "gui/$(id -u)/ai.switch.agent-responder" | grep -E 'state|pid'
# restart after a code change
launchctl kickstart -k "gui/$(id -u)/ai.switch.agent-responder"
# stop / remove
launchctl bootout "gui/$(id -u)/ai.switch.agent-responder"
rm ~/Library/LaunchAgents/ai.switch.agent-responder.plist
```

Manual run (foreground, for debugging):

```sh
.venv/bin/python scripts/switch_agent_responder.py
```

### What the audit looks like

For every addressed message it runs bounded `kubectl` reads (nodes, pods,
deployments, services, endpoints, jobs, events) and reports concrete findings:

- pods not `Running`/`Succeeded`, containers in `CrashLoopBackOff` /
  `ImagePullBackOff` / `CreateContainerConfigError`, repeated restarts;
- deployments unavailable or **scaled to zero**;
- **services whose selector matches no ready pod** (empty endpoint list);
- **services whose `targetPort` is not exposed by the selected pods** (endpoints
  exist but every connection is refused);
- node NotReady / pressure, failed jobs, warning events.

It links a scaled-to-zero deployment to the service that lost its endpoints and a
mismatched `targetPort` to the pod ports, so the commander answers with a root
cause and a recommended first action instead of asking for a scope. A service or
deployment named in the message focuses the audit on that resource's namespace. Replies also cite one `Knowledge:` playbook or
`K8s reference:` URL from the role-scoped `kube_investigator_docs` index. A namespace named in the message scopes the audit (for
example `simple-devops`); otherwise it audits the whole cluster. On this cluster
it reports: `simple-devops` deployment at 0 replicas, its Service with no
endpoints, and historical restarts in `imesh-demo`.

Role behaviour:

| Agent | Reply content |
|---|---|
| incident-commander | severity summary, root cause, first action, dispatch plan |
| cluster-investigator | full finding list and namespaces (answers namespace questions first) |
| change-analyst | zero-replica ReplicaSet history and image revisions |
| telemetry-analyst | live pod-log error lines and metrics-server usage when configured; otherwise guidance from its observability playbook |
| memory-researcher | approved-record summary, leads not evidence |
| evidence-verifier | re-runs the audit and states confirmed findings |

`--llm` composes the reply with the local OpenCode CLI from the same facts
(slower; `scripts/opencode-textonly.json` denies tools). The default is
deterministic so a demo never hangs on a model call. It is read-only: it never
mutates the cluster.

### Session reliability

Two fixes keep the agents "in session": the room socket's read loop now continues
on its own one-second read timeout instead of treating it as a dead connection
(`src/kube_investigator/switch/client.py`), so it keeps answering Switch's ping
frames; and the responder reconnects any dead session on every poll, plus retries
a post after reconnecting. Verified by SIGKILLing the worker: launchd restarts it
and the 13 sessions reopen automatically.

## Linking a chat account to a Switch user

The bots only take instructions from their **owner**: the Mattermost account must be claimed by a
Switch user, and that Switch user must own the agent (the default addressing policy is
`owner: true` across all rooms). On the standalone stack, a chat account that messages a bot
without a claim gets:

> I only take instructions from my owner, and this chat account isn't linked to a Switch user yet...

The link is the `external_user_claims` row (Switch Console normally creates it; this stack's
Console build has no messaging-app connector, so link it directly with the DB owner credentials
from `.data/switch/.env`):

```sh
# 1. Find the external account (created on its first message)
psql ... -c "select id, external_username from external_users where external_username='<mm-user>'"
# 2. Claim it for a Switch user
psql ... -c "insert into external_user_claims (external_user_id, user_id, tenant_id)
             values ('<external-user-id>', '<switch-user-id>', '00000000-0000-0000-0000-000000000000')"
# 3. Make that Switch user the owner of the six agents (bootstrap owned them)
psql ... -c "update agents set owner_id='<switch-user-id>'
             where owner_id='038d47b8-51ea-4c7f-810e-de056e27f309'"
```

After claiming, addressing is allowed, but a harness agent with no connected session answers:

> I'm not in an active session right now. I'll see this when I next connect to the room.

That is the runtime boundary: the standalone stack mirrors rooms to Mattermost but does not run an
LLM. For conversational answers, use the Console-managed agents (Console → room) or run
`kube-investigator switch-demo` to drive the deterministic investigation.

## Security notes

- The stack binds to **127.0.0.1 only** (`SWITCH_BIND_ADDR`). Keep it that way;
  rebinding to `0.0.0.0` exposes an admin team with generated passwords and the
  agent room bridge to your LAN.
- Passwords live in `.data/switch/.env` (mode 0600, gitignored). Rotate them by
  editing the file and recreating the stack.
- The Mattermost bots are protocol identities; they are not a same-uid isolation
  boundary, and the agents still run under the operator account.
