# Changelog

## v0.6.0

mailboxd becomes a connector that can be handed to an AI agent safely: scoped tokens, trash instead of erase, two-step drafts, send guards, and message analysis for phishing triage.

### New

1. **Raw source and headers.** `GET .../messages/{uid}/raw` (and the `get_raw_message` tool, truncated to `limits.mcp_inline_max_bytes`); `get_message` gains `headers=true` (every header in wire order) and returns `reply_to`, `in_reply_to`, `references`.
2. **Replies in the thread.** `POST .../messages/{uid}/reply` and the `reply` tool. Recipients, `Re:` subject, `In-Reply-To` and `References` are derived by the server from the original; `reply_all` removes the mailbox's own addresses; the original gets `\Answered`.
3. **Copy in Sent.** After a successful send the message (Bcc kept) is appended to the Sent folder (`smtp.save_sent`, default true). A failure is reported as `warnings: ["sent_copy_failed"]` and never fails the send.
4. **Two-step drafts.** `drafts` routes and `create_draft` / `list_drafts` / `update_draft` / `delete_draft` / `send_draft` tools. Drafts are stored with `\Draft` and an `X-Mailboxd-Draft-By` header; sending one strips internal headers, re-validates the identity and removes the draft.
5. **Scoped tokens.** `auth.tokens` entries are now `{name, token, scopes, mailboxes, dry_run}` with scopes `read`, `draft`, `organize`, `send`, `delete`, `admin`. `tools/list` only shows the tools a token may call; a mailbox outside a token's list is reported as not found. Bare string tokens still work (all scopes, all mailboxes) and log a warning.
6. **Trash and audit log.** Deleting moves to the Trash folder (`UID MOVE`, else `COPY` + `\Deleted` + `UID EXPUNGE` with `UIDPLUS`; without `UIDPLUS` the message is only flagged). `permanent=true` needs the `delete` scope and a Trash `folder`. Every write, and every refused write, is recorded in SQLite; `GET /audit` and the `audit_log` tool (admin) read it. Bodies are never stored.
7. **Send guards.** Rolling hourly rate limit per mailbox and per token (`send_guard.per_hour`, `smtp.per_hour`, `429` with `Retry-After`), dry-run (global, per token or per request) returning a preview, and a sender identity that must be configured (`422 invalid_identity`).
8. **Pagination and filters.** Opaque `cursor` / `next_cursor` paging, `has_attachments`, `recipient` (To, Cc, Delivered-To, X-Original-To), ISO dates, `total`.
9. **New mail only.** `since_cursor` / `next_since_cursor` and server-side named `checkpoint`s (per token, mailbox and folder). The oldest new messages come first so a checkpoint never skips mail; a UIDVALIDITY change returns `reset: true`.
10. **Conversations.** `GET .../thread` and `GET .../threads` (server `THREAD` when offered, local grouping by References / In-Reply-To otherwise).
11. **Labels, move, batches.** `POST .../messages/labels`, `.../messages/move`, `POST .../folders`, batch `seen` and `delete`; friendly labels map to IMAP keywords through `labels:` in the config; `label` search filter.
12. **Attachments.** Download (`GET .../attachments/{part_id}`, sanitized filename, `nosniff`, size cap, sha256 in the listing; `get_attachment` over MCP) and upload (`attachments` with `data_base64` or `from_message`) on send, reply and draft with a total size cap.
13. **Identities.** `smtp.identities` (address, display name, default), `GET .../identities`, `identity` argument everywhere.
14. **Signatures.** Per-identity text and HTML signatures, switchable with `signature: false`, never applied twice.
15. **Notifications.** `mailboxd watch` (IMAP IDLE with polling fallback) posts HMAC-SHA256 signed webhooks for new mail: headers and verdicts, never bodies. Optional `watch` service in `docker-compose.yml`.
16. **Search.** `text` / `body` full-text search with UTF-8 criteria exposed on every search route and tool.
17. **Safer HTML.** `body_html_safe` (nh3 allow-list, remote images removed) and `html_analysis` (`hidden_text`, `suspicious_links`).
18. **Stats and deep health.** `GET .../stats` (per-folder counts, quota) and `GET /health/deep` (admin).
19. **MCP polish.** Every tool has a title and `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` annotations; results carry `structuredContent`; errors are `isError` results with `{error: {code, message, details?}}`; blocking work runs off the event loop.

Also new: authentication verdicts (`authentication`: SPF, DKIM, DMARC, ARC) taken only from `Authentication-Results` headers stamped by `auth.trusted_authserv_id`, plus security signals (reply-to / return-path mismatch, display-name spoof, DKIM alignment); `${ENV}` interpolation in the config; `state.path` / `MAILBOXD_STATE_PATH`; `nh3` dependency; a state volume in the image and Compose file (published on `127.0.0.1` by default).

### Breaking changes

- **Tokens are scoped.** New-style tokens only hold the scopes you list. Legacy bare-string tokens keep every scope and every mailbox, but log a warning.
- **Delete moves to Trash.** `DELETE` / `delete_message` no longer erases. Permanent deletion needs the `delete` scope and must target the Trash folder. The folder-wide `EXPUNGE` that used to run on delete was removed and never runs again.
- **Reading no longer marks mail as read.** Messages are fetched with `BODY.PEEK[]` instead of `RFC822`.
- **`from_address` is validated.** It must match a configured identity (prefer the new `identity` field); other values return `422`.
- **No more Thunderbird `User-Agent`** header on outgoing mail.
- **MCP errors** are `isError` tool results with a structured `{error: {code, message}}` body instead of free text; HTTP errors gain the same `error` object next to the legacy `detail`.
- **State needs a writable path** (`/var/lib/mailboxd` in the image). Mount a volume or rate limits and the audit log are lost on restart.
- `GET /mailboxes` entries gain `address` and `identities`; stale documentation was removed (the "name is the MCP tool prefix" claim, POP3, and the `LOG_LEVEL` environment variable, which was never read: use `log_level` in the config).

## v0.5.1

- Clarify Docker's internal listener, the standalone CLI default, and host-side publishing. Documentation examples publish on host loopback and explain how to adjust the checked-in Compose file.

## v0.5.0

- Empty or omitted `auth.tokens` now denies protected HTTP and MCP requests instead of admitting anonymous access. `/health` remains public. Configure a bearer token and restart instances that previously ran without one.
- The standalone CLI now binds loopback by default. Set `--host 0.0.0.0` explicitly for network access. Container listeners remain available through published ports with bearer authentication.
- Pin PyJWT 2.15.1 to fix the reported dependency advisories. Dependency operations enforce a seven-day release-age cutoff.
- Close IMAP connections after login or TLS setup failures and report logout failures without logging credentials. Unified search no longer depends on a runtime assertion.
- Added HTTP, MCP, and real-network regression tests for authentication, mailbox selection, and credential forwarding. Configured tokens retain shared access to all mailboxes.
- Updated configuration examples, agent setup, and plugin metadata to require bearer credentials.
- Runtime version output now comes from installed package metadata.

## v0.4.16

Documentation only. The README's agent integrations section names [peen](https://github.com/psyb0t/peen) as an example of an agent that reads `.agents/skills/`.

## v0.4.15

Fixes the documented `/mcp` endpoint for Streamable HTTP clients that do not
follow redirects.

- `POST /mcp` returned `307` to `/mcp/` because the raw ASGI mount matched only
  the slash form. This broke MCP clients that retain the documented `/mcp` URL
  and do not follow redirects on Streamable HTTP requests.
- The app now normalizes only the exact `/mcp` path before routing. Bearer auth
  remains outermost, `/mcp/` still works, and the other HTTP routes keep their
  existing slash behavior.
- MCP contract tests now disable redirect following and cover both endpoint
  forms, so another redirect cannot pass unnoticed.
- Removed stale wording from the CI workflow comment. No behavior change.

## v0.4.14

Refreshes the MCP stack and locks the Python dependency graph.

- Upgraded the MCP SDK to 1.28.1 and Black to 26.3.1. Both runtime and
  development images now install strictly from `uv.lock`; they no longer fall
  back to an unlocked resolution.
- Added a Streamable HTTP MCP contract test that initializes the mounted route,
  lists the exposed tools, and calls `mailboxes` through the real application.
- Package commands now update exact pins correctly and support the `dev` group,
  so a development-only tool does not collide with runtime dependencies.
- Aligned Python runtime, package, health-response example, and Codex plugin
  version declarations with the release tag.

## v0.4.13

Fixes an IMAP command injection vulnerability (issue #1). Closes #1.

- The `folder`, `uid` and `search` values that reach the IMAP client were
  string-interpolated into IMAP commands and handed to Python's `imaplib`,
  which does not sanitize its own arguments. A CR/LF in any of them (reachable
  from an ordinary `%0D%0A` in an HTTP query or path parameter) ended the
  current command and spliced a second, attacker-chosen IMAP command onto the
  same authenticated connection, including a `STORE +FLAGS (\Deleted)` +
  `EXPUNGE` pair that deletes a whole folder through the read-only message
  routes.
- Every such value is now validated before it reaches `imaplib`: `folder`,
  `search` and structured search terms (including `since`/`before`) reject CR,
  LF and NUL; `uid` must be digits and sequence-set punctuation only; and
  folder names have any embedded `"` and `\` escaped so they cannot break out
  of the IMAP quoted string. The checks live in `imap_client`, so both the
  HTTP API and the MCP server inherit them.
- Added an in-process mock IMAP server and an IMAP test suite: the happy path
  (list, fetch, structured search) plus the injection cases, including the
  verbatim issue #1 payload driven through the read-only GET route.

## v0.4.12

Fixes the CI lint failure introduced by v0.4.11 so the image builds and publishes.

- v0.4.11 added `make lint` to CI for the first time, which surfaced a pre-existing flake8
  E501: a 103-character line in `src/mailboxd/server.py`. The lint job failed and the image
  build and publish steps, which depend on it, were skipped. Wrapped the line so flake8
  passes. No behavior change.

## v0.4.11

CI now lints, tests, and security-scans the code before building the image, via
the generic reusable code-workflow.

- New `code` pipeline job runs `make lint` (flake8 + mypy), `make test` (pytest
  unit + docker-in-docker integration), and `make sec`, and the image build now
  waits on it. The pipeline previously built and published the image without
  running any of these.
- New `make sec`: semgrep, bandit and pip-audit run in parallel and their
  findings merge into `sec.sarif` for the GitHub Security tab. It never fails the
  build; findings are reported, not gated. The scanners live in an isolated venv
  in the dev image so semgrep's bundled mcp pin cannot collide with the project's
  `mcp==1.9.0`.
- Removed the `make check` alias; use `make lint`, `make test`, `make sec`.
- Kept `.telemetry/` out of git and out of the image.

## v0.4.10

CI plumbing only. No code in this repo changed — every commit in this release touches `.github/workflows/`.

- The pipeline was split: building and publishing stay in `pipeline.yml`, and everything that leaves the host now lives beside it in `mirror-and-archive.yml`.
- The repo is mirrored to Codeberg as well as GitLab.
- It is archived to the Wayback Machine, Software Heritage and archive.org.
- Issues opened on either mirror are copied back to GitHub every six hours, and closed here when the original closes.
- Pull requests are switched off on the mirrors — they are force-pushed from GitHub, so anything merged there would be destroyed by the next sync. Issues and forking stay enabled.

## v0.4.9

Codex install command was missing from the README. Documentation only, no behavior change.

- The Codex subsection of `## Agent integrations` told readers to run `codex plugin marketplace add psyb0t/agents` and stopped there — it never showed the actual install command. Added `codex plugin add mailbox@psyb0t` right after it.
- Clarified that the invocation form depends on how the skill was obtained: installed via the marketplace it's `$mailbox:docker-mailbox`; picked up automatically (no install) from a repo's own `.agents/skills/` it's plain `$docker-mailbox`.

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
