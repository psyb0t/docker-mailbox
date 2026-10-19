# mailbox

[![CI](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/docker-mailbox/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/version.svg)](https://github.com/psyb0t/docker-mailbox/releases)
[![license](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/mailbox?style=flat-square)](https://hub.docker.com/r/psyb0t/mailbox)

Your inboxes, on tap, with guard rails. Point this at as many email accounts as you want over IMAP + SMTP and get **one HTTP API and one MCP server on the same port** (MCP rides a streamable-HTTP channel at `/mcp`). Humans, scripts and AI agents read, sort, draft and send mail through it, and each bearer token carries only the **scopes** and **mailboxes** you gave it.

Version 0.6 is built to be plugged into an agent safely:

- **Scoped tokens.** A token that holds `read`, `draft` and `organize` cannot send or delete, and does not even see those tools.
- **Nothing is destroyed by accident.** Deleting moves a message to Trash. Permanent erasure needs the `delete` scope and only works from the Trash folder. The service never issues a folder-wide `EXPUNGE`.
- **Drafts in two steps.** An agent can prepare a reply; a human (or a token with `send`) sends it.
- **Send guard.** Hourly rate limit, dry-run mode, and a sender address that must be one of the identities you configured.
- **Phishing signals.** Messages come with parsed SPF/DKIM/DMARC verdicts (only from your own server), header mismatches, hidden-text and deceptive-link analysis, and a sanitized HTML body.
- **Audit log.** Every write, and every refused write, is recorded (never message bodies).

The mail itself is never stored here. The only state is a small SQLite file (audit log, send counters, "new since" checkpoints) on a volume. Connections to your mail server come up per request and die in a `finally`.

Stdlib `imaplib` + `smtplib` under the hood, FastAPI on top, the official MCP Python SDK (streamable HTTP, no stdio), `nh3` for HTML sanitizing, and a supply-chain `exclude-newer` gate so a malicious pip release published at 3am can't sneak in.

## Table of Contents

- [What's Inside](#whats-inside)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
  - [Tokens and scopes](#tokens-and-scopes)
  - [Identities and signatures](#identities-and-signatures)
  - [State volume](#state-volume)
  - [Limits, labels, send guard](#limits-labels-send-guard)
- [HTTP API](#http-api)
- [MCP server](#mcp-server)
- [Safety behaviour](#safety-behaviour)
- [Webhooks and `mailboxd watch`](#webhooks-and-mailboxd-watch)
- [Agent integrations](#agent-integrations)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

## What's Inside

| Surface         | The goods                                                                                                                                       |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **HTTP API**    | Unified `GET /inbox` across accounts, per-mailbox read/search/threads/attachments, labels, move, trash, replies, drafts, sending, audit log.     |
| **MCP server**  | Streamable-HTTP MCP at `/mcp`: the same operations as a flat tool set that takes `mailbox` as a parameter, filtered by the caller's scopes.       |
| **Bearer auth** | Named tokens with `scopes` and a `mailboxes` allow-list. No tokens configured = no access.                                                       |
| **Protocols**   | IMAP (SSL / STARTTLS / plain) and SMTP (SSL / STARTTLS / plain).                                                                                 |
| **Config**      | One YAML file with `${ENV}` interpolation. Each mailbox declares whichever subset of `{imap, smtp}` you need.                                    |
| **State**       | SQLite on a volume: audit log, send rate limits, checkpoints. No mail content, no queue, no cache.                                               |
| **Watcher**     | Optional `mailboxd watch` process: IMAP IDLE (or polling) and HMAC-signed webhooks for new mail.                                                 |

## Quick Start

1. Copy `config.example.yaml` to `config.yaml`. Add your mail credentials and replace the example tokens with fresh ones from `openssl rand -hex 32` (preferably through `${ENV}` variables).
2. Light it up (the named volume keeps the audit log and rate limits across restarts):

```bash
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e MAILBOXD_TOKEN_MILA="$(openssl rand -hex 32)" \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  -v mailboxd-state:/var/lib/mailboxd \
  psyb0t/mailbox:latest
```

3. Poke it:

```bash
TOKEN="paste-the-token-here"

curl -s http://localhost:8000/health | jq                                               # health is always open
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/mailboxes | jq
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/inbox?limit=5' | jq
```

### docker compose

The checked-in `docker-compose.yml` publishes on `127.0.0.1:${PORT:-8000}`, mounts `./config.yaml` (or `$MAILBOXD_CONFIG`) read-only and creates the `mailboxd-state` volume:

```yaml
services:
  mailboxd:
    image: psyb0t/mailbox:latest
    ports: ["127.0.0.1:8000:8000"]
    volumes:
      # config.yaml holds your IMAP/SMTP passwords AND your auth tokens —
      # gitignore it, lock down its filesystem perms, treat it like an SSH key.
      - ./config.yaml:/etc/mailboxd/config.yaml:ro
      - mailboxd-state:/var/lib/mailboxd
volumes:
  mailboxd-state:
```

The optional webhook watcher is a second service in the same file, off by default: `docker compose --profile watch up -d`.

## Configuration

### Container listener and host exposure

The image listens on `0.0.0.0:8000` inside the container so Docker port forwarding and other containers can reach it. The standalone `mailboxd http` CLI defaults to `127.0.0.1`. If you replace the image command with `mailboxd http`, add `--host 0.0.0.0`.

Publish host-local access with `-p 127.0.0.1:8000:8000`. `-p 8000:8000` publishes on all host interfaces: do that only for deliberate network access, behind HTTPS. The Compose file publishes on loopback by default.

### The config file

One YAML file, found via `--config`, `MAILBOXD_CONFIG`, or `/etc/mailboxd/config.yaml`. See [`config.example.yaml`](config.example.yaml) for every option with comments. Any string may contain `${NAME}`; a variable that is not set is a startup error.

```yaml
log_level: INFO

auth:
  trusted_authserv_id: mx.cgs.example   # see "Authentication-Results trust"
  tokens:
    - name: mila
      token: ${MAILBOXD_TOKEN_MILA}
      scopes: [read, draft, organize]
      mailboxes: [contact, support]
      dry_run: false

mailboxes:
  - name: contact                # URL-safe handle, unique; passed as the `mailbox` argument
    description: "Contact inbox"
    imap: { host: imap.example.com, username: contact@cgs.example, password: ${PW} }
    smtp:
      host: smtp.example.com
      username: contact@cgs.example
      password: ${PW}
      from_address: "L'équipe CGS <contact@cgs.example>"
      identities:
        - { address: contact@cgs.example, name: "L'équipe CGS", default: true }
        - { address: support@cgs.example, name: "Support CGS" }
      save_sent: true
      per_hour: 20
    folders: { sent: null, drafts: null, trash: null, junk: null }   # null = auto-detect
```

- **At least one mailbox**, and each needs at least one of `imap` / `smtp`.
- **`name`** matches `[a-zA-Z0-9_-]+` and is unique. It is a URL path segment and the value of the `mailbox` argument of MCP tools. It is not a tool prefix.
- **Defaults**: IMAP `993/ssl`, SMTP `587/starttls`.
- **Special folders** (`sent`, `drafts`, `trash`, `junk`): auto-detected from IMAP `SPECIAL-USE` flags, then from usual names (`Sent`, `Sent Items`, `Drafts`, `Trash`, `Corbeille`, `[Gmail]/Trash`, ...). Pin a name to override.
- **The config file holds plaintext passwords and bearer tokens** unless you use `${ENV}`. Treat it like a credential vault: gitignore it, `chmod 600`, mount it read-only.

### Tokens and scopes

Every request except `GET /health` needs `Authorization: Bearer <token>`. Tokens are compared in constant time. The same gate covers `/mcp`.

A token entry is either a mapping `{name, token, scopes, mailboxes, dry_run}` or, for backwards compatibility, a bare string. A bare string is named `legacy-N`, holds **every scope on every mailbox** and logs a warning at startup. `name` is what shows up in the audit log and in draft headers; it must be unique.

| Scope      | Allows                                                                                                              |
| ---------- | ------------------------------------------------------------------------------------------------------------------- |
| `read`     | List, search, read, raw source, attachments, threads, identities, stats, list drafts.                               |
| `draft`    | Create, update and delete drafts; prepare a reply as a draft.                                                       |
| `organize` | Mark read/unread, labels, move, create folder, delete to Trash.                                                     |
| `send`     | Send, reply directly, send a draft.                                                                                 |
| `delete`   | Permanent deletion (only from the Trash folder).                                                                    |
| `admin`    | `GET /audit`, `GET /health/deep` (and the `audit_log` / `health_check` MCP tools).                                  |

`mailboxes` is a list of mailbox names or `"*"`. A mailbox outside the list behaves exactly like one that does not exist (`404`), so a restricted token cannot even learn its name. `dry_run: true` forces every send by that token to be previewed only; a request can switch dry-run on but never off.

### Identities and signatures

`smtp.identities` lists the addresses a mailbox may send as, each with an optional display `name`, `signature_text` and `signature_html`. Without `identities`, one is derived from `from_address`. Sending, replying and drafting take an `identity` argument (an address); the legacy `from_address` field is still accepted but must match an identity. Anything else is refused with `422 invalid_identity` and the list of allowed addresses. Signatures are appended to the text (`-- ` separator) and HTML bodies unless the request passes `"signature": false`.

### State volume

`state.path` (env `MAILBOXD_STATE_PATH`, default `/var/lib/mailboxd/state.db`) is a SQLite database in WAL mode, shared by all uvicorn workers and by `mailboxd watch`. It stores the audit log (token name, action, mailbox, folder, UIDs, outcome, no bodies), the send-rate counters and named checkpoints. Mount a volume on `/var/lib/mailboxd`: without it, the data is lost with the container and rate limits reset on every restart. The image creates the directory owned by the unprivileged `mailboxd` user.

### Limits, labels, send guard

```yaml
send_guard: { dry_run: false, per_hour: 30 }   # rolling hour, per mailbox AND per token; 0 = unlimited
limits:
  attachment_max_bytes: 15728640               # largest attachment served
  outgoing_attachments_max_bytes: 20971520     # total attachments on one outgoing message
  mcp_inline_max_bytes: 524288                 # raw source / base64 attachment returned inline over MCP
labels: { suspect: $Suspect, a_traiter: $ATraiter, fait: $Fait }   # friendly name -> IMAP keyword
```

`smtp.per_hour` overrides `send_guard.per_hour` for one mailbox. Labels are plain IMAP keywords: the friendly names are what you pass to `set_labels` and the `label` search filter (plus the built-in `flagged`).

## HTTP API

Everything is JSON except raw source and attachment downloads. UIDs are used everywhere, never sequence numbers.

### Errors

Errors carry a stable code. The body is `{"error": {"code", "message", "details"?}, "detail": "<message>"}` (`detail` is kept for older clients). The `401` body stays `{"detail": "missing or invalid Bearer token"}` with `WWW-Authenticate: Bearer`.

| Status | `code`               | When                                                                                       |
| ------ | -------------------- | ------------------------------------------------------------------------------------------ |
| `401`  |                      | Missing or invalid bearer, or no server tokens configured.                                 |
| `403`  | `permission_denied`  | The token lacks the scope (`details.required_scope`).                                       |
| `404`  | `not_found`          | Unknown mailbox, message or part — or a mailbox this token may not see.                    |
| `409`  | `conflict`           | The mailbox lacks the protocol, or the action does not apply (e.g. permanent delete outside Trash). |
| `413`  | `too_large`          | Attachment above `limits`.                                                                  |
| `422`  | `validation_error` / `invalid_identity` | Bad request body, or a sender address that is not a configured identity.          |
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
| `GET /mailboxes/{n}/messages` | read | `?folder&limit&search&cursor` — headers, newest first, raw IMAP `search`. |
| `GET /mailboxes/{n}/search` | read | Structured search (same filters as `/inbox`) plus `recipient`, `label`, `has_attachments`, `cursor`, `since_cursor`, `checkpoint`. |
| `GET /mailboxes/{n}/messages/{uid}` | read | `?folder&reader&headers&safe_html` — decoded message with `authentication`, `html_analysis`. |
| `GET /mailboxes/{n}/messages/{uid}/raw` | read | `message/rfc822`, sent as an attachment. |
| `GET /mailboxes/{n}/messages/{uid}/attachments/{part_id}` | read | `application/octet-stream`, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, sanitized filename. |
| `GET /mailboxes/{n}/messages/{uid}/thread` | read | The conversation containing the message. |
| `GET /mailboxes/{n}/threads` | read | `?folder&limit` — recent conversations. |
| `DELETE /mailboxes/{n}/messages/{uid}` | organize / delete | `?folder&permanent=false` — moves to Trash; `permanent=true` needs `delete` and a Trash `folder`. |
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
| `POST /mailboxes/{n}/drafts/{uid}/send` | send | `{"dry_run"}` — send a draft, file a Sent copy, remove the draft. |
| `GET /mailboxes/{n}/identities` | read | Sender identities. |
| `GET /mailboxes/{n}/stats` | read | Per-folder message/unseen/recent counts and quota. |
| `GET /audit` | admin | `?since&action&token&mailbox&limit` |

### Unified inbox

`GET /inbox` hits **every visible IMAP mailbox in parallel**, runs the same search on each, merges newest-first and tags each result with its `mailbox` and `mailbox_address`. A failing account lands in `errors` instead of aborting the call.

| Query param                              | What it does                                                                                              |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `mailbox`                                | CSV of mailbox names **or** addresses. Omit for all visible IMAP mailboxes. No match → `404`.            |
| `from`, `to`, `subject`, `body`, `text`  | IMAP SEARCH predicates. `text` is full text across headers and body.                                       |
| `recipient`                              | Matches To, Cc and the `Delivered-To` / `X-Original-To` headers, so `contact@` vs `support@` can be told apart even for aliases and blind copies. |
| `since`, `before`                        | ISO (`2026-01-31`) or IMAP (`31-Jan-2026`) dates.                                                          |
| `unseen`, `seen`, `flagged`, `answered`  | Flag filters.                                                                                              |
| `label`                                  | Only messages carrying this label.                                                                         |
| `larger_than`, `smaller_than`            | Bytes.                                                                                                     |
| `folder`, `limit`                        | Folder (default `INBOX`); `limit` ≤ 500, default 50.                                                       |
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
| `audit_log` | `admin` | read-only |
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

- **Trash, not erase.** `DELETE` / `delete_message` moves messages to the Trash folder: `UID MOVE` when the server has `MOVE`, otherwise `UID COPY` + `\Deleted` + `UID EXPUNGE` (needs `UIDPLUS`). Without `UIDPLUS` the original stays flagged `\Deleted` rather than risk purging someone else's flagged mail. A global `EXPUNGE` is never sent. `permanent=true` needs the `delete` scope and only works when `folder` is the Trash folder (otherwise `409`).
- **Two-step drafts.** An agent with `draft` saves a message (APPEND with `\Draft \Seen`, header `X-Mailboxd-Draft-By: <token name>`). Someone with `send` reviews it and calls send-draft: the server strips `X-Mailboxd-*` headers, refreshes `Date`, re-validates the sender identity, delivers, files the Sent copy and removes the draft.
- **Dry run.** Sends are only previewed when `send_guard.dry_run`, the token's `dry_run` or the request's `dry_run` is true. A request can only turn dry-run on. A dry run is audited and does not count against the rate limit.
- **Rate limit.** `send_guard.per_hour` (override `smtp.per_hour`) caps real sends per rolling hour, separately per mailbox and per token, atomically across workers. Over the limit: `429`, `Retry-After`.
- **Identity check.** The From address must be a configured identity (`422 invalid_identity` otherwise). The server also validates recipients and refuses CR/LF in any header value.
- **No implicit "read".** Messages are fetched with `BODY.PEEK`; only `mark_seen` changes `\Seen`.
- **Audit.** Every mutation and every denied mutation is written to the state database; query it with `GET /audit` or the `audit_log` tool.
- **No cross-mailbox leaks.** A hidden mailbox is indistinguishable from a missing one.

### Authentication-Results trust

Anyone can put an `Authentication-Results` header in a mail they send. Set `auth.trusted_authserv_id` to the hostname **your own** receiving server writes in that header; only headers stamped with it are used for the `spf` / `dkim` / `dmarc` verdicts and `authentication.trusted` is `true`. Without it the topmost header is shown for information but reported `trusted: false` and the signal `authentication_untrusted` is raised. Make sure your MTA strips inbound `Authentication-Results` headers claiming your own id (most do).

## Webhooks and `mailboxd watch`

`mailboxd watch --config config.yaml` is a separate long-running process (run it once, not per uvicorn worker; in Compose: `docker compose --profile watch up -d`). It keeps an IMAP IDLE connection per mailbox (polling every `watch.poll_seconds` when IDLE is missing) and, for each new UID, POSTs JSON to every webhook whose `mailboxes` list matches. Positions are kept in the state database so restarts neither miss nor repeat mail.

```json
{ "event": "message.new", "mailbox": "contact", "folder": "INBOX", "uid": "1234",
  "from": "...", "to": "...", "subject": "...", "date": "...", "message_id": "<...>",
  "authentication": { "spf": "pass", "dkim": "pass", "dmarc": "pass", "trusted": true },
  "signals": [] }
```

No message body is ever sent. The request carries `X-Mailboxd-Signature: sha256=<hex HMAC-SHA256 of the raw body keyed with the webhook secret>` and `X-Mailboxd-Timestamp`. Delivery is retried 3 times with backoff. Always verify the signature over the **raw** bytes, with a constant-time compare:

```python
import hashlib, hmac

def verify(secret: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

Deduplicate on `message_id` if your receiver is not idempotent.

## Agent integrations

The [skill](.agents/skills/docker-mailbox) works in any agent that reads `.agents/skills/` (such as [peen](https://github.com/psyb0t/peen)), and installs natively in the clients below. Give agents a token with the narrowest scopes they need (typically `read`, `draft`, `organize`).

### Claude Code

```bash
claude plugin marketplace add psyb0t/agents
claude plugin install mailbox@psyb0t
```

Claude Code prompts for the mailboxd URL and bearer token. The token is stored in your OS keychain.

### Codex

```bash
codex plugin marketplace add psyb0t/agents
codex plugin add mailbox@psyb0t
```

Installed via the marketplace, Codex invokes the skill as `$mailbox:docker-mailbox`. Codex also picks the skill up automatically, no install required, in any repo containing `.agents/skills/` — there it invokes as plain `$docker-mailbox`.

### OpenClaw

The skill is published to ClawHub on every release:

```bash
openclaw skills install @psyb0t/docker-mailbox
```

For MCP clients that speak local stdio, the [`@psyb0t/mailbox`](.agents/plugins/mailbox) plugin bridges to mailboxd's `/mcp` endpoint:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Then set `MAILBOX_URL` and `MAILBOX_TOKEN` to your server URL and configured bearer token.

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

The HTTP routes and the MCP tools are thin layers over one `Service`, which resolves the mailbox for the caller's `Principal`, checks the scope, acts, and audits. The only persisted data is the SQLite state file.

## Development

The CLI defaults to loopback; Docker and `make run` explicitly listen on `0.0.0.0` inside the container. `make run` publishes on all host interfaces. Use the [host-local Docker example](#quick-start) when that is not intended. Bearer auth remains required in every mode.

```bash
make help          # list all targets
make dev-image     # build the sandboxed dev container
make shell         # drop into it
make run           # boot the server (REST + /mcp); mount CONFIG=path/to/config.yaml
make test          # full suite (unit + docker-in-docker integration)
make test-unit     # in-process only, fast feedback loop
make lint          # flake8 + mypy
make sec           # semgrep + bandit + pip-audit -> sec.sarif (reports, never fails)
make format        # isort + black
```

Tests run against an in-process IMAP mock (`tests/imap_mock.py`) that can emulate a modern server (`MOVE`, `UIDPLUS`, `SPECIAL-USE`, several folders, flags, `APPENDUID`) and records every command it receives, so tests can assert that no folder-wide `EXPUNGE` is ever issued.

### Package management — supply-chain defense

We use `uv`'s `exclude-newer` to refuse package versions newer than a fixed cutoff. Dependency changes set that cutoff to UTC midnight seven days ago. `pkg-lock` keeps the existing cutoff.

```bash
make pkg-add PKG=foo==1.2.3
make pkg-remove PKG=foo
make pkg-update PKG=foo
make pkg-lock
make pkg-upgrade
```

Don't hand-edit `[tool.uv].exclude-newer`. Let the targets do it.

## License

WTFPL — see [LICENSE](LICENSE). Do what the fuck you want.
