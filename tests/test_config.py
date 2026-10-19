from pathlib import Path

import pytest

from mailboxd.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return str(p)


def test_load_minimal_imap_only(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: a
    imap:
      host: imap.example.com
      username: u
      password: p
""",
        )
    )
    assert len(cfg.mailboxes) == 1
    m = cfg.get("a")
    assert m is not None
    assert m.imap is not None
    assert m.smtp is None
    assert m.imap.port == 993
    assert m.imap.tls == "ssl"
    assert m.imap.default_folder == "INBOX"


def test_load_both_protocols(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: full
    imap: {host: i, username: u, password: p}
    smtp: {host: s, username: u, password: p, from_address: u@example.com}
""",
        )
    )
    m = cfg.get("full")
    assert m is not None
    assert m.imap and m.smtp


def test_rejects_mailbox_with_no_protocols(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: nope
""",
            )
        )


def test_rejects_duplicate_names(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: dup
    imap: {host: x, username: u, password: p}
  - name: dup
    imap: {host: y, username: u, password: p}
""",
            )
        )


def test_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: "has spaces"
    imap: {host: x, username: u, password: p}
""",
            )
        )


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "this is: not [valid yaml :::"))


# -- v0.6: tokens, identities, labels, interpolation ---------------------

_MB = """
mailboxes:
  - name: contact
    imap: {host: i, username: contact@example.com, password: p}
    smtp:
      host: s
      username: u
      password: p
      from_address: "CGS <contact@example.com>"
  - name: support
    imap: {host: i, username: support@example.com, password: p}
"""


def test_token_configs_and_legacy_tokens(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
auth:
  tokens:
    - bare-legacy-token
    - name: mila
      token: mila-secret
      scopes: [read, draft]
      mailboxes: [contact]
      dry_run: true
"""
            + _MB,
        )
    )
    legacy, mila = cfg.auth.token_configs()
    assert legacy.name == "legacy-0"
    assert set(legacy.scopes) == {"read", "draft", "organize", "send", "delete", "admin"}
    assert legacy.mailboxes == "*"
    assert mila.name == "mila"
    assert mila.scopes == ["read", "draft"]
    assert mila.mailboxes == ["contact"]
    assert mila.dry_run is True


def test_unknown_scope_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "auth:\n  tokens: [{name: x, token: t, scopes: [root]}]\n" + _MB)
        )


def test_token_with_unknown_mailbox_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown mailboxes"):
        load_config(
            _write(tmp_path, "auth:\n  tokens: [{name: x, token: t, mailboxes: [nope]}]\n" + _MB)
        )


def test_duplicate_token_names_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicate token name"):
        load_config(
            _write(
                tmp_path,
                "auth:\n  tokens: [{name: x, token: a}, {name: x, token: b}]\n" + _MB,
            )
        )


def test_env_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MB_TEST_PASSWORD", "s3cret")
    monkeypatch.setenv("MB_TEST_TOKEN", "tok-from-env")
    cfg = load_config(
        _write(
            tmp_path,
            """
auth:
  tokens: [{name: a, token: "${MB_TEST_TOKEN}"}]
mailboxes:
  - name: m
    imap: {host: i, username: u, password: "pre-${MB_TEST_PASSWORD}-post"}
""",
        )
    )
    m = cfg.get("m")
    assert m is not None and m.imap is not None
    assert m.imap.password == "pre-s3cret-post"
    assert cfg.auth.token_configs()[0].token == "tok-from-env"


def test_missing_env_var_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MB_TEST_DEFINITELY_UNSET", raising=False)
    with pytest.raises(ConfigError, match="MB_TEST_DEFINITELY_UNSET"):
        load_config(
            _write(
                tmp_path,
                "mailboxes:\n  - name: m\n"
                '    imap: {host: i, username: u, password: "${MB_TEST_DEFINITELY_UNSET}"}\n',
            )
        )


def test_identities_default_and_derived(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MB))
    contact = cfg.get("contact")
    assert contact is not None and contact.smtp is not None
    smtp = contact.smtp
    ids = smtp.all_identities()
    assert len(ids) == 1
    assert ids[0].address == "contact@example.com"
    assert ids[0].name == "CGS"
    assert ids[0].default is True
    assert smtp.default_identity().formatted() == "CGS <contact@example.com>"


def test_explicit_identities_and_default_pick(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
mailboxes:
  - name: m
    imap: {host: i, username: u@example.com, password: p}
    smtp:
      host: s
      username: u
      password: p
      from_address: u@example.com
      identities:
        - {address: a@example.com, name: "A"}
        - {address: b@example.com, name: "Team CGS", default: true}
""",
        )
    )
    m = cfg.get("m")
    assert m is not None and m.smtp is not None
    assert m.smtp.default_identity().address == "b@example.com"
    assert m.own_addresses() == {"u@example.com", "a@example.com", "b@example.com"}


def test_identity_address_must_be_bare(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
mailboxes:
  - name: m
    smtp:
      host: s
      username: u
      password: p
      from_address: u@example.com
      identities: [{address: "Bob <b@example.com>"}]
""",
            )
        )


def test_default_labels_and_validation(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MB))
    assert cfg.labels["suspect"] == "$Suspect"
    assert cfg.labels["a_traiter"] == "$ATraiter"
    custom = load_config(_write(tmp_path, "labels: {urgent: $Urgent}\n" + _MB))
    assert custom.labels == {"urgent": "$Urgent"}
    for bad in ('"has space"', '"\\\\Seen"', '""', '"a(b"'):
        with pytest.raises(ConfigError, match="invalid IMAP keyword"):
            load_config(_write(tmp_path, f"labels: {{x: {bad}}}\n" + _MB))


def test_defaults_for_guard_limits_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILBOXD_STATE_PATH", "/tmp/custom-state.db")
    cfg = load_config(_write(tmp_path, _MB))
    assert cfg.send_guard.dry_run is False
    assert cfg.send_guard.per_hour == 60
    assert cfg.state.path == "/tmp/custom-state.db"
    assert cfg.limits.mcp_inline_max_bytes == 512 * 1024
    assert cfg.watch.poll_seconds == 60
