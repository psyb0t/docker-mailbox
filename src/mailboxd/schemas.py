"""HTTP request / response schemas (shared by the HTTP API and the MCP server)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    version: str


class IdentitySummary(BaseModel):
    address: str
    name: str = ""
    default: bool = False


class MailboxSummary(BaseModel):
    name: str
    description: str = ""
    address: str = ""
    imap: bool
    smtp: bool
    identities: list[IdentitySummary] = Field(default_factory=list)


class MailboxList(BaseModel):
    mailboxes: list[MailboxSummary]


class FolderDetail(BaseModel):
    name: str
    delimiter: str | None = None
    flags: list[str] = Field(default_factory=list)
    special: str | None = None


class FoldersResponse(BaseModel):
    folders: list[str]
    details: list[FolderDetail] = Field(default_factory=list)


class MessageHeader(BaseModel):
    uid: str | None = None
    number: int | None = None
    size: int | None = None
    flags: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    from_: str = Field("", alias="from")
    to: str = ""
    subject: str = ""
    date: str = ""
    message_id: str = ""

    model_config = {"populate_by_name": True}


class MessagesResponse(BaseModel):
    messages: list[dict[str, Any]]
    total: int | None = None
    next_cursor: str | None = None
    next_since_cursor: str | None = None


class MessageDetail(BaseModel):
    uid: str | None = None
    number: int | None = None
    from_: str = Field("", alias="from")
    to: str = ""
    cc: str = ""
    subject: str = ""
    date: str = ""
    message_id: str = ""
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class AttachmentRef(BaseModel):
    """Re-attach a part of a message that already sits in the same mailbox."""

    uid: str
    part_id: str
    folder: str | None = None


class AttachmentIn(BaseModel):
    filename: str | None = None
    content_type: str | None = None
    data_base64: str | None = None
    from_message: AttachmentRef | None = None


class ComposeRequest(BaseModel):
    to: list[str] = Field(..., min_length=1)
    subject: str
    body_text: str | None = None
    body_html: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    identity: str | None = None
    # Legacy alias of `identity`; must still match a configured identity.
    from_address: str | None = None
    reply_to: str | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list)
    signature: bool = True
    dry_run: bool = False


SendRequest = ComposeRequest


class ReplyRequest(BaseModel):
    body_text: str | None = None
    body_html: str | None = None
    reply_all: bool = False
    identity: str | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list)
    quote: bool = True
    draft: bool = False
    signature: bool = True
    dry_run: bool = False


class DraftUpdate(BaseModel):
    """Every field optional: None keeps the draft's current value."""

    to: list[str] | None = None
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    identity: str | None = None
    from_address: str | None = None
    reply_to: str | None = None
    attachments: list[AttachmentIn] | None = None
    signature: bool | None = None


class SendResult(BaseModel):
    dry_run: bool
    from_: str = Field("", alias="from")
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    message_id: str = ""
    sent_copy: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    preview: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class DraftResult(BaseModel):
    uid: str | None = None
    folder: str
    message_id: str = ""
    replaced_uid: str | None = None


class SendDraftRequest(BaseModel):
    dry_run: bool = False


class LabelsRequest(BaseModel):
    uids: list[str] = Field(..., min_length=1)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class MoveRequest(BaseModel):
    uids: list[str] = Field(..., min_length=1)
    to_folder: str


class SeenRequest(BaseModel):
    seen: bool = True
    uids: list[str] | None = None


class FolderCreate(BaseModel):
    name: str


class GenericOK(BaseModel):
    ok: bool = True
