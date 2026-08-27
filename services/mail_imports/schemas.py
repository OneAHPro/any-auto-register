from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


MailImportProviderType = Literal["applemail", "microsoft"]
MailImportExecuteProviderType = Literal["auto", "applemail", "microsoft"]
MailImportAccountType = Literal[
    "microsoft_oauth",
    "mailapi_url",
    "applemail_oauth",
    "icloud_web",
    "chatgpt_password",
    "chatgpt_google_password",
    "chatgpt_password_totp",
    "chatgpt_password_remote_totp",
    "chatgpt_password_url_otp",
    "chatgpt_password_reset_url_mail",
]

DEFAULT_PREVIEW_LIMIT = 100
MAX_PREVIEW_LIMIT = 500


class MailImportProviderDescriptor(BaseModel):
    type: MailImportProviderType
    label: str
    description: str
    content_placeholder: str
    helper_text: str = ""
    supports_filename: bool = False
    filename_label: str = ""
    filename_placeholder: str = ""
    preview_empty_text: str = ""


class MailImportSnapshotItem(BaseModel):
    index: int
    email: str
    mailbox: str = ""
    enabled: bool | None = None
    has_oauth: bool | None = None
    account_type: MailImportAccountType | None = None
    pool_state: str = "available"
    last_error: str = ""
    last_task_id: str = ""


class MailImportSnapshotRequest(BaseModel):
    type: MailImportProviderType
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportExecuteRequest(BaseModel):
    type: MailImportExecuteProviderType
    content: str
    preferred_provider: MailImportProviderType | None = None
    filename: str = ""
    pool_dir: str = ""
    pool_file: str = ""
    enabled: bool = True
    bind_to_config: bool = True
    alias_split_enabled: bool = False
    alias_split_count: int = Field(default=5, ge=1, le=5)
    alias_include_original: bool = False
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportDeleteRequest(BaseModel):
    type: MailImportProviderType
    email: str
    mailbox: str = ""
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportDeleteItem(BaseModel):
    email: str
    mailbox: str = ""


class MailImportBatchDeleteRequest(BaseModel):
    type: MailImportProviderType
    items: list[MailImportDeleteItem] = Field(default_factory=list)
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportSnapshot(BaseModel):
    type: MailImportProviderType
    label: str
    count: int
    available_count: int | None = None
    visible_count: int | None = None
    items: list[MailImportSnapshotItem] = Field(default_factory=list)
    truncated: bool = False
    filename: str = ""
    path: str = ""
    pool_dir: str = ""


class MailImportSummary(BaseModel):
    total: int
    success: int
    failed: int


class MailImportResponse(BaseModel):
    type: MailImportExecuteProviderType
    summary: MailImportSummary
    snapshot: MailImportSnapshot
    errors: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class MailImportDetectionRequest(BaseModel):
    content: str = Field(min_length=1)


class MailImportDetectionRow(BaseModel):
    line_number: int
    email: str = ""
    provider: MailImportProviderType | None = None
    account_type: MailImportAccountType | None = None
    resolved: bool
    message: str = ""


class MailImportDetectionResponse(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    can_import: bool
    has_duplicates: bool = False
    duplicate_emails: list[str] = Field(default_factory=list)
    rows: list[MailImportDetectionRow] = Field(default_factory=list)
