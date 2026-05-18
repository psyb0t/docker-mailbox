"""CLI entrypoint: `mailboxd http` runs the FastAPI HTTP server which also
serves the MCP streamable-HTTP transport under /mcp."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mailboxd")
    parser.add_argument("--version", action="version", version=f"mailboxd {__version__}")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_http = sub.add_parser(
        "http",
        help="Run the FastAPI HTTP server (also serves MCP under /mcp)",
    )
    p_http.add_argument("--host", default="0.0.0.0")
    p_http.add_argument("--port", type=int, default=8000)
    p_http.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "1")))
    p_http.add_argument("--config", help="Path to config.yaml (overrides MAILBOXD_CONFIG)")

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

    parser.error(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
