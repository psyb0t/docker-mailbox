---
title: "Home"
description: "mailbox puts several IMAP/SMTP accounts behind one HTTP API and MCP endpoint, with scoped bearer tokens, send limits and an audit log."
nav_order: 1
permalink: /
seo:
  type: WebSite
---

# mailbox

mailbox (the `mailboxd` service, image `psyb0t/mailbox`) reads, organizes, drafts and sends mail across multiple IMAP/SMTP accounts. It serves an HTTP API and an MCP endpoint at `/mcp` on the same port. Each bearer token controls which operations and which mailboxes a client can use, so you can give an agent a token that reads and drafts but can never send or erase anything.

```text
HTTP client ─┐                                  ┌─ IMAP server
             ├─ bearer token ─ Service (scopes) ┤
MCP host ────┘                 │                └─ SMTP server
                               └─ state.db (SQLite): audit, send limits, checkpoints, webhook queue

mailboxd watch ─ IMAP IDLE ─ signed webhooks
```

Mail bodies and drafts stay on the IMAP server. A shared SQLite database stores audit records, send counters, checkpoints, draft send claims and webhook delivery metadata. API requests open mail-server connections and close them when they finish; only the optional watcher keeps an IMAP connection open.

## What it does

| Area | Operations |
| --- | --- |
| Reading | Unified inbox across accounts, structured search, raw source, headers, attachments, threads, folder stats and quota |
| Organization | Read/unread, labels, move, create folders, delete to Trash |
| Sending | Configured sender identities and signatures, replies with server-derived recipients, IMAP drafts, Sent copies, dry runs |
| Access | Per-token scopes and mailbox allowlists, shared by the HTTP API and MCP |
| Analysis | SPF/DKIM/DMARC verdicts from a trusted receiving server, header mismatches, hidden HTML text, deceptive links, sanitized HTML |
| Operations | SQLite audit log, hourly send limits, "new mail since" checkpoints, signed webhooks with retries and replay |

The safety rules that matter for agent access:

- A token holding `read`, `draft` and `organize` cannot send or permanently delete. Over MCP it does not even see the send tools or the `permanent` flag.
- Deleting moves a message to Trash. Permanent erasure needs the `delete` scope, works only from Trash and never issues a folder-wide `EXPUNGE`.
- An agent can save a reply as a draft; a person (or a token with `send`) sends it.
- Sends are rate limited per hour, can be forced into dry-run mode per token, and must use a configured sender identity.
- Writes on a visible mailbox land in the audit log, including those refused for a missing scope and those the mail server rejected. Message bodies are never logged.

[Safety and security](security.md) has the details.

## Quick start

1. Copy [`config.example.yaml`](https://github.com/psyb0t/docker-mailbox/blob/main/config.example.yaml) to `config.yaml` and [`.env.example`](https://github.com/psyb0t/docker-mailbox/blob/main/.env.example) to `.env`. Configure your accounts and replace every placeholder in `.env` with its password, or with a fresh token from `openssl rand -hex 32`. Restrict both files with `chmod 600`.
2. Start the container. The named volume keeps the state database across restarts:

```bash
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  -v mailboxd-state:/var/lib/mailboxd \
  psyb0t/mailbox:latest
```

3. Check health and list mail:

```bash
TOKEN="paste-the-token-here"

curl -s http://localhost:8000/health | jq      # always open, no token
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/mailboxes | jq
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/inbox?limit=5' | jq
```

4. Point an MCP client at `http://localhost:8000/mcp` with the same bearer token. See [MCP server](mcp.md).

## Where to go next

| Page | What is in it |
| --- | --- |
| [Installation](installation.md) | Docker, Compose, the webhook watcher, image tags, CLI commands and environment variables |
| [Configuration](configuration.md) | Every config field: mailboxes, tokens and scopes, identities and signatures, limits, labels |
| [HTTP API](api.md) | Every route, query parameter, request body, response and error code |
| [MCP server](mcp.md) | The tool set, scopes, annotations and client setup |
| [Safety and security](security.md) | Deletion, drafts, dry runs, rate limits, sender checks, audit, Authentication-Results trust, TLS |
| [Webhooks](webhooks.md) | `mailboxd watch`, the event payload, signature verification, retries and replay |
| [Agent integrations](integrations.md) | Claude Code, Codex and OpenClaw plugins and the agent skill |
| [Operations](operations.md) | State, backups, restores, health checks and upgrade notes |
| [Troubleshooting](troubleshooting.md) | Startup errors, common API errors and what to do about them |
| [Development](development.md) | Architecture, Make targets, tests and the release pipeline |

The service uses Python's `imaplib` and `smtplib`, FastAPI, the official MCP Python SDK and `nh3` for HTML sanitizing. Username and password logins only, including app passwords; there is no OAuth login.
