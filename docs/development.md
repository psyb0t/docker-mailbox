---
title: "Development"
description: "How mailboxd is built: architecture, the sandboxed dev image, every Make target, the test suite, dependency pinning and the release pipeline."
nav_order: 11
---

# Development

## Architecture

```
┌────────────┐    ┌──────────────────────────┐    ┌──────────────┐
│  HTTP CLI  │───▶│  FastAPI                 │    │  IMAP server │
│  / curl    │    │  /inbox, /mailboxes, …   │───▶│  SMTP server │
└────────────┘    │                          │    └──────────────┘
┌────────────┐    │  bearer gate → Principal │
│  MCP host  │───▶│  ─── Service (scopes) ── │───▶ state.db (SQLite volume)
│ (Claude…)  │    │  /mcp (streamable HTTP)  │      audit · send limits · checkpoints
└────────────┘    └──────────────────────────┘
                              ▲
        mailboxd watch ───────┘  IMAP IDLE → signed webhooks
```

The HTTP routes ([`server.py`](https://github.com/psyb0t/docker-mailbox/blob/main/src/mailboxd/server.py)) and the MCP tools ([`mcp_server.py`](https://github.com/psyb0t/docker-mailbox/blob/main/src/mailboxd/mcp_server.py)) are thin layers over one `Service` ([`services.py`](https://github.com/psyb0t/docker-mailbox/blob/main/src/mailboxd/services.py)). It resolves the mailbox for the caller's `Principal`, checks the scope, does the IMAP or SMTP work, and writes the audit row. The rest of [`src/mailboxd/`](https://github.com/psyb0t/docker-mailbox/tree/main/src/mailboxd):

| Module | Job |
| --- | --- |
| `config.py` | YAML loading, `${ENV}` interpolation, validation |
| `principal.py` | Token lookup, scopes, mailbox visibility |
| `imap_client.py`, `smtp_client.py` | Mail protocols, size caps, search criteria |
| `compose.py` | Building messages, identities, reply recipients |
| `mime.py` | Decoding, Authentication-Results, signals, HTML analysis and sanitizing |
| `insights.py` | Threads, stats, deep health |
| `state.py` | SQLite schema, audit, rate limits, checkpoints, webhook queue |
| `watch.py` | The IDLE watcher and webhook delivery |
| `cli.py` | The `mailboxd` command |

## Make targets

Every tool runs inside the dev image (`psyb0t/mailboxd-dev`), so the host needs only Docker and Make.

```bash
make help           # list all targets
make dev-image      # build the dev image
make shell          # shell inside it
make run            # server from source (CONFIG=path/to/config.yaml, PORT=8000)
make watch          # watcher from source, with ./config.yaml
make test           # full suite, docker-in-docker and container tests included
make test-unit      # in-process tests only (TEST_ARGS=... for pytest)
make test-docker    # production-container tests only
make test-coverage  # full suite with an 85 percent coverage floor
make lint           # flake8 and mypy
make format         # isort and black
make sec            # semgrep, bandit, pip-audit into sec.sarif; never fails
make audit          # advisories for the frozen runtime and dev dependency graph
make audit-compose  # check the production Compose defaults
make build          # build psyb0t/mailbox:latest locally
make version        # print the package version
make clean          # remove build and cache files
```

`make run` publishes the port on every host interface (`-p 8000:8000`). Bearer auth still protects everything but `/health`; for loopback-only access use the [Docker example](installation.md#docker).

## Tests

Tests run against an in-process IMAP mock ([`tests/imap_mock.py`](https://github.com/psyb0t/docker-mailbox/blob/main/tests/imap_mock.py)) that can act like a modern server (`MOVE`, `UIDPLUS`, `SPECIAL-USE`, several folders, flags, `APPENDUID`) and records every command it receives, so tests can assert that no folder-wide `EXPUNGE` is ever sent.

The suite also uses real SQLite files, local SMTP and HTTP servers, and live TLS connections with trusted, untrusted and wrong-host-name certificates. Production-container tests check startup, authentication, file permissions, persistent state and Compose rendering. None of this uses real provider accounts; provider quirks still need checking on the target deployment.

## Dependencies

`uv`'s `exclude-newer` refuses package versions newer than a fixed date. Dependency changes move that date to UTC midnight seven days ago, so nothing younger than a week gets in. `pkg-lock` keeps the current date.

```bash
make pkg-add PKG=foo==1.2.3      # GROUP=dev for a dev dependency
make pkg-remove PKG=foo
make pkg-update PKG=foo V=1.2.4
make pkg-lock
make pkg-upgrade
```

Don't edit `[tool.uv].exclude-newer` by hand; the targets manage it.

## Release pipeline

Push release commits to `main` before creating their annotated `v*` tags. The `main` pipeline runs the checks and publishes `psyb0t/mailbox:latest` and `psyb0t/mailbox:sha-<commit>` for `linux/amd64` and `linux/arm64`. A tag pipeline waits for that commit's `main` pipeline to succeed, then promotes the same image digest to the release tag without rebuilding. The wait fails if the `main` run fails, never appears or times out.

After promotion, a separate job creates the GitHub release from the annotated tag notes. Publishing the skill and plugin to ClawHub and the server to the MCP Registry waits for that release. Published tags are never changed.

## Documentation

This site is built by GitHub Pages from [`docs/`](https://github.com/psyb0t/docker-mailbox/tree/main/docs) on `main`, with Jekyll and the Just the Docs theme. Every page needs front matter with a `title`, a `description` of 50 to 170 characters and a `nav_order`. Link between pages with relative `.md` paths, and to files outside `docs/` with full GitHub URLs.
