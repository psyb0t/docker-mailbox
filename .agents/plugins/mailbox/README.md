# @psyb0t/mailbox

An OpenClaw/MCP plugin that connects your agent to a self-hosted
[docker-mailbox](https://github.com/psyb0t/docker-mailbox) IMAP/SMTP control
plane over the [Model Context Protocol](https://modelcontextprotocol.io).

docker-mailbox already serves a Streamable-HTTP MCP endpoint at `/mcp`. This
package is a thin stdio↔HTTP bridge (via
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote)) for MCP clients that
speak local stdio servers — it forwards everything to your running mailboxd
instance and authenticates with your bearer token when the server requires one.

> docker-mailbox is **self-hosted**. This plugin does not ship a mail server —
> it connects to a mailboxd instance that **you** run. See the
> [docker-mailbox repo](https://github.com/psyb0t/docker-mailbox) to stand one up.

## Tools

The docker-mailbox MCP tools become available to your agent: `mailboxes`
(discovery), `inbox` (unified newest-first read across every configured IMAP
mailbox), `list_folders`, `list_messages`, `search` (structured single-mailbox
search), `get_message` (with optional `reader` mode — HTML stripped to clean
markdown), `delete_message`, `mark_seen`, and `send`. Every per-mailbox tool
takes a `mailbox` argument (the configured name or its email address), so the
tool catalog stays constant-sized no matter how many mailboxes are configured.

## Configuration

| Env var | Required | Description |
|---|---|---|
| `MAILBOX_URL` | yes | Base URL of your running mailboxd server, e.g. `http://localhost:8000`. The bridge appends `/mcp`. |
| `MAILBOX_TOKEN` | no | Bearer token — only if the mailboxd server was started with `auth.tokens` configured. |

## Install

Install it into your OpenClaw agent from ClawHub:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Then set `MAILBOX_URL` (and `MAILBOX_TOKEN` if your server uses auth) in
the plugin's environment.

## Native remote MCP (no install)

If your MCP client already supports **remote** Streamable-HTTP servers, you
don't need this bridge — point the client straight at
`$MAILBOX_URL/mcp` with an `Authorization: Bearer <token>` header.

## License

MIT. See [LICENSE](LICENSE).
