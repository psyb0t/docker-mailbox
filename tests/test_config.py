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
