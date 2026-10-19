# @psyb0t/mailbox

An OpenClaw/MCP plugin that connects your agent to a self-hosted
[docker-mailbox](https://github.com/psyb0t/docker-mailbox) IMAP/SMTP control
plane over the [Model Context Protocol](https://modelcontextprotocol.io).

docker-mailbox already serves a Streamable-HTTP MCP endpoint at `/mcp`. This
package is a thin stdio↔HTTP bridge (via
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote)) for MCP clients that
speak local stdio servers — it forwards everything to your running mailboxd
instance and authenticates with your configured bearer token.

> docker-mailbox is **self-hosted**. This plugin does not ship a mail server —
> it connects to a mailboxd instance that **you** run. See the
> [docker-mailbox repo](https://github.com/psyb0t/docker-mailbox) to stand one up.

## Tools

The docker-mailbox MCP tools become available to your agent: `mailboxes`
(discovery), `inbox` (unified newest-first read across every configured IMAP
mailbox), `list_folders`, `list_messages`, `search` (structured single-mailbox
search), `get_message` (with optional `reader` mode — HTML stripped to clean
markdown), `get_raw_message`, `get_attachment`, `get_thread`, `set_labels`, `move_messages`, `create_draft`, `reply`, `send`, `send_draft`, `delete_message` (moves to Trash), `mark_seen`, and more. The server only lists the tools the token's scopes allow. Every per-mailbox tool
takes a `mailbox` argument (the configured name or its email address), so the
tool catalog stays constant-sized no matter how many mailboxes are configured.

## Configuration

| Env var | Required | Description |
|---|---|---|
| `MAILBOX_URL` | yes | Base URL of your running mailboxd server, e.g. `http://localhost:8000`. The bridge appends `/mcp`. |
| `MAILBOX_TOKEN` | yes | Bearer token matching an entry in the server's `auth.tokens`. |

## Install

Install it into your OpenClaw agent from ClawHub:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Then set `MAILBOX_URL` and `MAILBOX_TOKEN` in
the plugin's environment.

For clients on the Docker host, publish mailboxd with `-p 127.0.0.1:8000:8000`. The image still listens on `0.0.0.0:8000` internally. Another container uses a shared-network URL such as `http://mailboxd:8000`; its `localhost` refers to itself. Remote clients need a deliberately reachable endpoint, preferably HTTPS, and the configured bearer token.

## Native remote MCP (no install)

If your MCP client already supports **remote** Streamable-HTTP servers, you
don't need this bridge — point the client straight at
`$MAILBOX_URL/mcp` with an `Authorization: Bearer <token>` header.

## License

MIT. See [LICENSE](LICENSE).
