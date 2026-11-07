# Local data snapshot (sanitized)

A sanitized snapshot of the local `.data/` directory from 2026-11-07, published so the
repository shows the real layout without leaking live credentials.

## Included

- `console-agents/<agent>/opencode.json` — agent MCP configs with `<ACTOR_TOKEN>`,
  `<PUBLIC_KEY>` and `<REPO>/.data` placeholders;
- `switch/.env` — every key, with passwords/tokens/secret keys replaced by `<redacted>`;
- `switch/agents.json` — agent ids with `api_key` placeholders;
- `tool-audit.jsonl`, `memory-audit*.jsonl` — hash-chained audit samples (no secrets);
- `investigator.sqlite3` — the demonstration evidence/memory store;
- `mcp-auth/README.md` — explains that the private signing key is excluded.

## Excluded

- `mcp-auth/mcp-actor-private.pem` — the Ed25519 signing key;
- `k8s-docs/` — 50 MB docs corpus + SQLite index; rebuild with
  `python scripts/index_kubernetes_docs.py`;
- `smoke/` — throwaway smoke-test store.

## Restore locally

```sh
cp -R examples/local-data/snapshot/* .data/
ai-sre switch up --collab          # recreates switch/.env and agents
ai-sre switch bootstrap --write-env
ai-sre switch console setup --directory .
python scripts/index_kubernetes_docs.py
```

Never put real tokens, passwords or private keys back into this directory.
