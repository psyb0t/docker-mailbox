---
title: "Troubleshooting"
description: "mailboxd startup errors and common API failures, with the exact messages they print and how to fix each one."
nav_order: 10
---

# Troubleshooting

## Startup errors

`mailboxd http` and `mailboxd watch` check the config before doing anything else. On a problem they print one line starting with `mailboxd:` and exit with status 2:

| Message | Cause and fix |
| --- | --- |
| `config not found: <path>` | Nothing at `--config`, `MAILBOXD_CONFIG` or `/etc/mailboxd/config.yaml`. Check the mount |
| `config <path> is not valid YAML` | Fix the YAML syntax |
| `config <path> must be a YAML mapping` | The top level must be keys and values, not a list or a scalar |
| `environment variable 'NAME' referenced at <field> is unset` | The config uses `${NAME}`; pass it with `--env-file` or `-e` |
| `config <path> invalid: <field>: <rule>` | A field failed validation. Each problem is listed as its field path and rule, for example `mailboxes.0.imap.port: Input should be a valid integer` or `config: Value error, token 'a' references unknown mailboxes: ['nope']`. Field values are never printed |

Validation rules that commonly fail:

- mailbox `name` must match `[a-zA-Z0-9_-]+` and be unique;
- each mailbox needs `imap`, `smtp` or both;
- token names and token values must be unique and not empty;
- token and webhook `mailboxes` lists must name existing mailboxes;
- label keywords cannot contain spaces, quotes, parentheses, `\`, `%`, `*`, `{` or `]`;
- identity addresses must be bare addresses or `*@domain`; a pattern cannot be the default, and at least one concrete identity is required;
- webhook URLs need `http` or `https` and a host name, without user info or a fragment.

The container exits too, so `docker logs` shows the line. Releases before v0.8.1 started uvicorn directly; with more than one worker they kept restarting the failing workers and the container stayed up as `unhealthy`.

A state database created by a newer mailboxd is refused the first time a request or the watcher uses it, with `state database was created by a newer mailboxd version` in the log. Restore a backup taken with the older version, or run the newer image.

## Watcher

| Symptom | Cause and fix |
| --- | --- |
| `nothing to watch: need an IMAP mailbox and a matching watch.webhooks entry`, exit 2 | Add a `watch.webhooks` entry whose `mailboxes` includes a mailbox with `imap` |
| `watch-health` fails | No heartbeat in 90 seconds: the watcher is down, stuck, or using a different state database |
| `watch connection failed` in the log | The IMAP connection dropped or could not open. The watcher retries with backoff up to 5 minutes |
| Events never arrive | List deliveries with `GET /mailboxes/<name>/webhook-deliveries`. `pending` with growing `attempts` means your receiver is not answering `2xx`. A redirect counts as a failure |
| No events for mail that was already there | Expected. The watcher starts at the current newest message and only reports mail that arrives later |

## API errors

| Response | Cause and fix |
| --- | --- |
| `401` `missing or invalid Bearer token` | Wrong or missing `Authorization: Bearer` header, or the config has no tokens |
| `403 permission_denied` | The token lacks `details.required_scope`. Add it in the config and restart |
| `404 not_found` for a mailbox you configured | The token's `mailboxes` list leaves it out. Hidden and unknown look the same on purpose |
| `409 conflict`, "has no IMAP configured" or "has no SMTP configured" | The route needs a protocol this mailbox does not have |
| `409 conflict`, "messages are already in the trash folder" | Use `permanent=true` with a `delete` token to erase them |
| `409 conflict`, "permanent deletion requires UIDPLUS" | The server cannot expunge single messages safely. Delete from your mail client instead |
| `409 conflict`, "draft already sent or delivery outcome requires manual review" | That draft was sent, or a send attempt for it had an unknown result. Check the Sent folder before making a new draft |
| `413 too_large` | Raise the matching [limit](configuration.md#limits). Over MCP, download big attachments over HTTP instead |
| `422 invalid_identity` | Use one of the addresses in `details.allowed`, or add an identity |
| `429 rate_limited` | Wait `Retry-After` seconds, or raise `per_hour` |
| `502 imap_error`, `502 smtp_error` | The server refused the login or a command, or could not be reached. Check host, port and `tls`, then run `GET /health/deep` with an admin token to see the server's own message |

## TLS

A private server with a self-signed or internal certificate fails with a certificate verification error. Mount the CA bundle read-only and set `SSL_CERT_FILE` to it. A certificate issued for another host name fails the same way; connect with the name on the certificate.

## Folders

If Sent, Drafts or Trash land in the wrong folder, the server's special-use flags and the fallback names did not match. Pin them under `folders:` in the mailbox config. `GET /mailboxes/<name>/folders` shows what was detected.
