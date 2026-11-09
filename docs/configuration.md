---
title: "Configuration"
description: "Every mailboxd config.yaml field with its default: mailboxes, IMAP and SMTP, bearer tokens and scopes, sender identities, limits, labels and the watcher."
nav_order: 3
---

# Configuration

mailboxd reads one YAML file, from `--config`, else `MAILBOXD_CONFIG`, else `/etc/mailboxd/config.yaml`. [`config.example.yaml`](https://github.com/psyb0t/docker-mailbox/blob/main/config.example.yaml) shows every option with comments. Changes take effect when the process restarts.

Any string may contain `${NAME}`, which is replaced by that environment variable. A variable that is not set stops startup. Use this to keep passwords, tokens and webhook secrets out of the file.

**The config file holds credentials.** Even with `${ENV}` references it names your accounts and servers. Keep it out of Git, `chmod 600` it and mount it read-only.

## A minimal file

```yaml
log_level: INFO

auth:
  # See "Authentication-Results trust" on the security page.
  trusted_authserv_id: mx.cgs.example
  tokens:
    - name: mila
      token: ${MAILBOXD_TOKEN_MILA}
      scopes: [read, draft, organize]
      mailboxes: [contact]
      dry_run: false

mailboxes:
  - name: contact
    description: "Contact inbox"
    imap:
      host: imap.example.com
      username: contact@cgs.example
      password: ${PW}
    smtp:
      host: smtp.example.com
      username: contact@cgs.example
      password: ${PW}
      from_address: "L'équipe CGS <contact@cgs.example>"
      identities:
        - address: contact@cgs.example
          name: "L'équipe CGS"
          default: true
        - address: support@cgs.example
          name: "Support CGS"
      save_sent: true
      per_hour: 20
    folders:          # null = auto-detect
      sent: null
      drafts: null
      trash: null
      junk: null
```

## Reference

### Top level

| Field | Default | Meaning |
| --- | --- | --- |
| `log_level` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `auth` | no tokens | Bearer tokens and Authentication-Results trust. With no tokens every protected request gets `401` |
| `mailboxes` | `[]` | The accounts. Names must be unique |
| `state.path` | `MAILBOXD_STATE_PATH`, else `/var/lib/mailboxd/state.db` | SQLite state database |
| `send_guard` | `dry_run: false`, `per_hour: 60` | Global send rules |
| `limits` | see [Limits](#limits) | Size caps |
| `labels` | `suspect`, `a_traiter`, `fait` | Friendly label names mapped to IMAP keywords |
| `watch` | no webhooks | The webhook watcher, see [Webhooks](webhooks.md) |

### Mailboxes

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Matches `[a-zA-Z0-9_-]+`, unique. Used in URL paths and as the `mailbox` argument of MCP tools |
| `description` | `""` | Shown by `GET /mailboxes` and the `mailboxes` tool |
| `imap` | none | Reading and organizing. At least one of `imap` and `smtp` is required |
| `smtp` | none | Sending |
| `folders.sent`, `.drafts`, `.trash`, `.junk` | `null` (auto-detect) | Pin a special folder by name |

A client can name a mailbox by its `name`, or by its IMAP or SMTP `username` when that is an address.

| `imap` field | Default | Meaning |
| --- | --- | --- |
| `host` | required | Server hostname |
| `port` | `993` | 1 to 65535 |
| `tls` | `ssl` | `ssl`, `starttls` or `none` |
| `username`, `password` | required | Login |
| `default_folder` | `INBOX` | Folder used when a request names none |

| `smtp` field | Default | Meaning |
| --- | --- | --- |
| `host` | required | Server hostname |
| `port` | `587` | 1 to 65535 |
| `tls` | `starttls` | `ssl`, `starttls` or `none` |
| `username`, `password` | required | Login. An empty `username` skips SMTP authentication |
| `from_address` | required | Legacy single sender. Becomes the only identity when `identities` is empty |
| `identities` | `[]` | Sender addresses this mailbox may use, see [Identities and signatures](#identities-and-signatures) |
| `save_sent` | `true` | Append a copy of each sent message to the Sent folder. Set `false` for providers that file sent mail themselves, such as Gmail |
| `per_hour` | `send_guard.per_hour` | Per-mailbox send limit, 0 for unlimited |

`ssl` and `starttls` verify the server certificate chain and hostname, and there is no setting to turn that off. Trust a private CA with `SSL_CERT_FILE` (see [Installation](installation.md#environment-variables)). IMAP and SMTP sockets time out after 30 seconds.

Special folders are auto-detected from the IMAP `SPECIAL-USE` flags, then from common names:

| Kind | Names tried |
| --- | --- |
| sent | `Sent`, `Sent Items`, `Sent Messages`, `INBOX.Sent`, `Envoyés`, `Sent Mail` |
| drafts | `Drafts`, `INBOX.Drafts`, `Brouillons`, `Draft` |
| trash | `Trash`, `Deleted Items`, `Deleted Messages`, `INBOX.Trash`, `Corbeille`, `Bin` |
| junk | `Junk`, `Spam`, `INBOX.Junk`, `INBOX.Spam`, `Junk E-mail`, `Indésirables` |

## Tokens and scopes

Every request except `GET /health` needs `Authorization: Bearer <token>`, including `/mcp`. Tokens are compared in constant time.

| Token field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Unique. Shown in the audit log and in the `X-Mailboxd-Draft-By` header of drafts |
| `token` | required | The bearer value. Unique and not empty. `openssl rand -hex 32` makes a good one |
| `scopes` | **all six scopes** | What the token may do |
| `mailboxes` | `"*"` | A list of mailbox names, or `"*"` for all |
| `dry_run` | `false` | `true` turns every send by this token into a preview |

**Always list `scopes` and `mailboxes` explicitly.** A token entry that leaves them out gets every scope on every mailbox.

A bare string instead of a mapping is the legacy form. It is named `legacy-N` (N is its position in the list), gets every scope on every mailbox, and logs a warning at startup.

| Scope | Allows |
| --- | --- |
| `read` | List, search and read messages, raw source, attachments, threads, folders, identities, stats, list drafts |
| `draft` | Create, update and delete drafts; save a reply as a draft |
| `organize` | Mark read/unread, labels, move, create folders, delete to Trash |
| `send` | Send, reply directly, send a draft |
| `delete` | Permanent deletion, only from the Trash folder |
| `admin` | Audit log, deep health check, webhook delivery listing and replay, all limited to the token's mailboxes |

A mailbox outside a token's `mailboxes` list behaves exactly like one that does not exist (`404`), so a restricted token cannot learn its name. `GET /mailboxes` needs only a valid token, no scope.

## Identities and signatures

`smtp.identities` lists the addresses a mailbox may send as:

| Identity field | Default | Meaning |
| --- | --- | --- |
| `address` | required | A bare address, or a `*@domain` pattern |
| `name` | `""` | Display name |
| `default` | `false` | The sender used when a request names none |
| `signature_text` | none | Appended to text bodies after a `-- ` line |
| `signature_html` | none | Appended to HTML bodies. Without it, `signature_text` is escaped and used |

Without `identities`, one identity is derived from `from_address`. Send, reply and draft requests take an `identity` (an address). The older `from_address` request field still works but must match an identity too. Anything else is refused with `422 invalid_identity` and the list of allowed addresses. Signatures are added unless the request passes `"signature": false`.

For a catch-all domain, add an opt-in `*@domain` pattern next to a concrete default:

```yaml
identities:
  - address: contact@example.com
    name: Contact
    default: true
  - address: "*@example.com"
    name: Contact
    signature_text: "Contact team"
```

- An explicit `identity: shop@example.com` sends as that address with the pattern's name and signatures.
- Exact identities win over patterns, whatever their order in the file.
- A pattern matches only its exact domain, case-insensitively, never subdomains. Partial local parts (`info*@x`) and domain wildcards are rejected at startup.
- A pattern cannot be the default and cannot be sent literally. At least one concrete identity is required. Without `default: true`, the first concrete identity is the default.

Replies without an explicit identity use the first exact identity found in the original's To or Cc. Otherwise they use the first To, then Cc, address matching a pattern, with that pattern's name and signatures. Otherwise (Bcc-only delivery, for example) they use the default. Reply-all treats every address covered by a pattern as your own and leaves it out.

Only add a pattern for a domain this mailbox may really send as; the SMTP provider must accept those senders too.

## Send guard

```yaml
send_guard:
  dry_run: false
  per_hour: 60
```

- `per_hour` caps real sends per rolling hour, separately per mailbox and per token, 0 for unlimited. `smtp.per_hour` replaces it for one mailbox's limit; the per-token limit always uses `send_guard.per_hour`. Over the limit the API answers `429` with `Retry-After`.
- `dry_run: true` turns every send on the server into a preview.

Details on how attempts are counted are on the [security page](security.md#rate-limit).

## Limits

| Field | Default | Bounds | Meaning |
| --- | --- | --- | --- |
| `incoming_message_max_bytes` | 32 MiB | ≥ 1024 | Largest message read from IMAP |
| `request_max_bytes` | 32 MiB | 128 B to 128 MiB | Largest HTTP or MCP request body |
| `attachment_max_bytes` | 15 MiB | ≥ 1 | Largest attachment served by the download endpoint and `get_attachment` |
| `outgoing_attachments_max_bytes` | 20 MiB | ≥ 1 | Total attachments on one outgoing message |
| `mcp_inline_max_bytes` | 512 KiB | ≥ 1024 | Raw source and base64 attachments returned inline over MCP |

`incoming_message_max_bytes` applies to every operation that needs the whole message: reading it, downloading its source or an attachment, replying to it, attaching its parts with `from_message`, and updating or sending it as a draft. Over the limit those return `413 too_large`. The service checks `RFC822.SIZE` before downloading and requests at most the limit; a server that sends more anyway has its connection closed before the data is stored. Listings, threads and webhooks read capped headers only, so oversized messages still show up there.

## Labels

```yaml
labels:
  suspect: $Suspect
  a_traiter: $ATraiter
  fait: $Fait
```

Labels are IMAP keywords with friendly names. You pass the friendly names to `set_labels` and to the `label` search filter, plus the built-in `flagged` (`\Flagged`). Unknown names are refused with the list of known ones. Keywords cannot contain spaces, quotes, parentheses, `\`, `%`, `*`, `{` or `]`, and cannot start with `\`.

## Watcher

| Field | Default | Meaning |
| --- | --- | --- |
| `watch.folder` | `INBOX` | The folder watched in every mailbox |
| `watch.poll_seconds` | `60` | Poll interval for servers without IMAP IDLE, minimum 5 |
| `watch.webhooks[].url` | required | `http` or `https`, a hostname, no user info, no fragment |
| `watch.webhooks[].secret` | required | HMAC key, not empty |
| `watch.webhooks[].mailboxes` | `"*"` | Mailbox names this target receives events for |

See [Webhooks](webhooks.md) for the payload, signing and retries.

## Startup checks

The config is validated before the server starts. Unique mailbox names, unique token names and values, token and webhook `mailboxes` lists that name existing mailboxes, valid label keywords and valid identities are all required. The exact messages are listed in [Troubleshooting](troubleshooting.md#startup-errors).
