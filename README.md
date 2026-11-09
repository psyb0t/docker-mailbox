# mailbox

[![CI](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/version.svg)](https://github.com/psyb0t/docker-mailbox/releases)
[![license](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/mailbox?style=flat-square)](https://hub.docker.com/r/psyb0t/mailbox)

Read, organize, draft and send mail across multiple IMAP/SMTP accounts through one HTTP API and an MCP endpoint at `/mcp`, on the same port. Each bearer token controls which operations and mailboxes a client can access.

For agent access, configure tokens with explicit permissions:

- **Scoped tokens.** A token that holds `read`, `draft` and `organize` cannot send or permanently delete. Over MCP it does not even see the send tools or the `permanent` flag.
- **Deletion defaults to Trash.** Deleting moves a message to Trash. Permanent erasure needs the `delete` scope and only works from the Trash folder. The service never issues a folder-wide `EXPUNGE`.
- **Drafts in two steps.** An agent can prepare a reply; a human (or a token with `send`) sends it.
- **Send guard.** Hourly rate limit, dry-run mode, and a sender address authorized by your configured exact identities or opt-in domain patterns.
- **Message analysis.** Inspect SPF/DKIM/DMARC verdicts, header mismatches, hidden text and deceptive links. Verdicts are trusted only when the receiving server matches your configuration. Request sanitized HTML when needed.
- **Audit log.** Writes are recorded, including those refused for a missing scope or rejected by the mail server. Message bodies never are.

Mail bodies and drafts stay on IMAP. A shared SQLite database stores audit records, send counters, checkpoints, draft send claims and webhook delivery metadata. API requests close their mail-server connections after use; the watcher keeps an IMAP connection open for notifications.

The service uses Python's `imaplib` and `smtplib`, FastAPI, the official MCP Python SDK and `nh3` for HTML sanitizing. Dependency updates enforce a seven-day release-age cutoff through `uv`'s `exclude-newer` setting.

## Contents

- [Quick start](#quick-start)
- [Capabilities](#capabilities)
- [Documentation](#documentation)
- [Development](#development)
- [Sponsor](#sponsor)
- [License](#license)

## Quick start

1. Copy `config.example.yaml` to `config.yaml` and `.env.example` to `.env`. Configure your accounts and replace every placeholder in `.env` with its password or a fresh token from `openssl rand -hex 32`. Restrict both files with `chmod 600`.
2. Start the container. The named volume preserves operational state across restarts:

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

# Health is always open, no token needed.
curl -s http://localhost:8000/health | jq

curl -s \
  -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/mailboxes | jq

curl -s \
  -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/inbox?limit=5' | jq
```

### Docker Compose

The checked-in `docker-compose.yml` publishes on `127.0.0.1:${PORT:-8000}`, mounts `./config.yaml` (or `$MAILBOXD_CONFIG`) read-only and creates the `mailboxd-state` volume:

```yaml
services:
  mailboxd:
    image: psyb0t/mailbox:latest
    ports: ["127.0.0.1:8000:8000"]
    volumes:
      # Keep config.yaml out of Git and restrict access to its credentials.
      - ./config.yaml:/etc/mailboxd/config.yaml:ro
      - mailboxd-state:/var/lib/mailboxd
volumes:
  mailboxd-state:
```

The optional webhook watcher is a second service in the same file, off by default: `docker compose --profile watch up -d`.

## Capabilities

| Area | Operations |
| --- | --- |
| Reading | Unified inbox, structured search, raw source, headers, attachments and threads |
| Organization | Seen flags, labels, move, mailbox creation and Trash |
| Sending | Configured identities, signatures, replies, IMAP drafts and Sent copies |
| Access | Per-token scopes and mailbox allowlists shared by REST and MCP |
| Operations | SQLite audit records, shared send limits, checkpoints and durable signed webhooks |

Mount `/var/lib/mailboxd` for persistent operational state. HTTP workers and the optional watcher share one SQLite database on a local filesystem. Read the [upgrade notes](https://psyb0t.github.io/docker-mailbox/operations/#upgrade-notes) before replacing a stateless deployment.

## Documentation

The full documentation lives at **[psyb0t.github.io/docker-mailbox](https://psyb0t.github.io/docker-mailbox/)**.

| Page | What is in it |
| --- | --- |
| [Installation](https://psyb0t.github.io/docker-mailbox/installation/) | Docker, Compose, the webhook watcher, image tags, CLI and environment variables |
| [Configuration](https://psyb0t.github.io/docker-mailbox/configuration/) | Every config field: mailboxes, tokens and scopes, identities, limits, labels |
| [HTTP API](https://psyb0t.github.io/docker-mailbox/api/) | Routes, parameters, bodies, responses and error codes |
| [MCP server](https://psyb0t.github.io/docker-mailbox/mcp/) | Tools, scopes, annotations and client setup |
| [Safety and security](https://psyb0t.github.io/docker-mailbox/security/) | Deletion, drafts, dry runs, rate limits, sender checks, audit, TLS |
| [Webhooks](https://psyb0t.github.io/docker-mailbox/webhooks/) | Event payload, signature verification, retries and replay |
| [Agent integrations](https://psyb0t.github.io/docker-mailbox/integrations/) | Claude Code, Codex and OpenClaw |
| [Operations](https://psyb0t.github.io/docker-mailbox/operations/) | State, backups, health checks and upgrade notes |
| [Troubleshooting](https://psyb0t.github.io/docker-mailbox/troubleshooting/) | Startup errors and common API failures |

## Development

```bash
make lint           # flake8 and mypy
make test-coverage  # full suite, production container and coverage floor
make audit          # frozen runtime and dev dependency advisories
make build          # production image
make help           # all available targets
```

All project tools run inside the development image. Tests use socket-level IMAP/SMTP fixtures, a local HTTP receiver, real SQLite files and a production-container smoke test. See [development](https://psyb0t.github.io/docker-mailbox/development/) for details.

## Sponsor

<a href="https://www.buymeacoffee.com/psyb0t"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-blue.png" alt="Buy Me a Coffee" height="60" width="217"></a>

## License

WTFPL. See [LICENSE](LICENSE) and [third-party components](THIRD_PARTY.md).
