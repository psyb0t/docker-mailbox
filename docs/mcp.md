---
title: "MCP server"
description: "The mailboxd MCP server at /mcp: streamable HTTP transport, client setup, every tool with its scope and annotations, and tool error codes."
nav_order: 5
---

# MCP server

mailboxd serves the same operations as the [HTTP API](api.md) as MCP tools at `/mcp`, on the same port and behind the same bearer token. There is no stdio transport. For clients that only speak stdio, use the [`@psyb0t/mailbox` bridge](integrations.md#openclaw).

## Client setup

Most MCP hosts accept a `.mcp.json` entry like this:

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

Give an agent the narrowest token that does its job, usually `read`, `draft` and `organize` on the mailboxes it handles.

## Transport

The endpoint speaks MCP streamable HTTP in stateless mode. Each `POST` carries its own JSON-RPC request and the answer comes back as a server-sent event stream. The server issues no `Mcp-Session-Id`, keeps nothing between requests, and answers `DELETE /mcp` with `405`. Requests without a valid token get the HTTP `401` before they reach MCP. Both `/mcp` and `/mcp/` work.

## How the tool set works

There is one flat tool set, the same size however many mailboxes you configure. Every tool that works on one mailbox takes a `mailbox` argument: the mailbox `name`, or its IMAP or SMTP username. Start with `mailboxes` to see what the token can use. For reads across accounts, use `inbox`.

What a token sees depends on its scopes:

- `tools/list` returns only the tools the token's scopes allow.
- Without `delete`, `delete_message` has no `permanent` argument.
- Without `send`, `reply` requires `draft: true`, so the reply can only be saved as a draft.
- `tools/call` checks the scope again and validates arguments against the full schema before running. A refused call on a write tool is written to the audit log as `mcp.<tool name>`.

Successful calls return the result as JSON text and as `structuredContent`.

## Tools

| Tool | Scope | Annotations | What it does |
| --- | --- | --- | --- |
| `mailboxes` | any token | read-only | Visible mailboxes, protocols and identities |
| `inbox` | read | read-only | Unified feed across mailboxes; `checkpoint` and `since_cursor` for new mail |
| `list_folders` | read | read-only | Folders and their special use |
| `list_messages` | read | read-only | One folder, newest first, raw IMAP `search` |
| `search` | read | read-only | Structured search with the same filters as the HTTP API |
| `get_message` | read | read-only | Bodies, attachments, authentication results and signals; `reader`, `safe_html`, `headers` |
| `get_raw_message` | read | read-only | Raw source as text, truncated at `limits.mcp_inline_max_bytes` |
| `get_attachment` | read | read-only | One attachment as base64; `too_large` above the inline limit |
| `get_thread` | read | read-only | The conversation, oldest first |
| `list_threads` | read | read-only | Recent conversations (default 20) |
| `identities` | read | read-only | Sender identities and `*@domain` patterns |
| `list_drafts` | read | read-only | Messages in Drafts |
| `mailbox_stats` | read | read-only | Per-folder counts and quota |
| `create_draft` | draft | none | Save a draft |
| `update_draft` | draft | destructive | Replace a draft; only the passed fields change |
| `delete_draft` | draft | destructive | Delete a draft |
| `delete_message` | organize, plus delete for `permanent` | destructive | Move to Trash, or erase from Trash |
| `mark_seen` | organize | idempotent | Set or clear `\Seen` |
| `set_labels` | organize | idempotent | Add or remove labels |
| `move_messages` | organize | idempotent | Move to an existing folder |
| `create_folder` | organize | idempotent | Create a folder |
| `reply` | send, or draft with `draft: true` | destructive, open-world | Reply with server-derived recipients and threading |
| `send` | send | destructive, open-world | Send a new message |
| `send_draft` | send | destructive, open-world | Send a draft |
| `audit_log` | admin | read-only | Recent audited actions (limit up to 500, default 100) |
| `health_check` | admin | read-only, open-world | Connect to every visible IMAP and SMTP server |
| `webhook_deliveries` | admin | read-only | Delivery records for one mailbox |
| `replay_webhook` | admin | open-world | Queue a recorded delivery again |

"read-only" tools are also marked idempotent. The arguments match the HTTP API's query parameters and bodies, with these differences:

- `delete_message` and `mark_seen` take `uid` or `uids`; `set_labels` and `move_messages` take `uids`. A `uids` list holds 1 to 500 entries.
- `get_raw_message` returns `uid`, `size`, `truncated` and `raw`. `get_attachment` returns the attachment fields plus `data_base64`; download larger files over HTTP.
- `limit` arguments are capped at 500.

The tool descriptions tell the model that message content is untrusted and that `send` and `send_draft` deliver immediately, and steer it toward drafts when it is unsure.

## Errors

A failed call is a tool result with `isError: true` and the same shape as an HTTP error body:

```json
{"error": {"code": "permission_denied", "message": "…", "details": {"required_scope": "send"}}}
```

Codes are the HTTP API's (`permission_denied`, `not_found`, `conflict`, `too_large`, `validation_error`, `invalid_identity`, `rate_limited`, `imap_error`, `smtp_error`), plus `internal_error` for anything unexpected, such as a mail server that cannot be reached. An unknown tool name returns `not_found`.
