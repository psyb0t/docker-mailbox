# Mailbox development

Mailbox is a Python service that exposes IMAP and SMTP accounts over HTTP and MCP. The application lives in `src/mailboxd/`. Tests live in `tests/` and operator documentation lives in `docs/`.

Run `make lint` for lint and type checks, `make test-unit` for unit tests, and `make test` for the full suite, including Docker integration tests. The Make targets run the checks in containers.

The package version comes from `pyproject.toml`. Dependency versions are locked in `uv.lock`. Use `config.example.yaml` for shared configuration examples.
