# Operations and recovery

## Persistence

HTTP workers and `mailboxd watch` use one SQLite database at `MAILBOXD_STATE_PATH`, default `/var/lib/mailboxd/state.db`. The database holds audit metadata, rolling send counters, per-token checkpoints, draft send claims and webhook delivery records. Queued notifications contain message headers and authentication summaries. Bodies, attachments and IMAP/SMTP credentials are not stored in SQLite.

Mount the same local volume into both services. The image runs as UID 10001 and creates its state directory before mounting a named volume. For a host bind mount, grant that UID write access. SQLite WAL requires processes on the same host and a local filesystem. A shared network volume across multiple hosts is unsupported.

An empty state volume resets quotas and checkpoints and loses queued deliveries. Initial watcher startup records the current mailbox position, so historical mail does not trigger notifications. UIDVALIDITY changes also start at the current position. Schema migrations run transactionally when state is first used; a database from a newer schema version is rejected.

Audit access requires `admin` and respects mailbox allowlists. Audit metadata records actor names, actions, target identifiers and outcomes. It is operational history, not an independently tamper-proof compliance log. Limit filesystem access and define a retention policy appropriate to the deployment.

## Webhook delivery

Configure targets under `watch.webhooks`, then run `docker compose --profile watch up -d`. The watcher uses IMAP IDLE when available and polls otherwise. The HTTP service and watcher share SQLite; they do not need separate databases.

The watcher records a heartbeat while its discovery and delivery threads are alive. `mailboxd watch-health` exits successfully when a heartbeat is no older than 90 seconds. Compose uses this command for its watcher healthcheck. API and MCP request bodies are limited to `limits.request_max_bytes`, default 32 MiB. Messages read from IMAP are limited to `limits.incoming_message_max_bytes`, also 32 MiB by default.

Discovery atomically commits the checkpoint and per-recipient notification records. A worker claims a delivery with a lease, sends once, then records success or schedules retry. A crash can leave the receiver with a successful request and an unrecorded outcome. Delivery is therefore at least once. Receivers must deduplicate by `event_id`. A lease expires after 60 seconds; one outbound attempt times out after 10 seconds.

After the first failure, retry delays are 30 seconds, 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours. Exhausted records become `dead`. A removed recipient remains undelivered and eventually becomes dead-lettered; changing its URL does not forward old metadata to another endpoint. Secrets come from current configuration and are not stored in delivery rows. HTTP redirects are refused. Private HTTP endpoints are supported because destinations are configured by the operator, not supplied by API callers. Use HTTPS outside a trusted local network.

Requests carry these headers:

| Header | Value |
| --- | --- |
| `X-Mailboxd-Event-ID` | Stable event ID, also present in the JSON body |
| `X-Mailboxd-Timestamp` | Unix seconds for this attempt |
| `X-Mailboxd-Signature` | `sha256=` followed by HMAC-SHA256 of timestamp, period and raw body |

Verification in a receiver must use its configured secret and the exact request bytes:

```python
import hashlib
import hmac
import time

def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    try:
        sent_at = int(timestamp)
        if abs(time.time() - sent_at) > 300:
            return False
        signed = timestamp.encode("ascii") + b"." + body
    except (ValueError, UnicodeEncodeError):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())
```

Authenticate the receiver before parsing the event. Persist its event ID before applying effects and acknowledge duplicates without repeating them. The timestamp is refreshed on retries while the event ID stays stable.

An administrator can inspect a permitted mailbox with `GET /mailboxes/<name>/webhook-deliveries?limit=100&offset=0`. The response contains `deliveries`, `limit`, `offset` and `hasMore`. Use `POST /mailboxes/<name>/webhook-deliveries/<id>/replay` to reset a recorded delivery to pending. In-flight records return 409. MCP exposes the same operations through `webhook_deliveries` and `replay_webhook`. Replaying retains the event ID and can trigger receiver-side deduplication.

## Sending and deletion

Drafts remain in IMAP. Sending a draft records a claim keyed by mailbox, Drafts location, UIDVALIDITY and UID before SMTP delivery. The same draft cannot be automatically sent twice. If SMTP fails after the claim, the outcome can be ambiguous: inspect the SMTP provider and Sent mail before creating a replacement draft. A dry run does not claim a draft or consume send quota.

The SMTP transaction and IMAP Sent filing cannot be atomic. A successful send can return `sent_copy_failed`, `answered_flag_failed` or `draft_not_removed`. These warnings require reconciliation, not another send. The shared hourly quota counts attempted deliveries, including failed attempts, to avoid bypassing the limit with repeated failures.

Deletion normally moves messages to Trash using MOVE, or COPY followed by UID-specific expunge. Without UIDPLUS, the fallback preserves the source marked Deleted. Permanent deletion requires UIDPLUS, the `delete` scope and Trash as the source. The service refuses unsupported permanent deletion without issuing a global EXPUNGE.

## Backup and restore

Use SQLite's online backup API rather than copying a live database file without its WAL. Run the backup as the state owner, write to a separate protected destination and encrypt off-host copies. Schedule backups according to the acceptable loss of audit and delivery history. Keep secrets and backups outside the image build context.

```python
import sqlite3

with sqlite3.connect("/var/lib/mailboxd/state.db") as source:
    with sqlite3.connect("/protected-backup/mailboxd.db") as destination:
        source.backup(destination)
```

To restore, stop only this deployment's HTTP and watch processes, retain a copy of the existing state, restore the snapshot with UID 10001 ownership, and restart. Check SQLite integrity, audit visibility, quota behavior and pending deliveries in an isolated environment before restoring production. Recovered notifications may be delivered again, so receivers still need event-ID deduplication. Test restores periodically and expire backups according to a documented retention policy.

## Migration

SSL and STARTTLS now verify certificate chains and hostnames for both IMAP and SMTP. Private mail servers must present a certificate trusted by the container. Mount a private CA bundle read-only and set `SSL_CERT_FILE` when needed. Self-signed certificates are rejected unless explicitly trusted; there is no verification-disable setting. IMAP socket operations have a 30-second timeout.

Existing bare token strings continue to grant all scopes across all mailboxes. Replace them with named tokens and explicit scopes for restricted agents. Keep the state volume when updating or recreating containers. Mailboxd now retains operational metadata and requires writable persistent storage; the previous stateless deployment description no longer applies.

Normal delete changes from permanent erasure to moving to Trash. Permanent deletion now requires an explicit flag, a Trash source and UIDPLUS. Sender overrides must match a configured identity. HTTP errors include a stable structured code; MCP errors use `isError`. New message fields are additive. Raw source and attachment endpoints return bytes, rather than JSON.

Use `make lint`, `make test-coverage`, `make audit` and `make build` for verification. `make sec` emits security findings for inspection and does not gate its exit status. No browser UI is shipped.
