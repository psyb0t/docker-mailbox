---
title: "Webhooks"
description: "mailboxd watch posts HMAC-signed webhooks for new mail: setup, the event payload, signature verification, at-least-once retries, inspection and replay."
nav_order: 7
---

# Webhooks

`mailboxd watch` is a separate long-running process that notices new mail and POSTs a signed JSON event to your webhook targets. The HTTP API does not need it; run it only if you want push notifications.

## Setup

Add targets to `config.yaml`:

```yaml
watch:
  folder: INBOX          # the folder watched in every mailbox
  poll_seconds: 60       # only used when the server lacks IMAP IDLE (minimum 5)
  webhooks:
    - url: https://automation.example.com/hooks/mailboxd
      secret: ${MAILBOXD_WEBHOOK_SECRET}
      mailboxes: [contact, support]     # or "*"
```

Then start the watcher next to the server, with the same config and the same state volume:

```bash
docker compose --profile watch up -d
```

Run exactly one watcher per state database. It watches every mailbox that has IMAP and at least one matching webhook, and exits with status 2 if there are none.

## How it works

- One thread per watched mailbox keeps an IMAP connection open on `watch.folder`. It waits with IMAP IDLE, reissued every 25 minutes, or polls every `watch.poll_seconds` when the server lacks IDLE. On connection errors it reconnects with exponential backoff up to 5 minutes.
- For each new UID it reads the message headers, builds an event, and commits one delivery record per target together with its position in SQLite. A restart resumes from that position without missing or repeating mail.
- On first start, and after a UIDVALIDITY change, it starts at the current newest message; existing mail does not trigger events.
- A separate delivery thread sends due deliveries one at a time.
- Every 10 seconds the watcher writes a heartbeat. `mailboxd watch-health` exits 0 while the newest heartbeat is under 90 seconds old; the Compose healthcheck uses it.

## The event

```json
{
  "event": "message.new",
  "event_id": "3f1c…",
  "uidvalidity": "1",
  "mailbox": "contact",
  "folder": "INBOX",
  "uid": "1234",
  "from": "Someone <someone@example.org>",
  "to": "contact@cgs.example",
  "subject": "…",
  "date": "…",
  "message_id": "<…>",
  "authentication": {
    "spf": "pass",
    "dkim": "pass",
    "dmarc": "pass",
    "trusted": true
  },
  "signals": []
}
```

- `event_id` is a SHA-256 of mailbox, folder, UIDVALIDITY and UID, so it stays the same across retries, replays and restarts.
- `authentication` and `signals` mean the same as on [`get_message`](api.md#reading-a-message), with `dkim` reduced to one result (`pass` if any signature passed). Trust them only with `auth.trusted_authserv_id` set.
- No body or attachment is ever sent. Fetch the message with the API when you need it.

## Request headers

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Mailboxd-Event-ID` | The event ID, also in the body |
| `X-Mailboxd-Timestamp` | Unix seconds when this attempt was sent |
| `X-Mailboxd-Signature` | `sha256=` followed by the hex HMAC-SHA256 of the timestamp, a period and the raw body, keyed with the target's `secret` |

## Verify the signature

Check the signature over the exact request bytes before parsing anything:

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
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    return hmac.compare_digest(expected.encode(), signature.encode())
```

- Reject timestamps more than five minutes off.
- Store the event ID before acting on the event, and acknowledge a duplicate without acting twice. The timestamp changes on every retry; the event ID does not.

## Delivery and retries

Delivery is at least once. A worker leases a delivery for 60 seconds, makes one POST with a 10-second timeout, then records success or schedules a retry. A crash between the POST and the record means the receiver may see the same event again; deduplicate by event ID.

- Any `2xx` answer is success. Anything else, a timeout or a connection error is a failure.
- Redirects are refused and count as a failure.
- After the first failure, retries wait 30 seconds, 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours. A delivery that fails all seven attempts becomes `dead`.
- Targets fail independently.
- Secrets are read from the current config at send time and are not stored in delivery rows. Targets are matched by URL: if you change a target's URL, queued deliveries for the old URL are not sent to the new one and eventually die.
- Private and local network targets are allowed because only the operator configures them. Use HTTPS outside a trusted network.

## Inspect and replay

An admin token can list a mailbox's deliveries:

```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/mailboxes/contact/webhook-deliveries?limit=100&offset=0' \
  | jq
```

The response has `deliveries` (each with `id`, `event_id`, `status`, `attempts` and `next_attempt` in Unix seconds), `limit` (1 to 500), `offset` and `hasMore`. `status` is `pending`, `inflight`, `delivered` or `dead`.

To send one again:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/mailboxes/contact/webhook-deliveries/<id>/replay
```

Replay resets the delivery to `pending` with zero attempts. An in-flight delivery returns `409`. The event ID stays the same, so a receiver that already processed it will treat it as a duplicate. Over MCP, use `webhook_deliveries` and `replay_webhook`.
