---
title: "Installation"
description: "Run mailboxd with Docker or Compose, enable the webhook watcher, pick an image tag, and use the mailboxd CLI and its environment variables."
nav_order: 2
---

# Installation

mailboxd ships as the Docker image [`psyb0t/mailbox`](https://hub.docker.com/r/psyb0t/mailbox) for `linux/amd64` and `linux/arm64`. You need Docker and at least one mail account that accepts username and password login over IMAP, SMTP or both. App passwords work. OAuth login is not supported.

## Image tags

| Tag | What it is |
| --- | --- |
| `latest` | The newest build of the `main` branch that passed CI |
| `sha-<commit>` | The build of one `main` commit, by its full commit hash |
| `vX.Y.Z` | A release. Release tags reuse the exact image digest already built and tested on `main`; nothing is rebuilt |

Pin a `vX.Y.Z` tag in production and read the [changelog](https://github.com/psyb0t/docker-mailbox/blob/main/CHANGELOG.md) before moving to a new one.

## Docker

Write `config.yaml` first (see [Configuration](configuration.md)), then:

```bash
docker run -d --name mailboxd \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  -v mailboxd-state:/var/lib/mailboxd \
  psyb0t/mailbox:latest
```

- The server listens on `0.0.0.0:8000` inside the container, so Docker port forwarding and other containers can reach it.
- `-p 127.0.0.1:8000:8000` publishes it on the host's loopback only. `-p 8000:8000` publishes it on every host interface; do that only on purpose, behind HTTPS.
- `--env-file .env` supplies the values that `config.yaml` references as `${NAME}`. A referenced variable that is not set stops the server at startup.
- `/var/lib/mailboxd` holds the SQLite state database. Use a named volume, or a host directory writable by UID 10001.

The image runs as the unprivileged user `mailboxd` (UID 10001), declares `/var/lib/mailboxd` as a volume, and has a Docker healthcheck that requests `/health` every 30 seconds. It starts uvicorn with `WORKERS` worker processes, 2 by default.

## Docker Compose

The repository's [`docker-compose.yml`](https://github.com/psyb0t/docker-mailbox/blob/main/docker-compose.yml) runs the server and, optionally, the webhook watcher:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 config.yaml .env
# edit both files, then:
docker compose up -d
```

What the file sets up:

- **`mailboxd`**: publishes `127.0.0.1:${PORT:-8000}`, runs `${WORKERS:-2}` workers, mounts `${MAILBOXD_CONFIG:-./config.yaml}` from the host read-only at `/etc/mailboxd/config.yaml`, and mounts the `mailboxd-state` volume. Its healthcheck requests `/health` every 10 seconds.
- **`watch`**: the same image running `mailboxd watch`, in the `watch` profile, so it only starts when asked for. It shares the config and the state volume. Its healthcheck runs `mailboxd watch-health`.
- **Hardening on both**: `init`, all capabilities dropped, `no-new-privileges`, a read-only root filesystem with a 64 MB `noexec` tmpfs at `/tmp`, 512 MB memory, 1 CPU, 256 processes, JSON logs rotated at 10 MB × 5 files, and a private `mailbox` network.
- Both services read `.env` with `env_file`. In `.env`, `MAILBOXD_CONFIG` is the config path on the host; inside the containers Compose sets it to `/etc/mailboxd/config.yaml`.

Start the watcher too:

```bash
docker compose --profile watch up -d
```

Run one watcher per state database. It needs `watch.webhooks` entries in the config; see [Webhooks](webhooks.md).

## CLI

The image's `mailboxd` command has three modes. The default container command runs uvicorn directly, which is the same as `mailboxd http --host 0.0.0.0 --workers $WORKERS`.

| Command | What it does |
| --- | --- |
| `mailboxd http [--host H] [--port P] [--workers N] [--config PATH]` | HTTP API plus MCP at `/mcp`. Defaults: host `127.0.0.1`, port `8000`, workers from `WORKERS` or 1 |
| `mailboxd watch [--config PATH]` | The webhook watcher. Exits with 2 on a config error or when no IMAP mailbox has a matching webhook, and with 1 when one of its threads dies |
| `mailboxd watch-health [--config PATH]` | Exits with 0 when a watcher using the same state database wrote a heartbeat in the last 90 seconds, 1 otherwise |
| `mailboxd --version` | Prints the version |

If you replace the image command with `mailboxd http`, add `--host 0.0.0.0`, or the server only listens inside the container.

## Environment variables

| Variable | Default | Used for |
| --- | --- | --- |
| `MAILBOXD_CONFIG` | `/etc/mailboxd/config.yaml` | Config file path. `--config` overrides it |
| `MAILBOXD_STATE_PATH` | `/var/lib/mailboxd/state.db` | SQLite state path when the config has no `state.path` |
| `WORKERS` | `2` in the image, `1` for the bare CLI | uvicorn worker processes |
| `SSL_CERT_FILE` | unset | A CA bundle to trust for private IMAP/SMTP servers. Mount it read-only |
| `TZ` | `UTC` in the image | Process time zone |
| Anything referenced as `${NAME}` | none | Passwords, tokens and webhook secrets interpolated into `config.yaml` |

`PORT` is read by Compose only, for the published host port. `MAILBOX_URL` and `MAILBOX_TOKEN` in `.env.example` are for the [agent integrations](integrations.md), not the server.

## Upgrade

Pull the new tag and recreate the container with the same state volume. Database migrations run on first use. A database written by a newer version is refused, so roll back by restoring a [backup](operations.md#backup-and-restore), not by starting an older image on newer state. Check the [upgrade notes](operations.md#upgrade-notes) and the changelog's "Breaking changes" sections first.

## Remove

```bash
docker compose --profile watch down
docker volume rm docker-mailbox_mailboxd-state   # Compose prefixes the project name; check `docker volume ls`
```

Removing the volume deletes the audit log, send counters, checkpoints and the webhook queue. Mail on your IMAP server is not touched.
