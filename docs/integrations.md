---
title: "Agent integrations"
description: "Install the mailbox agent skill and plugins in Claude Code, Codex and OpenClaw, or connect any MCP client to mailboxd's /mcp endpoint."
nav_order: 8
---

# Agent integrations

The [`docker-mailbox` skill](https://github.com/psyb0t/docker-mailbox/tree/main/.agents/skills/docker-mailbox) teaches an agent how to use a running mailboxd: discovery, paging, drafts, safety rules. It works in any agent that reads `.agents/skills/` (such as [peen](https://github.com/psyb0t/peen)) and installs natively in the clients below.

All of them talk to a server you run yourself. Give the agent a token with the narrowest scopes it needs, typically `read`, `draft` and `organize`.

## Claude Code

```bash
claude plugin marketplace add psyb0t/agents
claude plugin install mailbox@psyb0t
```

Claude Code asks for the mailboxd URL and the bearer token when you enable the plugin; the token is stored as a sensitive value.

## Codex

```bash
codex plugin marketplace add psyb0t/agents
codex plugin add mailbox@psyb0t
```

Invoke the marketplace skill as `$mailbox:docker-mailbox`. In a repository containing `.agents/skills/`, Codex finds the local skill as `$docker-mailbox` without installing anything.

## OpenClaw

The skill is published to ClawHub on every release:

```bash
openclaw skills install @psyb0t/docker-mailbox
```

For clients that only speak stdio MCP, the [`@psyb0t/mailbox`](https://github.com/psyb0t/docker-mailbox/tree/main/.agents/plugins/mailbox) plugin bridges to mailboxd's `/mcp` endpoint with `mcp-remote`:

```bash
openclaw plugins install clawhub:@psyb0t/mailbox
```

Set `MAILBOX_URL` (for example `http://localhost:8000`; the bridge adds `/mcp`) and `MAILBOX_TOKEN` in the plugin's environment.

## Any MCP client

Point the client at `http://<host>:8000/mcp` with an `Authorization: Bearer` header. See [MCP server](mcp.md#client-setup) for a `.mcp.json` example. The server is also listed in the official MCP Registry as `io.github.psyb0t/mailbox`, which points at the Docker image.

From another container, use a URL on a shared Docker network such as `http://mailboxd:8000`; `localhost` inside a container means that container.
