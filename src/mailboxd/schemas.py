"""HTTP request / response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    version: str


class MailboxSummary(BaseModel):
    name: str
    description: str = ""
    imap: bool
    smtp: bool


class MailboxList(BaseModel):
    mailboxes: list[MailboxSummary]


class FoldersResponse(BaseModel):
    folders: list[str]


class MessageHeader(BaseModel):
    uid: str | None = None
    number: int | None = None
    size: int | None = None
    flags: list[str] = Field(default_factory=list)
    from_: str = Field("", alias="from")
    to: str = ""
    subject: str = ""
    date: str = ""
    message_id: str = ""

    model_config = {"populate_by_name": True}


class MessagesResponse(BaseModel):
    messages: list[dict[str, Any]]


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


class SendRequest(BaseModel):
    to: list[str] = Field(..., min_length=1)
    subject: str
    body_text: str | None = None
    body_html: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    from_address: str | None = None
    reply_to: str | None = None


class SendResponse(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    subject: str
    message_id: str

    model_config = {"populate_by_name": True}


class GenericOK(BaseModel):
    ok: bool = True
