# mailbox

[![version](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/version.svg)](https://github.com/psyb0t/docker-mailbox/releases)
[![license](https://raw.githubusercontent.com/psyb0t/docker-mailbox/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/mailbox?style=flat-square)](https://hub.docker.com/r/psyb0t/mailbox)

Your inboxes, on tap. Point this thing at as many email accounts as you want over IMAP + SMTP, and out the other end you get **one HTTP API and one MCP server, both on the same port** (MCP rides a streamable-HTTP channel at `/mcp`) so you can read mail, send mail, and nuke mail across every account from one place. No webmail. No database. No three-thousand-toggle desktop app. Just: "here's some email creds" → "now my agent / shell script / chaotic 3am curl pipeline can drive the inbox."

Every other "unified inbox" thing on the planet wants to *own* your mail — slurp it all into their cloud, charge you forever, lose it in a breach next quarter. This one stores **zero bytes**. Restart the container, nothing's lost, because there was never anything to lose. Connections come up per request, do their job, and die in a `finally`.

Stdlib `imaplib` + `smtplib` under the hood, FastAPI on top, official MCP Python SDK riding shotgun (streamable HTTP, no stdio nonsense), a supply-chain `exclude-newer` gate so a malicious pip release published at 3am can't sneak in, and a real-SMTP-server integration test that actually puts bytes on a socket.

## Table of Contents

- [What's Inside](#whats-inside)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [HTTP API](#http-api)
  - [Authentication](#authentication)
- [MCP server](#mcp-server)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

## What's Inside

| Surface         | The goods                                                                                                                                       |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **HTTP API**    | `GET /inbox` fans out across every account at once (filter by mailbox, sender, subject, date, flags…). Per-mailbox reads + deletes. SMTP send.   |
| **MCP server**  | Streamable-HTTP MCP at `/mcp` — same port, same bearer, same boss. A flat set of tools (`mailboxes`, `inbox`, `list_messages`, `send`, …) that take `mailbox` as a parameter. 100 accounts? Still one tool catalog. |
| **Bearer auth** | One token list in YAML guards both the API and `/mcp`. Empty list = wide open (your problem). Multiple tokens = zero-downtime rotation.          |
| **Protocols**   | IMAP (SSL / STARTTLS / plain), SMTP (SSL / STARTTLS / plain). Standards-boring on purpose.                                                       |
| **Config**      | One YAML file. Add a mailbox, restart, done. Each one declares whichever subset of `{imap, smtp}` you actually care about.                       |
| **State**       | None. Truly none. No DB, no queue, no cache, no "oh just this little Redis." A connection opens, does the work, closes. Next.                    |

## Quick Start

1. Drop a `config.yaml` next to you (steal `config.example.yaml` if you're feeling lazy — that's what it's there for).
2. Light it up:

```bash
docker run --rm \
  -p 8000:8000 \
  -v "$PWD/config.yaml:/etc/mailboxd/config.yaml:ro" \
  psyb0t/mailbox:latest
```

3. Poke it:

```bash
# no auth? this works as-is. with auth.tokens set, add: -H "Authorization: Bearer YOUR_TOKEN"
TOKEN="paste-a-token-from-config-here"

curl -s http://localhost:8000/health | jq                                               # health is always open
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/mailboxes | jq
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:8000/inbox?limit=5' | jq
```

### docker compose

```yaml
services:
  mailbox:
    image: psyb0t/mailbox:latest
    ports: ["8000:8000"]
    volumes:
      # config.yaml holds your IMAP/SMTP passwords AND your auth.tokens —
      # gitignore it, lock down its filesystem perms, treat it like an SSH key.
      - ./config.yaml:/etc/mailboxd/config.yaml:ro
```

## Configuration

One YAML file. Lives at `MAILBOXD_CONFIG`, or `--config`, or `/etc/mailboxd/config.yaml` if you can't be bothered.

```yaml
log_level: INFO

# Bearer-token gate. Guards the HTTP API AND /mcp. Empty / missing = no auth
# (good luck out there). Multi-token list = rotate without downtime: add a
# new one, swap clients over, retire the old one.
auth:
  tokens:
    - "long-random-token-1"
    - "long-random-token-2"

mailboxes:
  - name: personal              # URL-safe handle; shows up in /mailboxes/<name>/... and as the MCP tool prefix
    description: "Gmail"

    imap:
      host: imap.gmail.com
      port: 993                 # default 993
      tls: ssl                  # ssl | starttls | none   (default ssl)
      username: me@gmail.com
      password: "app-password"  # Gmail/Yahoo/etc. need an app password, not your real one
      default_folder: INBOX     # default folder when callers don't specify one

    smtp:
      host: smtp.gmail.com
      port: 465                 # default 587
      tls: ssl                  # default starttls
      username: me@gmail.com
      password: "app-password"
      from_address: "Me <me@gmail.com>"

  - name: work
    imap:  { host: mail.work.com, port: 143, tls: starttls, username: me, password: "...", default_folder: INBOX }
    smtp:  { host: mail.work.com, port: 587, tls: starttls, username: me, password: "...", from_address: me@work.com }
```

The fine print:

- **At least one mailbox.** Each one needs at least one of `imap` / `smtp`. Both is fine. Neither is a config error.
- **`name`** matches `[a-zA-Z0-9_-]+` and is unique — it's the URL path segment and the MCP tool prefix, so don't put spaces or emojis in it.
- **Defaults**: IMAP `993/ssl`, SMTP `587/starttls`. Override if your provider is weird.
- **The config file holds plaintext passwords and your bearer tokens.** Treat it like a credential vault: gitignore it, `chmod 600`, mount read-only, don't paste it in Slack.

## HTTP API

### Authentication

If `auth.tokens` is set, **every request except `GET /health`** has to carry a bearer:

```
Authorization: Bearer <one of auth.tokens>
```

No header, wrong shape, wrong value → `401` with `WWW-Authenticate: Bearer`. Tokens get a constant-time compare so you don't leak them through timing. The same gate covers `/mcp` — there's no second auth system to learn.

Leave `auth.tokens` empty (or skip the block) and everything's open. Fine for "this is bound to 127.0.0.1 and there's a reverse proxy in front." Catastrophic otherwise. Your call.

### Errors

Everything's JSON. Errors look like `{"detail": "..."}`:

| Status | When                                                                                       |
| ------ | ------------------------------------------------------------------------------------------ |
| `401`  | Missing or invalid bearer (when auth is on).                                                |
| `404`  | Unknown mailbox name in the URL.                                                            |
| `409`  | You're asking a mailbox for a protocol it doesn't have (IMAP endpoint on an SMTP-only one). |
| `502`  | The IMAP / SMTP server upstream said no.                                                    |

### `GET /health`

```json
{ "ok": true, "version": "0.1.0" }
```

Always open, no bearer required. Point your liveness probe at this and forget about it.

### `GET /mailboxes`

```json
{
  "mailboxes": [
    { "name": "personal", "description": "Gmail",
      "imap": true, "smtp": true }
  ]
}
```

### Unified inbox (the main event)

`GET /inbox` is the read endpoint you actually want 90% of the time. It hits **every IMAP-configured mailbox in parallel**, runs the same structured search on each one, merges newest-first, and tags every result with which account it came from. "Show me everything from `boss@corp.com`," "what's unread right now," "what came in this morning" — all the same call, no fanout dance on the client side.

| Query param                                      | What it does                                                                                                                              |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `mailbox`                                        | CSV filter by mailbox name (`personal`) **or** email address (`me@gmail.com`). Omit to search all of them.                                 |
| `from`, `to`, `subject`, `body`, `text`          | IMAP SEARCH predicates. `text` is full-text across headers + body.                                                                         |
| `since`, `before`                                | IMAP dates, e.g. `1-Jan-2026`.                                                                                                             |
| `unseen`, `seen`, `flagged`, `answered`          | Flag filters. Set the ones you want to true.                                                                                               |
| `larger_than`, `smaller_than`                    | Bytes.                                                                                                                                     |
| `folder`                                         | IMAP folder (default `INBOX`).                                                                                                             |
| `limit`                                          | Hard-capped at 500. Default 50.                                                                                                            |

Response:
```json
{
  "messages": [
    {
      "uid": "1234",
      "mailbox": "personal",
      "mailbox_address": "me@gmail.com",
      "from": "boss@corp.com",
      "subject": "weekly sync",
      "date": "..."
    }
  ],
  "errors": [
    { "mailbox": "work", "error": "login failed: ..." }
  ]
}
```

Per-mailbox blowups land in `errors` instead of killing the call. One dead account doesn't blind you to the other nine.

```bash
# everything from one sender, all accounts at once
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/inbox?from=boss@corp.com&limit=20' | jq

# unread mail in just two of them
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8000/inbox?mailbox=personal,work&unseen=true' | jq
```

### Per-mailbox IMAP

When you want to zero in on one account:

| Method   | Path                                                  | Does                                                                                                                        |
| -------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `GET`    | `/mailboxes/{name}/folders`                           | List IMAP folders.                                                                                                          |
| `GET`    | `/mailboxes/{name}/messages?folder=&limit=&search=`   | Newest-first headers. `search` is raw IMAP (`ALL`, `UNSEEN`, `FROM foo@bar`). `limit` ≤ 500.                                |
| `GET`    | `/mailboxes/{name}/search?from=&subject=&...`         | Same structured query as `/inbox` minus `mailbox`, scoped to one account.                                                   |
| `GET`    | `/mailboxes/{name}/messages/{uid}?folder=&reader=`    | One message, fully decoded (`body_text` + `body_html` + attachment metadata). Add `reader=true` to also get `body_reader` — HTML stripped to clean text/markdown (no tables, styles, tracking pixels). |
| `DELETE` | `/mailboxes/{name}/messages/{uid}?folder=`            | `\Deleted` + EXPUNGE. Gone. Really gone.                                                                                    |
| `POST`   | `/mailboxes/{name}/messages/{uid}/seen?folder=`       | Body `{"seen": true|false}` — flip the `\Seen` flag.                                                                        |

UIDs everywhere, never sequence numbers — identifiers stay stable when the mailbox shifts around under you.

### Reader mode

`?reader=true` on `GET /mailboxes/{name}/messages/{uid}` (or `reader=true` on the MCP `get_message` tool) adds a `body_reader` field — the HTML body flattened into clean markdown. Built for LLMs and humans who don't want to read raw `<table><tr><td style="…">…</td></tr></table>` chrome.

How it works:

1. The HTML body is run through [html2text](https://github.com/Alir3z4/html2text) with `ignore_images=True`, `body_width=0` (no hard wrap), `unicode_snob=True`. Styles, scripts, comments, head, and `<img>` tags are dropped.
2. Headings → `#`, bold/italic preserved, `<a href="x">text</a>` → `[text](x)` inline, lists/tables converted to markdown equivalents.
3. If there is no HTML body, `body_reader` falls back to the trimmed `body_text`.
4. The original `body_text` and `body_html` are still returned alongside — `body_reader` is additive. UI clients can render HTML; agents can read markdown; nobody loses anything.

Why not just use `body_text`? Most marketing/transactional mail ships `multipart/alternative` where the `text/plain` part is missing, a "view in HTML" stub, or auto-generated noise. The real content lives in the HTML part. Reader mode extracts it. Why not readability/trafilatura? Those are tuned to find the article inside a webpage full of navigation and ads. Emails ARE the content — the noise is styling, which is exactly what html2text strips. No DOM-extraction needed.

Reply-quote chains aren't stripped (you get the full thread), table-layout emails come through as pipe-tables (faithful but noisy), and attachments stay as metadata only.

### SMTP

| Method | Path                          | Does           |
| ------ | ----------------------------- | -------------- |
| `POST` | `/mailboxes/{name}/send`      | Send an email. |

Body:
```json
{
  "to":           ["dest@example.com"],
  "cc":           ["copy@example.com"],
  "bcc":          ["hidden@example.com"],
  "subject":      "hi",
  "body_text":    "plain text body",
  "body_html":    "<p>optional html body</p>",
  "from_address": "override@example.com",
  "reply_to":     "noreply@example.com"
}
```

At least one of `body_text` / `body_html` is required. Both = `multipart/alternative` like a respectable mail client. We also add a Thunderbird-shaped `User-Agent`, a domain-aligned `Message-ID`, and a `Date` header — provider spam filters get hostile when those are missing or sloppy, so we play the game.

## MCP server

Same operations as the HTTP API, exposed as MCP tools over **streamable HTTP** at `/mcp` (same port, same bearer). One flat tool set — every per-mailbox op takes `mailbox` as a parameter (name OR address), so 100 inboxes still ship the same handful of tools:

```
mailboxes                   # discovery: list configured mailboxes + capabilities
inbox                       # unified read across all IMAP mailboxes (mailbox= filter)
list_folders                # (mailbox)
list_messages               # (mailbox, folder, limit, search)
search                      # (mailbox, from, subject, since, ...)
get_message                 # (mailbox, uid, reader=true → +body_reader)
delete_message              # (mailbox, uid)
mark_seen                   # (mailbox, uid, seen)
send                        # (mailbox, to, subject, body_text/html, ...)
```

An agent finds what's available via `mailboxes`, then passes the chosen name (`"personal"`) or address (`"me@gmail.com"`) as the `mailbox` arg. For cross-account queries use `inbox` — `inbox(from="boss@corp.com")` fans out across every IMAP-enabled mailbox at once.

IMAP-only tools only appear if at least one mailbox has IMAP. Same for SMTP. No dead buttons.

### Transport

Plain old streamable-HTTP MCP at `/mcp`. The endpoint speaks the full transport: `GET` opens the SSE stream back to the client, `POST` ships requests, `DELETE` terminates the session. Whatever your MCP host knows how to do, do that. There is **no stdio transport** — `make run` is all you need; the same process serves the REST API and `/mcp`. Point your client at `http://host:8000/mcp` and you're done.

### Wiring it into Claude / Pi / any MCP host

Most MCP hosts take a `.mcp.json` (or equivalent) like this:

```json
{
  "mcpServers": {
    "mailbox": {
      "transport": "streamable-http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

Drop the `headers` block if you're running without `auth.tokens`. Keep it if you value sleep.

## Architecture

```
┌────────────┐    ┌──────────────────────────┐    ┌──────────────┐
│  HTTP CLI  │───▶│  FastAPI                 │    │  IMAP server │
│  / curl    │    │  /inbox, /mailboxes, …   │───▶│  SMTP server │
└────────────┘    │                          │    └──────────────┘
┌────────────┐    │  bearer-auth gate        │
│  MCP host  │───▶│  ─── shared ops ───      │
│ (Claude…)  │    │                          │
└────────────┘    │  /mcp  (streamable HTTP) │
                  │  mailboxes/inbox/send/…  │
                  └──────────────────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │  config.yaml         │
                    │  auth.tokens         │
                    │  one entry per       │
                    │  mailbox             │
                    └──────────────────────┘
```

Stateless. No DB. No queue. No cache. Connections open per request and die in a `finally`. Kill the container mid-flight — there's nothing to recover because nothing was ever persisted. Boot it back up. Same story. Boring on purpose.

## Development

```bash
make help          # list all targets
make dev-image     # build the sandboxed dev container
make shell         # drop into it
make run           # boot the server (REST + /mcp); mount CONFIG=path/to/config.yaml
make test          # full suite (unit + docker-in-docker integration)
make test-unit     # in-process only — fast feedback loop
make lint          # flake8 + mypy
make format        # isort + black
make check         # lint + tests
```

### Package management — supply-chain defense

We use `uv`'s `exclude-newer` to refuse any package version published after a fixed cutoff. The cutoff gets **bumped to today** automatically by every `pkg-*` make target, so a freshly-published malicious release can't sneak in on the next `pkg-add`.

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
