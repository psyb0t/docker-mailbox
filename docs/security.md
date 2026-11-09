---
title: "Safety and security"
description: "What mailboxd does to keep agents and clients in bounds: scopes, Trash-first deletion, two-step drafts, dry runs, send limits, sender checks, audit and TLS."
nav_order: 6
---

# Safety and security

mailboxd is built to sit between your mail accounts and clients you do not fully trust, AI agents included. This page lists each safeguard and its limits.

## Access control

- **Scoped tokens.** Each token has a list of scopes and a list of mailboxes. The HTTP API, the MCP tools and the service layer all check them. See [Tokens and scopes](configuration.md#tokens-and-scopes).
- **Hidden mailboxes.** A mailbox outside a token's list answers exactly like a mailbox that does not exist, so a token cannot discover the names of the others.
- **Admin is per mailbox.** The audit log, deep health check and webhook endpoints show only the admin token's own mailboxes.
- **Defaults are wide.** A token entry without `scopes` gets all of them, and one without `mailboxes` gets all mailboxes. So does a bare-string legacy token. Write both lists out for every token.

## Deletion

- A normal delete moves messages to Trash, with `UID MOVE`, or with `UID COPY` then `\Deleted` and `UID EXPUNGE` of exactly those UIDs.
- On a server without `UIDPLUS`, the copy fallback leaves the original flagged `\Deleted` instead of running a plain `EXPUNGE`, which would also erase messages someone else flagged.
- Permanent deletion needs `permanent=true`, the `delete` scope, Trash as the source folder and `UIDPLUS`. Anything else gets `409`.
- The service never sends a folder-wide `EXPUNGE`. The test suite records every IMAP command to prove it.

## Two-step drafts

An agent with `draft` saves a message to Drafts (`\Draft \Seen`, with `X-Mailboxd-Draft-By: <token name>`). A person reviews it in their mail client, or a token with `send` calls send-draft. Sending a draft strips every `X-Mailboxd-*` header, refreshes `Date` and checks the sender against the configured identities again, so editing the From header of a draft does not get around the identity check.

Each draft can be sent once. The server records a claim on the draft (mailbox, Drafts folder, UIDVALIDITY, UID) before talking to SMTP, and a second attempt gets `409`, even if the first attempt's SMTP outcome was unknown.

## Dry run

A send is only previewed when any of these is true: `send_guard.dry_run`, the token's `dry_run`, or the request's `dry_run`. A request can switch dry-run on, never off. Dry runs build and validate the whole message, are audited with outcome `dry_run`, do not count against the rate limit, and do not claim a draft.

## Rate limit

`send_guard.per_hour` (default 60, `smtp.per_hour` overrides it per mailbox) caps sends per rolling hour, counted separately per mailbox and per token. The check and the count happen in one SQLite transaction, so several workers cannot race past it. Attempts are counted before SMTP, so failed sends count too; retrying a failing send in a loop cannot get around the limit. Over the limit the answer is `429` with `Retry-After`.

## Sender and header checks

- The From address must be a concrete address matching an exact identity or an opt-in `*@domain` pattern. Otherwise the answer is `422 invalid_identity`. A pattern cannot be sent literally.
- Replies get their recipients, subject and threading headers from the original message; the caller cannot set them.
- Recipients must be addresses, and line breaks are refused in addresses, the subject and IMAP arguments.

## Message content

- Reading never sets `\Seen`; only `mark_seen` changes it.
- `get_message` returns sender authentication verdicts, header mismatch signals, hidden HTML text and suspicious links, so a client can spot phishing. See [Reading a message](api.md#reading-a-message).
- `safe_html=true` returns an allow-listed HTML body with scripts, event handlers and images removed.
- The MCP tool descriptions tell the model that message content is untrusted and must not be followed as instructions. That is advice to the model, not enforcement; scopes are the enforcement.

## Authentication-Results trust

Anyone can put an `Authentication-Results` header in a message they send. Set `auth.trusted_authserv_id` to the host name **your own** receiving server writes in that header (look at a message you received). Then only headers carrying that id are used for the `spf`, `dkim` and `dmarc` verdicts, and `authentication.trusted` is `true`.

Without it, the topmost header is shown for information, `trusted` is `false`, and the `authentication_untrusted` signal is raised. Make sure your mail server strips incoming `Authentication-Results` headers that claim your own id; most do.

## Audit

Writes on a visible mailbox are recorded in the state database: who, what, which mailbox, folder and UIDs, and the outcome (`ok`, `denied`, `error`, `dry_run`). That includes writes refused for a missing scope and those that failed at the mail server. Message bodies are never recorded. Requests rejected earlier, such as an invalid token, a hidden mailbox or a malformed UID, are not recorded.

The audit log is operational history in a SQLite file, not tamper-proof compliance storage. Nothing prunes it; plan a retention policy. Read it with `GET /audit` or the `audit_log` tool.

## Network and TLS

- IMAP and SMTP over `ssl` or `starttls` verify the certificate chain and host name with Python's default context. There is no switch to turn verification off; trust a private CA with `SSL_CERT_FILE`. `tls: none` sends credentials in clear text and is only for trusted local servers.
- The container listens on all interfaces inside the container. Publish it on `127.0.0.1` unless you mean to expose it, and put HTTPS in front of anything that leaves the host. mailboxd itself does not terminate TLS.
- `GET /health` is the only route without a token. It returns the version and nothing else.
- Webhook targets come from the config, not from API callers, so private network targets are allowed. Redirects are refused. See [Webhooks](webhooks.md).

## Stored data

| Where | What |
| --- | --- |
| `config.yaml` | Account passwords, bearer tokens and webhook secrets, unless referenced as `${ENV}` |
| State database (`state.db`, mode 600, directory mode 700) | Audit metadata, send counters, checkpoints, draft send claims, and queued webhook payloads (headers and verdicts, no bodies) |
| IMAP server | All mail, including drafts and Sent copies |

Bodies, attachments and credentials are never written to the state database.

## Container

The image runs as UID 10001. The bundled Compose file adds a read-only root filesystem, drops all capabilities, sets `no-new-privileges` and limits memory, CPU and processes; see [Installation](installation.md#docker-compose).
