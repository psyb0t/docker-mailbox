---
title: "Operations"
description: "Running mailboxd in production: the SQLite state volume, health checks and logs, send and delete reconciliation, backup and restore, and upgrade notes."
nav_order: 9
---

# Operations

## State

HTTP workers and `mailboxd watch` share one SQLite database at `state.path` (default `/var/lib/mailboxd/state.db`) in WAL mode. It holds:

- audit metadata;
- rolling send counters;
- checkpoints, per token, name, mailbox and folder;
- draft send claims;
- webhook delivery records, whose payloads hold message headers and authentication summaries.

Bodies, attachments and IMAP/SMTP credentials are never stored there.

Mount the same volume into the server and the watcher. The image creates its state directory owned by UID 10001 before the volume is mounted, so a fresh named volume works as is. For a host directory, give UID 10001 write access. All processes must run on the same host with the database on a local filesystem; SQLite WAL does not work across hosts or on network filesystems.

An empty state volume resets send counters and checkpoints and loses queued webhook deliveries. Schema migrations run in a transaction the first time the database is used. A database from a newer mailboxd version is refused.

Nothing prunes the audit table. Decide how long to keep it and trim or archive it yourself.

## Health and logs

| Check | What it tells you |
| --- | --- |
| `GET /health` | The HTTP server is up. No token needed. Used by the image and Compose healthchecks |
| `GET /health/deep` (admin) | Each visible mailbox's IMAP and SMTP login works, with latency |
| `mailboxd watch-health` | The watcher wrote a heartbeat in the last 90 seconds |

Logs go to stdout and stderr. `log_level` in the config sets the level. Message bodies and credentials are not logged. HTTP and MCP errors caused by the mail server say only `mail server operation failed`. To see the server's own message, check the `errors` list of `GET /inbox` or the `error` fields of `GET /health/deep`.

## Sending and deletion

Drafts stay in IMAP. Before sending a draft, the server records a claim on it (mailbox, Drafts folder, UIDVALIDITY, UID), so the same draft cannot be sent twice automatically. If SMTP fails after the claim, the outcome may be unknown: check the provider and the Sent folder before creating a replacement draft. Dry runs do not claim a draft or use send quota.

The SMTP send and the IMAP Sent copy cannot be one atomic step. A successful send can come back with `sent_copy_failed`, `answered_flag_failed` or `draft_not_removed`. Fix those by hand; do not send again. Send quota counts attempts, failed ones included.

Normal deletion moves to Trash with MOVE, or COPY followed by a UID-specific expunge. Without UIDPLUS, the original stays flagged `\Deleted`. Permanent deletion needs UIDPLUS, the `delete` scope and Trash as the source, and is refused otherwise.

## Backup and restore

Use SQLite's online backup API; copying a live database file without its WAL can produce a broken copy. Run the backup as the state owner, write it somewhere protected, and encrypt copies that leave the host.

```python
import sqlite3

with sqlite3.connect("/var/lib/mailboxd/state.db") as source:
    with sqlite3.connect("/protected-backup/mailboxd.db") as destination:
        source.backup(destination)
```

To restore: stop this deployment's server and watcher, keep a copy of the current state, put the backup in place owned by UID 10001, and start again. Restored webhook deliveries may be sent again, which receivers handle through event-ID deduplication. Try a restore on a test deployment before you need one in production.

## Upgrade notes

These changes matter when coming from older releases. The [changelog](https://github.com/psyb0t/docker-mailbox/blob/main/CHANGELOG.md) lists every release's "Breaking changes".

- **State is required.** mailboxd keeps operational metadata and needs a writable, persistent state volume. Keep the volume when recreating containers.
- **TLS is verified.** SSL and STARTTLS verify certificate chains and host names for IMAP and SMTP. Private servers need a certificate the container trusts; mount a CA bundle and set `SSL_CERT_FILE`. IMAP and SMTP sockets time out after 30 seconds.
- **Bare tokens still work** and still grant every scope on every mailbox. Replace them with named tokens that list their scopes.
- **Delete means Trash.** Normal delete moves to Trash instead of erasing. Permanent deletion needs an explicit flag, Trash as the source and UIDPLUS.
- **Senders must be identities.** Sender overrides must match a configured identity. Since v0.8.0, `*@domain` identities are patterns, not literal senders, and at least one concrete identity must exist.
- **Messages over 32 MiB** are refused by default since v0.7.0. Raise `limits.incoming_message_max_bytes` to read them.
- **Errors are structured.** HTTP errors include a stable `error.code`; MCP errors use `isError`. Raw source and attachment endpoints return bytes, not JSON.

There is no browser UI beyond FastAPI's generated `/docs` page.
