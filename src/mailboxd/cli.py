"""CLI entrypoint: `mailboxd http` runs the FastAPI HTTP server which also
serves the MCP streamable-HTTP transport under /mcp."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def _run_watch(config_path: str | None) -> int:
    import logging
    import signal
    import threading

    from . import watch
    from .config import ConfigError, load_config

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"mailboxd: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return watch.run(cfg, stop=stop)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailboxd")
    parser.add_argument("--version", action="version", version=f"mailboxd {__version__}")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_http = sub.add_parser(
        "http",
        help="Run the FastAPI HTTP server (also serves MCP under /mcp)",
    )
    p_http.add_argument("--host", default="127.0.0.1")
    p_http.add_argument("--port", type=int, default=8000)
    p_http.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "1")))
    p_http.add_argument("--config", help="Path to config.yaml (overrides MAILBOXD_CONFIG)")

    p_watch = sub.add_parser(
        "watch",
        help="Watch mailboxes (IMAP IDLE/poll) and POST signed webhooks for new mail",
    )
    p_watch.add_argument("--config", help="Path to config.yaml (overrides MAILBOXD_CONFIG)")

    args = parser.parse_args(argv)

    if args.config:
        os.environ["MAILBOXD_CONFIG"] = args.config

    if args.mode == "http":
        import uvicorn

        uvicorn.run(
            "mailboxd.server:create_app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            factory=True,
        )
        return 0

    if args.mode == "watch":
        return _run_watch(args.config)

    parser.error(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
