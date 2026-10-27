# HTTP API and MCP reference

## HTTP API

Everything is JSON except raw source and attachment downloads. UIDs are used everywhere, never sequence numbers.

### Errors

Errors carry an `error` object with `code`, `message` and optional `details`, plus the legacy `detail` string. The `401` body is `{"detail": "missing or invalid Bearer token"}` with `WWW-Authenticate: Bearer`.

| Status | `code`               | When                                                                                       |
| ------ | -------------------- | ------------------------------------------------------------------------------------------ |
| `401`  |                      | Missing or invalid bearer, or no server tokens configured.                                 |
| `403`  | `permission_denied`  | The token lacks the scope (`details.required_scope`).                                       |
| `404`  | `not_found`          | Unknown mailbox, message or part, or a mailbox outside the token's allowlist. |
| `409`  | `conflict`           | The mailbox lacks the protocol, or the action does not apply (e.g. permanent delete outside Trash). |
| `413`  | `too_large`          | Request body, attachment or message read from IMAP exceeds its configured size limit. |
| `422`  | `validation_error` / `invalid_identity` | Bad request body, or a sender address not authorized by configured identities. |
| `429`  | `rate_limited`       | Hourly send limit reached. `Retry-After` header and `details.retry_after`.                  |
| `502`  | `imap_error` / `smtp_error` | The upstream server said no.                                                         |

### Routes

| Method & path | Scope | Does |
| --- | --- | --- |
| `GET /health` | none | Liveness, always open. |
| `GET /health/deep` | admin | Per mailbox: IMAP and SMTP connect/auth with latencies. |
| `GET /mailboxes` | any | Visible mailboxes with address, protocols and identities. |
| `GET /inbox` | read | Unified, newest-first feed across mailboxes. |
| `GET /mailboxes/{n}/folders` | read | Folders with delimiter, flags and detected special use. |
| `POST /mailboxes/{n}/folders` | organize | `{"name"}` creates (and subscribes to) a folder. |
| `GET /mailboxes/{n}/messages` | read | `?folder&limit&search&cursor`. headers, newest first, raw IMAP `search`. |
| `GET /mailboxes/{n}/search` | read | Structured search (same filters as `/inbox`) plus `recipient`, `label`, `has_attachments`, `cursor`, `since_cursor`, `checkpoint`. |
| `GET /mailboxes/{n}/messages/{uid}` | read | `?folder&reader&headers&safe_html`. decoded message with `authentication`, `html_analysis`. |
| `GET /mailboxes/{n}/messages/{uid}/raw` | read | `message/rfc822`, sent as an attachment. |
| `GET /mailboxes/{n}/messages/{uid}/attachments/{part_id}` | read | `application/octet-stream`, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, sanitized filename. |
| `GET /mailboxes/{n}/messages/{uid}/thread` | read | The conversation containing the message. |
| `GET /mailboxes/{n}/threads` | read | `?folder&limit`. recent conversations. |
| `DELETE /mailboxes/{n}/messages/{uid}` | organize / delete | `?folder&permanent=false`. moves to Trash; `permanent=true` needs `delete` and a Trash `folder`. |
| `POST /mailboxes/{n}/messages/delete` | organize / delete | Batch: `{"uids": [...], "permanent": false}`. |
| `POST /mailboxes/{n}/messages/{uid}/seen` | organize | `{"seen": true}` |
| `POST /mailboxes/{n}/messages/seen` | organize | Batch: `{"uids": [...], "seen": true}` |
| `POST /mailboxes/{n}/messages/labels` | organize | `{"uids", "add": [], "remove": []}` (`?folder`). |
| `POST /mailboxes/{n}/messages/move` | organize | `{"uids", "to_folder"}` (`?folder`). |
| `POST /mailboxes/{n}/send` | send | Send a new message. |
| `POST /mailboxes/{n}/messages/{uid}/reply` | send, or draft with `"draft": true` | Reply in the thread. |
| `GET /mailboxes/{n}/drafts` | read | List drafts. |
| `POST /mailboxes/{n}/drafts` | draft | Save a draft. |
| `PUT /mailboxes/{n}/drafts/{uid}` | draft | Replace a draft (the UID changes; the old one is removed). |
| `DELETE /mailboxes/{n}/drafts/{uid}` | draft | Delete a draft. |
| `POST /mailboxes/{n}/drafts/{uid}/send` | send | `{"dry_run"}`. send a draft, file a Sent copy, remove the draft. |
| `GET /mailboxes/{n}/identities` | read | Sender identities. |
| `GET /mailboxes/{n}/stats` | read | Per-folder message/unseen/recent counts and quota. |
| `GET /audit` | admin | `?since&action&token&mailbox&limit` |
| `GET /mailboxes/{n}/webhook-deliveries` | admin | `?limit&offset`. Delivery records for a permitted mailbox. |
| `POST /mailboxes/{n}/webhook-deliveries/{id}/replay` | admin | Queue a recorded delivery again; rejects an active lease. |

### Unified inbox

`GET /inbox` searches every visible IMAP mailbox in parallel, merges results newest-first and tags each result with its `mailbox` and `mailbox_address`. Account failures appear in `errors`; other accounts still return results.

| Query param                              | What it does                                                                                              |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `mailbox`                                | CSV of mailbox names **or** addresses. Omit for all visible IMAP mailboxes. No match → `404`.            |
| `from`, `to`, `subject`, `body`, `text`  | IMAP SEARCH predicates. `text` is full text across headers and body.                                       |
| `recipient`                              | Matches To, Cc and the `Delivered-To` / `X-Original-To` headers, so `contact@` vs `support@` can be told apart even for aliases and blind copies. |
| `since`, `before`                        | ISO (`2026-01-31`) or IMAP (`31-Jan-2026`) dates.                                                          |
| `unseen`, `seen`, `flagged`, `answered`  | Flag filters.                                                                                              |
| `label`                                  | Only messages carrying this label.                                                                         |
| `larger_than`, `smaller_than`            | Bytes.                                                                                                     |
| `folder`, `limit`                        | Folder (default `INBOX`); `limit` is per mailbox, at most 500, default 50. Unified results can exceed it. |
| `since_cursor`, `checkpoint`             | "New mail only", see below.                                                                                |

```json
{
  "messages": [
    { "uid": "1234", "mailbox": "contact", "mailbox_address": "contact@cgs.example",
      "from": "boss@corp.com", "subject": "weekly sync", "date": "...", "flags": ["\\Seen"] }
  ],
  "errors": [{ "mailbox": "support", "error": "login failed: ..." }],
  "next_since_cursor": "eyJjb250YWN0IjpbIjEiLDEyMzRdfQ",
  "positions": { "contact": { "uidvalidity": "1", "max_uid": 1234, "folder": "INBOX" } }
}
```

### Paging and "new mail only"

- **Backwards paging.** Single-mailbox listings return `next_cursor` (opaque). Pass it back as `cursor` to get the next, older page. `total` is the number of matches.
- **New since last time.** Pass the `next_since_cursor` of a previous response as `since_cursor`, or give a name as `checkpoint=` and the server remembers the position for you (per token, mailbox and folder, in the state database). Only mail newer than the position is returned; the oldest unseen messages come first so a checkpoint never skips mail.
- **UIDVALIDITY.** If the server reset its UID numbering since the cursor was issued, the response says `"reset": true` and starts from scratch.

### Reading a message

`GET /mailboxes/{n}/messages/{uid}` returns the decoded message (`body_text`, `body_html`, `attachments` with `part_id`, size, `sha256`) plus:

- `authentication`: `{trusted, spf, dkim, dmarc, arc, raw, signals}` (see [Authentication-Results trust](#authentication-results-trust)).
- `html_analysis` (when there is an HTML body): `hidden_text` (display:none, zero font size, white-on-white, ...) and `suspicious_links` (link text that names another domain, punycode, IP literals, URL shorteners, `javascript:`).
- `?headers=true`: every header in wire order as `[name, value]` pairs.
- `?safe_html=true`: `body_html_safe`, an allow-list-sanitized body (scripts, event handlers and remote images removed; links kept with `rel="noopener noreferrer nofollow"`).
- `?reader=true`: `body_reader`, the HTML flattened to clean markdown with [html2text](https://github.com/Alir3z4/html2text) (falls back to the plain text part).

Reading never marks a message as read: the server uses `BODY.PEEK`.

`security signals` that can appear: `reply_to_domain_mismatch`, `return_path_domain_mismatch`, `display_name_spoof`, `dkim_not_aligned`, `dkim_fail`, `spf_fail` / `spf_softfail`, `dmarc_fail`, `no_authentication_results`, `authentication_untrusted`.

### Sending, replying and drafts

`POST /mailboxes/{n}/send` (and `POST .../drafts`, which takes the same body):

```json
{
  "to": ["dest@example.com"], "cc": [], "bcc": ["hidden@example.com"],
  "subject": "hi",
  "body_text": "plain text body", "body_html": "<p>optional html body</p>",
  "identity": "support@cgs.example",
  "reply_to": "noreply@example.com",
  "attachments": [
    { "filename": "a.pdf", "content_type": "application/pdf", "data_base64": "..." },
    { "from_message": { "uid": "1234", "part_id": "3", "folder": "INBOX" } }
  ],
  "signature": true,
  "dry_run": false
}
```

At least one of `body_text` / `body_html` is required (both = `multipart/alternative`). Bcc recipients are delivered but never appear in the headers on the wire; the copy filed in Sent keeps them.

`POST .../messages/{uid}/reply` body: `{"body_text", "body_html", "reply_all": false, "identity", "attachments": [], "quote": true, "draft": false, "signature": true, "dry_run": false}`. The server, not the caller, fills the recipients (`Reply-To`, else `From`; with `reply_all` also To/Cc minus your own addresses), the `Re:` subject, `In-Reply-To` and `References`. Replying to your own sent message goes to the original recipients. After a real send the original gets `\Answered`. With `"draft": true` the reply is saved in Drafts and only needs the `draft` scope.

With no explicit `identity`, replies prefer an exact configured identity in To/Cc, then the first To/Cc address matching a configured `*@domain` pattern, then the concrete default. Pattern matches inherit the configured name and signatures. Reply-all excludes matching aliases. See [identities and signatures](configuration.md#identities-and-signatures) for configuration and domain boundaries.

A send result looks like:

```json
{ "dry_run": false, "from": "...", "to": ["..."], "cc": [], "subject": "...",
  "message_id": "<...@cgs.example>", "sent_copy": { "folder": "Sent", "uid": "42" },
  "warnings": [] }
```

`warnings: ["sent_copy_failed"]` means the mail went out but filing the Sent copy failed. In dry-run mode `preview` holds the headers and text body that would have been sent.

## MCP server

The same operations as MCP tools over **streamable HTTP** at `/mcp` (same port, same bearer). One flat tool set: every per-mailbox tool takes `mailbox` (a name or an address). `tools/list` only returns the tools the caller's scopes allow, and `call_tool` checks again.

| Tool | Scope | Annotations |
| --- | --- | --- |
| `mailboxes` | any | read-only |
| `inbox`, `list_folders`, `list_messages`, `search`, `get_message`, `get_raw_message`, `get_attachment`, `get_thread`, `list_threads`, `identities`, `list_drafts`, `mailbox_stats` | `read` | read-only, idempotent |
| `create_draft` | `draft` | |
| `update_draft`, `delete_draft` | `draft` | destructive (replaces/removes only that draft) |
| `delete_message` | `organize` (Trash), `delete` for `permanent` | destructive. The `permanent` argument is hidden without `delete`. |
| `mark_seen`, `set_labels`, `move_messages`, `create_folder` | `organize` | idempotent |
| `reply` | `send`, or `draft` with `draft=true` | destructive, open-world |
| `send`, `send_draft` | `send` | destructive, open-world |
| `audit_log`, `webhook_deliveries` | `admin` | read-only |
| `replay_webhook` | `admin` | destructive, open-world |
| `health_check` | `admin` | read-only, open-world |

- `get_raw_message` returns the source as text, truncated to `limits.mcp_inline_max_bytes` (`"truncated": true`); `get_attachment` returns base64 up to the same cap.
- Successful calls return the JSON both as text and as `structuredContent`.
- Errors come back as a tool result with `isError: true` and `{"error": {"code", "message", "details"?}}` using the same codes as the HTTP API (`permission_denied`, `not_found`, `rate_limited`, `invalid_identity`, `validation_error`, `imap_error`, `smtp_error`).

Discovery flow for an agent: call `mailboxes`, then pass the chosen name or address as `mailbox`. For cross-account reads use `inbox`.

### Transport

Plain streamable-HTTP MCP at `/mcp`: `POST` ships requests, `GET` opens the SSE stream, `DELETE` ends the session. There is **no stdio transport**. Most MCP hosts take a `.mcp.json` like this:

```json
{
  "mcpServers": {
    "mailbox": {
      "transport": "streamable-http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN_HERE" }
    }
  }
}
```

## Safety behaviour

- **Deletion.** `DELETE` / `delete_message` moves messages to Trash using `UID MOVE`, or `UID COPY` followed by `\Deleted` and `UID EXPUNGE`. Without `UIDPLUS`, the fallback leaves the source flagged `\Deleted`. The service never sends a global `EXPUNGE`. Permanent deletion requires `permanent=true`, the `delete` scope, Trash as the source and UIDPLUS; unsupported requests return `409`.
- **Two-step drafts.** An agent with `draft` saves a message (APPEND with `\Draft \Seen`, header `X-Mailboxd-Draft-By: <token name>`). Someone with `send` reviews it and calls send-draft: the server strips `X-Mailboxd-*` headers, refreshes `Date`, re-validates the sender identity, delivers, files the Sent copy and removes the draft.
- **Dry run.** Sends are only previewed when `send_guard.dry_run`, the token's `dry_run` or the request's `dry_run` is true. A request can only turn dry-run on. A dry run is audited and does not count against the rate limit.
- **Rate limit.** `send_guard.per_hour` (override `smtp.per_hour`) caps real sends per rolling hour, separately per mailbox and per token, atomically across workers. Over the limit: `429`, `Retry-After`.
- **Identity check.** The concrete From address must match an exact configured identity or an opt-in `*@domain` pattern (`422 invalid_identity` otherwise). A wildcard string cannot be sent literally. The server also validates recipients and refuses CR/LF in any header value.
- **No implicit "read".** Messages are fetched with `BODY.PEEK`; only `mark_seen` changes `\Seen`.
- **Audit.** Every mutation and every denied mutation is written to the state database; query it with `GET /audit` or the `audit_log` tool.
- **No cross-mailbox leaks.** A hidden mailbox is indistinguishable from a missing one.

### Authentication-Results trust

Anyone can put an `Authentication-Results` header in a mail they send. Set `auth.trusted_authserv_id` to the hostname **your own** receiving server writes in that header; only headers stamped with it are used for the `spf` / `dkim` / `dmarc` verdicts and `authentication.trusted` is `true`. Without it the topmost header is shown for information but reported `trusted: false` and the signal `authentication_untrusted` is raised. Make sure your MTA strips inbound `Authentication-Results` headers claiming your own id (most do).

## Webhooks and `mailboxd watch`

`mailboxd watch --config config.yaml` is a separate long-running process (run it once, not per uvicorn worker; in Compose: `docker compose --profile watch up -d`). It keeps an IMAP IDLE connection per mailbox (polling every `watch.poll_seconds` when IDLE is missing) and, for each new UID, POSTs JSON to every webhook whose `mailboxes` list matches. Discovery checkpoints and per-recipient delivery records commit together in SQLite. Restarts resume pending work. Delivery is at least once; receivers deduplicate by event_id. Initial startup begins at the current mailbox position without replaying history.

```json
{ "event": "message.new", "event_id": "EXAMPLE-EVENT-ID", "uidvalidity": "1",
  "mailbox": "contact", "folder": "INBOX", "uid": "1234",
  "from": "...", "to": "...", "subject": "...", "date": "...", "message_id": "<...>",
  "authentication": { "spf": "pass", "dkim": "pass", "dmarc": "pass", "trusted": true },
  "signals": [] }
```

No message body is sent. SQLite retains notification metadata for retries and inspection. Requests carry a stable `X-Mailboxd-Event-ID`, `X-Mailboxd-Timestamp`, and `X-Mailboxd-Signature`. HMAC-SHA256 covers the ASCII timestamp, a literal period, and raw body bytes. Receivers must verify it in constant time, enforce a five-minute timestamp window and deduplicate event IDs.

After the initial attempt, retries wait 30 seconds, 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours. Exhausted deliveries remain marked `dead`. Recipient failures are independent and redirects are refused. Administrators inspect `GET /mailboxes/<name>/webhook-deliveries?limit=100&offset=0` and replay with `POST /mailboxes/<name>/webhook-deliveries/<id>/replay`. MCP exposes `webhook_deliveries` and `replay_webhook`. See [operations and recovery](operations.md).



[Project overview](../README.md)
