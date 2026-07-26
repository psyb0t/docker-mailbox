# Changelog

## v0.4.1

- Hardened the skill docs with explicit destructive-operation guardrails and auth/exfil warnings. No behavior change — documentation only.
  - New "Security & safety" section in the `docker-mailbox` skill.
  - `DELETE /mailboxes/<name>/messages/<uid>` is now flagged as destructive & irreversible everywhere it's documented, with explicit confirm-before-delete guidance for agents.
  - The "Find and delete" workflow now treats the search step as a dry-run: list and confirm matches with the user before deleting, instead of piping a broad search straight into bulk delete.
  - Documented the unauthenticated-when-`auth.tokens`-is-empty risk and that `MAILBOX_URL` traffic leaves the host.

## v0.4.0

- ClawHub plugin: new `@psyb0t/mailbox` code plugin (stdio↔HTTP MCP bridge via `mcp-remote` to `/mcp`), MIT-licensed.
- Pipeline switched to reusable `clawhub-publish.yml` (publishes skill + plugin).
