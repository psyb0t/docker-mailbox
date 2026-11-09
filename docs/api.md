---
title: "HTTP API"
description: "mailboxd HTTP API reference: every route with its scope, query parameters, request bodies, response fields, paging cursors and error codes."
nav_order: 4
---

# HTTP API

Everything is JSON except raw source and attachment downloads. Messages are addressed by IMAP UID, never by sequence number. A UID is unique within one folder, so routes that take a UID also take `?folder=`, which defaults to the mailbox's `imap.default_folder` (`INBOX` unless configured).

`{n}` below is a mailbox name, or its IMAP or SMTP username when that is an address.

## Authentication

Send `Authorization: Bearer <token>` on every request except `GET /health`. A missing or wrong token gets:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer

{"detail":"missing or invalid Bearer token"}
```

The token's scopes and mailbox list decide what it can do; see [Tokens and scopes](configuration.md#tokens-and-scopes). FastAPI's generated schema is served at `/openapi.json`, with Swagger UI at `/docs` and ReDoc at `/redoc`. They need the bearer token like every other route, so fetch the schema with `curl -H "Authorization: Bearer $TOKEN"`.

## Errors

Errors carry an `error` object with `code`, `message` and optional `details`, plus a `detail` field for older clients:

```json
{"error": {"code": "permission_denied", "message": "token 'drafter' lacks the 'send' scope",
           "details": {"required_scope": "send"}},
 "detail": "token 'drafter' lacks the 'send' scope"}
```

| Status | `code` | When |
| --- | --- | --- |
| `401` | none | Missing or invalid bearer token, or no tokens configured |
| `403` | `permission_denied` | The token lacks the scope. `details.required_scope` names it |
| `404` | `not_found` | Unknown mailbox, message, part, folder or delivery, or a mailbox outside the token's list |
| `409` | `conflict` | The mailbox lacks IMAP or SMTP, or the action does not apply here (see [Conflicts](#conflicts)) |
| `413` | `too_large` | Request body, attachment or message over its [configured limit](configuration.md#limits) |
| `422` | `validation_error` | Bad body or parameters. FastAPI validation errors list each problem in `details.errors` |
| `422` | `invalid_identity` | Sender address not covered by the configured identities. `details.allowed` lists them |
| `429` | `rate_limited` | Hourly send limit reached. `Retry-After` header and `details.retry_after`, in seconds |
| `502` | `imap_error` / `smtp_error` | The mail server refused a command or the login. The message is always `mail server operation failed` |

A mail server that cannot be reached at all (connection refused, DNS failure, timeout) currently returns a plain-text `500 Internal Server Error` on most routes instead of `502`. `/inbox` reports it per account in `errors`, and sends report it as `smtp_error`.

### Conflicts

`409 conflict` is returned when:

- the mailbox has no `imap` (reading, organizing, drafts) or no `smtp` (sending, identities);
- a normal delete targets messages already in Trash, or a permanent delete targets any other folder;
- permanent deletion is requested on a server without `UIDPLUS`;
- `POST /mailboxes/{n}/folders` names a folder that exists;
- a draft was already sent once, or an earlier send attempt for it had an unknown outcome;
- a webhook delivery is being replayed while it is in flight.

## Routes

| Method and path | Scope | What it does |
| --- | --- | --- |
| `GET /health` | none | `{"ok": true, "version": "…"}` |
| `GET /health/deep` | admin | Connects and logs in to each visible mailbox's IMAP and SMTP servers |
| `GET /mailboxes` | any token | Visible mailboxes with address, protocols and identities |
| `GET /inbox` | read | Unified newest-first feed across mailboxes |
| `GET /mailboxes/{n}/folders` | read | Folders with delimiter, flags and detected special use |
| `POST /mailboxes/{n}/folders` | organize | `{"name": "…"}` creates a folder and subscribes to it |
| `GET /mailboxes/{n}/messages` | read | One folder, newest first, with a raw IMAP `search` string |
| `GET /mailboxes/{n}/search` | read | Structured search in one folder |
| `GET /mailboxes/{n}/messages/{uid}` | read | The decoded message with security analysis |
| `GET /mailboxes/{n}/messages/{uid}/raw` | read | The RFC 822 source, as a download |
| `GET /mailboxes/{n}/messages/{uid}/attachments/{part_id}` | read | One attachment, as a download |
| `GET /mailboxes/{n}/messages/{uid}/thread` | read | The conversation containing the message |
| `GET /mailboxes/{n}/threads` | read | Recent conversations |
| `GET /mailboxes/{n}/identities` | read | Sender identities |
| `GET /mailboxes/{n}/stats` | read | Per-folder counts and quota |
| `DELETE /mailboxes/{n}/messages/{uid}` | organize, or delete with `permanent=true` | Move to Trash, or erase from Trash |
| `POST /mailboxes/{n}/messages/delete` | organize, or delete with `permanent` | Batch: `{"uids": […], "permanent": false}` |
| `POST /mailboxes/{n}/messages/{uid}/seen` | organize | `{"seen": true}` (the default) or `false` |
| `POST /mailboxes/{n}/messages/seen` | organize | Batch: `{"uids": […], "seen": true}` |
| `POST /mailboxes/{n}/messages/labels` | organize | `{"uids": […], "add": […], "remove": […]}` |
| `POST /mailboxes/{n}/messages/move` | organize | `{"uids": […], "to_folder": "…"}`. The folder must exist |
| `POST /mailboxes/{n}/send` | send | Send a new message |
| `POST /mailboxes/{n}/messages/{uid}/reply` | send, or draft with `"draft": true` | Reply in the thread |
| `GET /mailboxes/{n}/drafts` | read | List drafts, `?limit=` (1 to 500, default 50) |
| `POST /mailboxes/{n}/drafts` | draft | Save a draft |
| `PUT /mailboxes/{n}/drafts/{uid}` | draft | Replace a draft. The UID changes |
| `DELETE /mailboxes/{n}/drafts/{uid}` | draft | Delete a draft |
| `POST /mailboxes/{n}/drafts/{uid}/send` | send | `{"dry_run": false}`. Send a draft |
| `GET /audit` | admin | Audit log, see [below](#audit-log) |
| `GET /mailboxes/{n}/webhook-deliveries` | admin | Webhook delivery records, see [Webhooks](webhooks.md#inspect-and-replay) |
| `POST /mailboxes/{n}/webhook-deliveries/{id}/replay` | admin | Queue a delivery again |

HTTP batch bodies have no UID count limit beyond `limits.request_max_bytes`; MCP caps them at 500. Every UID must be numeric.

## Listing and searching

### Search filters

`GET /inbox` and `GET /mailboxes/{n}/search` take the same filters. All are optional and combine with AND.

| Parameter | What it matches |
| --- | --- |
| `from`, `to`, `subject` | That header contains the text |
| `body` | The body contains the text |
| `text` | Headers or body contain the text |
| `recipient` | To, Cc, `Delivered-To` or `X-Original-To` contains the text, so mail for `contact@` and `support@` aliases, including blind copies, can be told apart |
| `message_id` | The `Message-ID` header |
| `since`, `before` | Date on or after / before. ISO (`2026-01-31`) or IMAP (`31-Jan-2026`) |
| `unseen`, `seen`, `flagged`, `answered` | `true` to require the flag. `false` is the same as leaving it out |
| `label` | A friendly label name from the config, or `flagged` |
| `keyword`, `unkeyword` | Has / lacks a raw IMAP keyword |
| `larger_than`, `smaller_than` | Size in bytes |
| `folder` | Folder to search. Default: the mailbox's `default_folder` |
| `limit` | 1 to 500, default 50. On `/inbox` the limit applies per mailbox, so the merged list can be longer |

Matching is done by the IMAP server, so substring and case rules are the server's. Non-ASCII terms are sent with `CHARSET UTF-8`.

`/search` also takes `has_attachments=true|false`. With it, `total` is `-1` because counting would mean scanning every message.

### Unified inbox

`GET /inbox` searches every visible IMAP mailbox in parallel, merges the results newest first and tags each message with `mailbox`, `mailbox_address` (the IMAP username) and `folder`. `?mailbox=` takes a comma-separated list of names or addresses; one that does not match returns `404`. A failing account goes into `errors` and the others still answer.

```json
{
  "messages": [
    { "uid": "1234", "mailbox": "contact", "mailbox_address": "contact@cgs.example", "folder": "INBOX",
      "from": "boss@corp.com", "to": "contact@cgs.example", "cc": "", "subject": "weekly sync",
      "date": "…", "message_id": "<…>", "in_reply_to": "", "references": [],
      "size": 4821, "flags": ["\\Seen"], "labels": [] }
  ],
  "errors": [{ "mailbox": "support", "error": "login failed: …" }],
  "next_since_cursor": "eyJjb250YWN0IjpbIjEiLDEyMzRdfQ",
  "positions": { "contact": { "uidvalidity": "1", "max_uid": 1234, "next_after_uid": 1234,
                              "more_new": false, "folder": "INBOX" } }
}
```

### Single-folder listings

`GET /mailboxes/{n}/messages?folder&limit&search&cursor` lists one folder newest first. `search` is a raw IMAP SEARCH string such as `UNSEEN` or `FROM "boss"`, default `ALL`. Prefer `/search` unless you need a criterion it lacks.

Both `/messages` and `/search` return `messages`, `total` (the number of matches) and `next_cursor`. `/search` also returns `next_since_cursor`, and `more_new` when following a cursor.

### Paging and "new mail only"

- **Older pages.** Pass `next_cursor` back as `cursor` for the next, older page. It is `null` on the last page.
- **New since last time.** Pass a previous `next_since_cursor` as `since_cursor`, or give a name as `checkpoint=` and the server stores the position for you, per token, checkpoint name, mailbox and folder. Only mail newer than the position comes back, oldest first in the scan so nothing is skipped; `more_new: true` means call again. The first call with a new checkpoint name returns the newest `limit` messages and stores the position after them.
- **UIDVALIDITY.** If the server renumbered the folder since the cursor was issued, the response has `"reset": true` (and on `/inbox`, `reset_mailboxes`). A `since_cursor` or checkpoint then starts over from the oldest message; a paging `cursor` starts over from the newest.

Cursors are opaque base64 strings. Do not build them yourself.

## Reading a message

`GET /mailboxes/{n}/messages/{uid}?folder&reader&headers&safe_html` returns:

| Field | Content |
| --- | --- |
| `uid`, `from`, `to`, `cc`, `reply_to`, `subject`, `date`, `message_id`, `in_reply_to`, `references` | Decoded headers |
| `body_text`, `body_html` | The first `text/plain` and `text/html` parts, or `null` |
| `attachments` | `part_id`, `filename`, `content_type`, `size`, `sha256`, `inline` for each attachment |
| `authentication` | `trusted`, `authserv_id`, `spf`, `dkim` (a list), `dmarc`, `arc`, `raw` (every Authentication-Results header) and `signals` |
| `html_analysis` | Present when there is an HTML body: `hidden_text`, `suspicious_links`, `link_count` |
| `body_reader` | With `reader=true`: the HTML as readable markdown via [html2text](https://github.com/Alir3z4/html2text), else the plain text |
| `headers` | With `headers=true`: every header in wire order, as `[name, value]` pairs |
| `body_html_safe` | With `safe_html=true`: the HTML through an `nh3` allow-list. Scripts, event handlers, comments and every `<img>` are removed; links keep only `http`, `https` and `mailto` and get `rel="noopener noreferrer nofollow"` |

Reading never marks a message as read; the server fetches with `BODY.PEEK`.

**`signals`** can contain:

| Signal | Meaning |
| --- | --- |
| `reply_to_domain_mismatch` | Reply-To is on a different organizational domain than From |
| `return_path_domain_mismatch` | Return-Path is on a different organizational domain than From |
| `display_name_spoof` | The From display name contains an address other than the real one |
| `no_authentication_results` | No Authentication-Results header at all |
| `authentication_untrusted` | Headers exist, but none from your `trusted_authserv_id` (or it is unset) |
| `spf_fail`, `spf_softfail`, `spf_permerror` | SPF result |
| `dmarc_fail` | DMARC `fail` or `permerror` |
| `dkim_fail` | No DKIM signature passed |
| `dkim_not_aligned` | No passing DKIM signature on the From domain |

How much to trust these depends on [Authentication-Results trust](security.md#authentication-results-trust).

**`hidden_text`** collects text hidden with `display:none`, `visibility:hidden`, zero font size, zero opacity, zero `max-height`, zero height with overflow hidden, the `hidden` attribute, or an element whose text color equals its background color. **`suspicious_links`** lists links with one or more `reasons`: `script_or_data_url`, `ip_literal`, `punycode`, `url_shortener`, `userinfo_in_url`, `text_domain_mismatch` (the link text shows another domain).

### Downloads

- `/raw` returns `message/rfc822` with `Content-Disposition: attachment; filename="message-<uid>.eml"`.
- `/attachments/{part_id}` returns `application/octet-stream` with `Content-Disposition: attachment`, a sanitized file name and `X-Content-Type-Options: nosniff`. Attachments over `limits.attachment_max_bytes` get `413`.

`part_id` comes from the message's `attachments` list.

### Threads and stats

- `/messages/{uid}/thread` searches the message's folder and the Sent folder for messages linked by `Message-ID`, `In-Reply-To` and `References`, and returns `root`, `count` and `messages` oldest first.
- `/threads?folder&limit` (1 to 200, default 20) groups the newest headers of the folder and Sent into conversations: `thread_id`, `subject`, `count`, `unread`, `participants`, `last_date` and `messages`.
- `/stats` returns `folders` (each with `name`, `special`, `messages`, `unseen` and `recent`, or `error` when the server refused STATUS for it) and `quota` when the server supports the QUOTA extension.
- `/identities` returns `address`, `name`, `default` and `has_signature` for each identity, including `*@domain` patterns.

## Sending, replying and drafts

`POST /mailboxes/{n}/send` and `POST /mailboxes/{n}/drafts` take the same body:

```json
{
  "to": ["dest@example.com"], "cc": [], "bcc": ["hidden@example.com"],
  "subject": "hi",
  "body_text": "plain text body", "body_html": "<p>optional html body</p>",
  "identity": "support@cgs.example",
  "reply_to": "noreply@example.com",
  "attachments": [
    { "filename": "a.pdf", "content_type": "application/pdf", "data_base64": "…" },
    { "from_message": { "uid": "1234", "part_id": "3", "folder": "INBOX" } }
  ],
  "signature": true,
  "dry_run": false
}
```

- `to` (at least one) and `subject` are required, plus at least one of `body_text` and `body_html`. Both make a `multipart/alternative` message.
- `identity` defaults to the mailbox's default identity. See [Identities and signatures](configuration.md#identities-and-signatures).
- Each attachment has either `data_base64` or `from_message`, which copies a part of a message in the same mailbox and also needs the `read` scope. A missing `content_type` is guessed from the file name.
- Bcc recipients get the message but never appear in the headers that go on the wire. The Sent copy and drafts keep the Bcc header.
- Addresses and the subject cannot contain line breaks.
- `dry_run` is ignored by `/drafts`.

`POST /mailboxes/{n}/messages/{uid}/reply` takes `{"body_text", "body_html", "reply_all": false, "identity", "attachments": [], "quote": true, "draft": false, "signature": true, "dry_run": false}`. The server fills in the rest from the original message:

- **To**: Reply-To, else From. Replying to a message you sent goes to its original recipients.
- **Cc** with `reply_all`: the original To and Cc, minus your own addresses (the IMAP username, every identity, and addresses covered by `*@domain` patterns).
- **Subject**: `Re: ` added unless the subject already starts with a reply or forward prefix.
- **Threading**: `In-Reply-To` and up to 20 `References`.
- **Quote**: with `quote: true`, the original text (up to 50,000 characters) is quoted below the reply.

After a real send the original gets `\Answered`. With `"draft": true` the reply is saved to Drafts and needs only the `draft` scope.

A send returns:

```json
{ "dry_run": false, "from": "…", "to": ["…"], "cc": [], "subject": "…",
  "message_id": "<…@cgs.example>", "sent_copy": { "folder": "Sent", "uid": "42" },
  "warnings": [] }
```

With a dry run, nothing is delivered and `preview` holds the headers and text body that would have been sent. `warnings` can hold `sent_copy_failed`, `answered_flag_failed` or `draft_not_removed`: the mail went out, but filing it afterwards failed. Fix those by hand; do not send again. `sent_copy` is `null` when `save_sent` is off.

### Drafts

- Creating a draft appends it to the Drafts folder with `\Draft \Seen` and an `X-Mailboxd-Draft-By: <token name>` header, and returns `uid`, `folder` and `message_id`.
- `PUT /drafts/{uid}` takes any subset of the compose fields; the rest is kept from the old draft. It saves a new draft, removes the old one, and returns the new `uid` with `replaced_uid`. A signature added earlier is replaced, not doubled.
- `POST /drafts/{uid}/send` strips `X-Mailboxd-*` headers, refreshes `Date`, checks the sender against the identities again, delivers, files the Sent copy, removes the draft, and flags the message it replies to (looked up by `In-Reply-To` in the default folder) as `\Answered`.

## Organizing

- `DELETE /messages/{uid}` and `POST /messages/delete` move messages to Trash and return `{"ok": true, "action": "trashed", "method": …, "trash_folder": …}`. `method` is `move`, `copy+uid-expunge` or `copy+flagged-deleted`; the last one means the server lacks `UIDPLUS` and the original was left flagged `\Deleted` rather than expunging the whole folder.
- With `permanent=true`, `folder` must be the Trash folder, the token needs `delete`, and the server needs `UIDPLUS`. The result has `"action": "deleted"`.
- `POST /messages/labels` needs at least one of `add` and `remove`.
- `POST /folders` rejects empty names, names over 200 characters and names with line breaks or NUL.

## Audit log

`GET /audit?since&action&token&mailbox&limit` returns `{"entries": […]}`, newest first. `since` is an ISO date or timestamp, or Unix seconds; `limit` is 1 to 1000, default 100. Each entry has `id`, `ts` (Unix seconds), `token`, `action`, `mailbox`, `folder`, `uids`, `outcome` (`ok`, `denied`, `error` or `dry_run`) and `details`. Only entries for the token's own mailboxes are returned.

Actions are `send`, `reply`, `send_draft`, `create_draft`, `update_draft`, `delete_draft`, `delete`, `delete_permanent`, `mark_seen`, `set_labels`, `move`, `create_folder` and `replay_webhook`. A refused MCP tool call is logged as `mcp.<tool name>`.

## Deep health

`GET /health/deep` returns `{"ok": …, "mailboxes": [{"name", "imap", "smtp"}]}`, where `imap` and `smtp` are `{"ok", "latency_ms", "error"}` or `null` when not configured. It checks only the mailboxes the token can see.
