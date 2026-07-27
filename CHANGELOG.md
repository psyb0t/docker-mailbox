# Changelog

## v0.4.8

Renames the plugin from `docker-mailbox` to `mailbox`. Metadata only, no behavior change.

- **The install name is now `mailbox@psyb0t`.** v0.4.7 named the plugin after the repository, which made it the only entry in the `psyb0t` marketplace carrying a `docker-` prefix; every other plugin installs under its bare tool name. `mailbox` also matches the MCP-bridge plugin in `.agents/plugins/mailbox` and the container's own name.
- The ClawHub skill is unaffected and stays `@psyb0t/docker-mailbox`, as does `openclaw plugins install clawhub:@psyb0t/mailbox`. Only the Claude Code / Codex plugin identifier changed.

## v0.4.7

Agent-client distribution wiring. Documentation and metadata only, no behavior change.

- Added `.agents/.claude-plugin/plugin.json` and `.agents/.codex-plugin/plugin.json` so the existing `docker-mailbox` skill installs natively as a plugin in Claude Code and Codex, rooted at `.agents/`.
- New `## Agent integrations` README section (with Table of Contents entry) documenting the `claude plugin` / `codex plugin` marketplace install commands, the OpenClaw skill install, and the OpenClaw MCP-bridge plugin install for `@psyb0t/mailbox`.

## v0.4.6

- Added a GitHub Actions CI status badge to the README.

## v0.4.5

- Added self-hosted version and license badges plus a Docker Hub pulls badge; wired a badges job into pipeline.yml.

## v0.4.4

Listed on the official MCP Registry — no behavior change.

- Added `server.json` — published to the official Model Context Protocol Registry (`registry.modelcontextprotocol.io`) as `io.github.psyb0t/mailbox`, pointing at the `psyb0t/mailbox` Docker image. Ownership is proven by an `io.modelcontextprotocol.server.name` LABEL on the image; publishing runs on tag pushes via GitHub OIDC (secretless). Also added a `glama.json` maintainer claim.

## v0.4.3

Third-party license notices. Documentation only, no behavior change.

- Added `THIRD_PARTY.md` + `LICENSES/` documenting the GPL-3.0 `html2text` dependency bundled into the published image. The project's own code stays WTFPL.
- gitignore the container-generated `CLAUDE.md` so it never ships.

## v0.4.2

Skill docs de-duplicated. Documentation only, no behavior change.

- Collapsed the three repeated message-deletion warnings in `.agents/skills/docker-mailbox/SKILL.md` into one clear mention in the delete-endpoint reference plus a brief Security & safety note.

## v0.4.1

- Hardened the skill docs with explicit destructive-operation guardrails and auth/exfil warnings. No behavior change — documentation only.
  - New "Security & safety" section in the `docker-mailbox` skill.
  - `DELETE /mailboxes/<name>/messages/<uid>` is now flagged as destructive & irreversible everywhere it's documented, with explicit confirm-before-delete guidance for agents.
  - The "Find and delete" workflow now treats the search step as a dry-run: list and confirm matches with the user before deleting, instead of piping a broad search straight into bulk delete.
  - Documented the unauthenticated-when-`auth.tokens`-is-empty risk and that `MAILBOX_URL` traffic leaves the host.

## v0.4.0

- ClawHub plugin: new `@psyb0t/mailbox` code plugin (stdio↔HTTP MCP bridge via `mcp-remote` to `/mcp`), MIT-licensed.
- Pipeline switched to reusable `clawhub-publish.yml` (publishes skill + plugin).
