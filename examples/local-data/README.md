# Local data template (safe to commit)

This is a **redacted template** of the runtime `.data/` directory. The real
`.data/` holds credentials and evidence and must stay out of git:

| Real file | Why it must not be committed |
|---|---|
| `.data/mcp-auth/mcp-actor-private.pem` | Ed25519 signing key for every actor token |
| `.data/console-agents/*/opencode.json` | Signed actor tokens in the MCP environment |
| `.data/switch/.env` | Mattermost, Gateway and Postgres passwords |
| `.data/switch/agents.json` | Switch agent API keys |
| `.data/investigator.sqlite3`, `*-audit.jsonl` | Evidence and audit records |

Regenerate the real directory locally:

```sh
pip install -e '.[dev,kubernetes]'
kube-investigator switch up --collab          # writes .data/switch/.env
kube-investigator switch bootstrap            # writes .data/switch/agents.json
kube-investigator switch console setup --directory .   # writes .data/console-agents/*
```

The templates below show the shape only. Replace `<REPO>`, `<ACTOR_TOKEN>` and
`<PUBLIC_KEY>` with values produced by the commands above.
