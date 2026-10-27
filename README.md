# mailbox

[![CI](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/version.svg)](https://github.com/psyb0t/docker-mailbox/releases)
[![license](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/mailbox?style=flat-square)](https://hub.docker.com/r/psyb0t/mailbox)

Read, organize, draft and send mail across multiple IMAP/SMTP accounts through one HTTP API and an MCP endpoint at `/mcp`, on the same port. Each bearer token controls which operations and mailboxes a client can access.

For agent access, configure tokens with explicit permissions:

- **Scoped tokens.** A token that holds `read`, `draft` and `organize` cannot send or delete, and does not even see those tools.
- **Deletion defaults to Trash.** Deleting moves a message to Trash. Permanent erasure needs the `delete` scope and only works from the Trash folder. The service never issues a folder-wide `EXPUNGE`.
- **Drafts in two steps.** An agent can prepare a reply; a human (or a token with `send`) sends it.
- **Send guard.** Hourly rate limit, dry-run mode, and a sender address authorized by your configured exact identities or opt-in domain patterns.
- **Message analysis.** Inspect SPF/DKIM/DMARC verdicts, header mismatches, hidden text and deceptive links. Verdicts are trusted only when the receiving server matches your configuration. Request sanitized HTML when needed.
- **Audit log.** Every write, and every refused write, is recorded (never message bodies).

Mail bodies and drafts stay on IMAP. A shared SQLite database stores audit records, send counters, checkpoints, draft send claims and webhook delivery metadata. API requests close their mail-server connections after use; the watcher keeps an IMAP connection open for notifications.

The service uses Python's `imaplib` and `smtplib`, FastAPI, the official MCP Python SDK and `nh3` for HTML sanitizing. Dependency updates enforce a seven-day release-age cutoff through `uv`'s `exclude-newer` setting.

## Contents

- [Quick start](#quick-start)
- [Configuration](docs/configuration.md)
- [HTTP API, MCP, safety and webhooks](docs/api.md)
- [Agent integrations](docs/integrations.md)
- [Architecture and development](docs/development.md)
- [Operations, backups and migration](docs/operations.md)

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

curl -s http://localhost:8000/health | jq                                               # health is always open
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/mailboxes | jq
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/inbox?limit=5' | jq
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

Mount `/var/lib/mailboxd` for persistent operational state. HTTP workers and the optional watcher share one SQLite database on a local filesystem. Read the [migration notes](docs/operations.md#migration) before replacing a stateless deployment.

## Development

```bash
make lint           # flake8 and mypy
make test-coverage  # full suite, production container and coverage floor
make audit          # frozen runtime and dev dependency advisories
make build          # production image
make help           # all available targets
```

All project tools run inside the development image. Tests use socket-level IMAP/SMTP fixtures, a local HTTP receiver, real SQLite files and a production-container smoke test. See [development](docs/development.md) for details.

## License

WTFPL. See [LICENSE](LICENSE) and [third-party components](THIRD_PARTY.md).
