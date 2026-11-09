# @psyb0t/mailbox

Connect an OpenClaw agent or local MCP client to a running [docker-mailbox](https://github.com/psyb0t/docker-mailbox) service.

This package uses [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) to bridge local stdio clients to mailboxd's streamable HTTP endpoint at `/mcp`. It authenticates with your configured bearer token.

Run mailboxd yourself before connecting the plugin. See the [setup guide](https://github.com/psyb0t/docker-mailbox#quick-start).

## Tools

Tools cover mailbox discovery, unified inbox and search, message reading, raw source, attachments, threads, labels, moves, drafts, replies and sending. `delete_message` moves mail to Trash by default. The server lists only tools permitted by the token's scopes. Per-mailbox tools take a `mailbox` argument containing the configured name or email address. See the [tool reference](https://psyb0t.github.io/docker-mailbox/mcp/#tools).

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

Set `MAILBOX_URL` and `MAILBOX_TOKEN` in the plugin's environment.

For clients on the Docker host, publish mailboxd with `-p 127.0.0.1:8000:8000`. The image still listens on `0.0.0.0:8000` internally. Another container uses a shared-network URL such as `http://mailboxd:8000`; its `localhost` refers to itself. Remote clients need a deliberately reachable endpoint, preferably HTTPS, and the configured bearer token.

## Native remote MCP (no install)

Clients with native streamable HTTP support can connect directly to `$MAILBOX_URL/mcp` with an `Authorization: Bearer <token>` header.

## License

MIT. See [LICENSE](LICENSE).

## Permissions and state

Use a named token with explicit scopes and mailbox access. Read, draft and organize permissions can be granted without send or permanent-delete access. The bridge stores no mail. The server keeps operational state in SQLite and exposes admin-only webhook delivery inspection and replay. Mail and drafts remain on IMAP. Check send warnings before retrying.
