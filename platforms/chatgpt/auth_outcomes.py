"""Typed, secret-free outcomes for ChatGPT authentication stages."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum


class AuthFailureDomain(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SESSION = "session"
    CREDENTIAL = "credential"
    MFA = "mfa"
    EMAIL_BACKEND = "email_backend"
    EMAIL_CHALLENGE = "email_challenge"
    REMOTE_ACCOUNT = "remote_account"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthOutcome:
    ok: bool
    stage: str
    domain: AuthFailureDomain | None = None
    code: str = ""
    retryable: bool = False
    credential_rejected: bool = False
    email_fallback_used: bool = False
    email_risk_challenge: bool = False
    deferred: bool = False

    @classmethod
    def success(cls, *, stage: str, **flags) -> "AuthOutcome":
        return cls(ok=True, stage=str(stage or ""), **flags)

    @classmethod
    def failure(
        cls,
        *,
        stage: str,
        domain: AuthFailureDomain,
        code: str = "",
        retryable: bool = False,
        credential_rejected: bool = False,
        **flags,
    ) -> "AuthOutcome":
        return cls(
            ok=False,
            stage=str(stage or ""),
            domain=domain,
            code=str(code or ""),
            retryable=bool(retryable),
            credential_rejected=bool(credential_rejected),
            **flags,
        )

    def with_flags(self, **changes) -> "AuthOutcome":
        return replace(self, **changes)


@dataclass(frozen=True)
class VerificationCodeResult:
    """A mailbox code plus freshness metadata when the provider has it."""

    code: str
    message_id: str = ""
    received_at: datetime | float | str | None = None

    @classmethod
    def from_value(cls, value) -> "VerificationCodeResult | None":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            code = str(value.get("code") or value.get("verification_code") or "").strip()
            if not code:
                return None
            return cls(
                code=code,
                message_id=str(value.get("message_id") or value.get("id") or "").strip(),
                received_at=value.get("received_at") or value.get("receivedAt"),
            )
        object_code = getattr(value, "code", None)
        if object_code not in (None, ""):
            return cls(
                code=str(object_code).strip(),
                message_id=str(getattr(value, "message_id", "") or "").strip(),
                received_at=getattr(value, "received_at", None),
            )
        code = str(value or "").strip()
        return cls(code=code) if code else None

    def received_timestamp(self) -> float | None:
        value = self.received_at
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000 if numeric > 10_000_000_000 else numeric
        if isinstance(value, datetime):
            normalized = value
        else:
            try:
                normalized = datetime.fromisoformat(
                    str(value).strip().replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                return None
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    def is_fresh_for(self, otp_sent_at: float, *, skew_seconds: float = 5.0) -> bool:
        received = self.received_timestamp()
        return bool(
            self.message_id
            and received is not None
            and received >= float(otp_sent_at or 0) - float(skew_seconds)
        )


class EmailBackendError(RuntimeError):
    """Mailbox access failed before a fresh verification code was available."""

    error_code = "email_backend"

    def __init__(self, message: str, *, code: str = "email_backend") -> None:
        super().__init__(str(message or "邮箱后端不可用"))
        self.code = str(code or "email_backend")
        self.outcome = AuthOutcome.failure(
            stage="email_backend",
            domain=AuthFailureDomain.EMAIL_BACKEND,
            code=self.code,
            retryable=True,
        )
