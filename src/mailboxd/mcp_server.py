"""MCP streamable-HTTP server exposing the same operations as the HTTP API.

Tool naming: `<mailbox>__<verb>` so multiple mailboxes don't collide.
The set of registered tools depends on which protocols each mailbox has
configured (no IMAP → no list/fetch/delete tools for that mailbox).

This module exposes a `StreamableHTTPSessionManager` and a lifespan helper
that the FastAPI app mounts under `/mcp`. There is no stdio transport.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, AsyncIterator, Callable

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__
from .config import Config, MailboxConfig
from .imap_client import ImapError
from .imap_client import delete_message as imap_delete
from .imap_client import fetch_message as imap_fetch
from .imap_client import list_folders as imap_list_folders
from .imap_client import list_messages as imap_list_messages
from .imap_client import mark_seen as imap_mark_seen
from .imap_client import search_messages as imap_search_messages
from .imap_client import unified_search as imap_unified_search
from .smtp_client import SmtpError
from .smtp_client import send as smtp_send

_SEARCH_PROPS: dict[str, Any] = {
    "folder": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
    "from": {"type": "string", "description": "Filter by From address"},
    "to": {"type": "string"},
    "subject": {"type": "string"},
    "body": {"type": "string"},
    "text": {"type": "string", "description": "Full-text search"},
    "since": {"type": "string", "description": "IMAP date e.g. 1-Jan-2026"},
    "before": {"type": "string"},
    "unseen": {"type": "boolean"},
    "seen": {"type": "boolean"},
    "flagged": {"type": "boolean"},
    "answered": {"type": "boolean"},
    "larger_than": {"type": "integer", "minimum": 0},
    "smaller_than": {"type": "integer", "minimum": 0},
}


def _search_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "folder": args.get("folder"),
        "limit": int(args.get("limit", 50)),
        "from_": args.get("from"),
        "to": args.get("to"),
        "subject": args.get("subject"),
        "body": args.get("body"),
        "text": args.get("text"),
        "since": args.get("since"),
        "before": args.get("before"),
        "unseen": args.get("unseen"),
        "seen": args.get("seen"),
        "flagged": args.get("flagged"),
        "answered": args.get("answered"),
        "larger_than": args.get("larger_than"),
        "smaller_than": args.get("smaller_than"),
    }


def _build_tools(
    cfg: Config,
) -> tuple[list[types.Tool], dict[str, Callable[[dict[str, Any]], Any]]]:
    tools: list[types.Tool] = []
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    _register_unified(cfg, tools, handlers)

    for mb in cfg.mailboxes:
        prefix = mb.name

        if mb.imap is not None:
            _register_imap(prefix, mb, tools, handlers)
        if mb.smtp is not None:
            _register_smtp(prefix, mb, tools, handlers)

    return tools, handlers


def _register_unified(
    cfg: Config,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    """Top-level unified-inbox tool that fans out across all IMAP mailboxes."""
    if not any(mb.imap is not None for mb in cfg.mailboxes):
        return

    schema_props: dict[str, Any] = {
        "mailbox": {
            "type": "string",
            "description": (
                "CSV of mailbox names or email addresses to restrict the search "
                "to. Omit for all IMAP mailboxes."
            ),
        },
        **_SEARCH_PROPS,
    }

    tools.append(
        types.Tool(
            name="inbox",
            description=(
                "Unified inbox across all configured IMAP mailboxes. Returns a "
                "merged, newest-first feed. Use `mailbox` to filter by mailbox "
                "name or email address. Use `from`, `subject`, etc. to filter "
                "content. Each result is tagged with its source `mailbox` and "
                "`mailbox_address`."
            ),
            inputSchema={
                "type": "object",
                "properties": schema_props,
                "additionalProperties": False,
            },
        )
    )

    def _inbox(args: dict[str, Any], _c: Config = cfg) -> dict[str, Any]:
        filter_csv = args.get("mailbox")
        if filter_csv:
            wanted = {s.strip() for s in str(filter_csv).split(",") if s.strip()}
            targets = [
                m
                for m in _c.mailboxes
                if m.imap is not None and (m.name in wanted or m.imap.username in wanted)
            ]
        else:
            targets = [m for m in _c.mailboxes if m.imap is not None]
        return imap_unified_search(targets, **_search_kwargs(args))

    handlers["inbox"] = _inbox


def _register_imap(
    prefix: str,
    mb: MailboxConfig,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    assert mb.imap is not None
    imap = mb.imap

    tools.append(
        types.Tool(
            name=f"{prefix}__list_folders",
            description=f"List IMAP folders in mailbox '{mb.name}'.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    )

    def _list_folders(_args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        return {"folders": imap_list_folders(_i)}

    handlers[f"{prefix}__list_folders"] = _list_folders

    tools.append(
        types.Tool(
            name=f"{prefix}__list_messages",
            description=(
                f"List recent messages from mailbox '{mb.name}'. "
                "Returns newest-first headers; uid is what other tools want."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder (default INBOX)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    "search": {
                        "type": "string",
                        "description": "IMAP SEARCH criteria, e.g. 'UNSEEN' or 'FROM foo@bar'",
                        "default": "ALL",
                    },
                },
                "additionalProperties": False,
            },
        )
    )

    def _list_messages(args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        return {
            "messages": imap_list_messages(
                _i,
                folder=args.get("folder"),
                limit=int(args.get("limit", 50)),
                search=str(args.get("search", "ALL")),
            )
        }

    handlers[f"{prefix}__list_messages"] = _list_messages

    tools.append(
        types.Tool(
            name=f"{prefix}__search",
            description=(
                f"Structured search in mailbox '{mb.name}'. Filter by from/to/"
                "subject/body/text/dates/flags/size. Returns newest-first headers."
            ),
            inputSchema={
                "type": "object",
                "properties": _SEARCH_PROPS,
                "additionalProperties": False,
            },
        )
    )

    def _search(args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        return {"messages": imap_search_messages(_i, **_search_kwargs(args))}

    handlers[f"{prefix}__search"] = _search

    tools.append(
        types.Tool(
            name=f"{prefix}__get_message",
            description=f"Fetch a full message (incl. body) from mailbox '{mb.name}' by UID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                },
                "required": ["uid"],
                "additionalProperties": False,
            },
        )
    )

    def _get_message(args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        return imap_fetch(_i, str(args["uid"]), folder=args.get("folder"))

    handlers[f"{prefix}__get_message"] = _get_message

    tools.append(
        types.Tool(
            name=f"{prefix}__delete_message",
            description=f"Permanently delete a message (flag + expunge) in mailbox '{mb.name}'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                },
                "required": ["uid"],
                "additionalProperties": False,
            },
        )
    )

    def _del(args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        imap_delete(_i, str(args["uid"]), folder=args.get("folder"))
        return {"ok": True}

    handlers[f"{prefix}__delete_message"] = _del

    tools.append(
        types.Tool(
            name=f"{prefix}__mark_seen",
            description=f"Set or clear the \\Seen flag on a message in mailbox '{mb.name}'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "folder": {"type": "string"},
                    "seen": {"type": "boolean", "default": True},
                },
                "required": ["uid"],
                "additionalProperties": False,
            },
        )
    )

    def _seen(args: dict[str, Any], _i: Any = imap) -> dict[str, Any]:
        imap_mark_seen(
            _i,
            str(args["uid"]),
            folder=args.get("folder"),
            seen=bool(args.get("seen", True)),
        )
        return {"ok": True}

    handlers[f"{prefix}__mark_seen"] = _seen


def _register_smtp(
    prefix: str,
    mb: MailboxConfig,
    tools: list[types.Tool],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> None:
    assert mb.smtp is not None
    smtp = mb.smtp

    tools.append(
        types.Tool(
            name=f"{prefix}__send",
            description=f"Send an email from mailbox '{mb.name}' via SMTP.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "subject": {"type": "string"},
                    "body_text": {"type": "string"},
                    "body_html": {"type": "string"},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "bcc": {"type": "array", "items": {"type": "string"}},
                    "from_address": {"type": "string"},
                    "reply_to": {"type": "string"},
                },
                "required": ["to", "subject"],
                "additionalProperties": False,
            },
        )
    )

    def _send(args: dict[str, Any], _s: Any = smtp) -> dict[str, Any]:
        return smtp_send(
            _s,
            to=list(args["to"]),
            subject=str(args["subject"]),
            body_text=args.get("body_text"),
            body_html=args.get("body_html"),
            cc=list(args.get("cc") or []),
            bcc=list(args.get("bcc") or []),
            from_address=args.get("from_address"),
            reply_to=args.get("reply_to"),
        )

    handlers[f"{prefix}__send"] = _send


def build_mcp_app(cfg: Config) -> Server:
    """Build the low-level MCP `Server` with all tools registered for `cfg`."""
    tools, handlers = _build_tools(cfg)

    server: Server = Server("mailboxd", version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        fn = handlers.get(name)
        if fn is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            result = fn(arguments or {})
        except (ImapError, SmtpError) as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def build_session_manager(cfg: Config) -> StreamableHTTPSessionManager:
    """Build a streamable-HTTP session manager for the MCP server.

    The returned manager must be driven by its `run()` async-context manager,
    typically from the host ASGI app's lifespan.
    """
    mcp_app = build_mcp_app(cfg)
    # stateless=True keeps things simple: no server-side session table, every
    # request carries its own correlation. Plays well behind any LB.
    return StreamableHTTPSessionManager(
        mcp_app,
        json_response=False,
        stateless=True,
    )


@contextlib.asynccontextmanager
async def mcp_lifespan(manager: StreamableHTTPSessionManager) -> AsyncIterator[None]:
    """Lifespan hook that starts/stops the MCP session manager."""
    async with manager.run():
        yield


def get_notification_options() -> NotificationOptions:
    """Exposed for callers/tests that need the default notification options."""
    return NotificationOptions()
