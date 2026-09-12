"""Pure reservation policy and metadata value types.

The PostgreSQL store is the authority for production timestamps and
serialization.  This module intentionally contains no application startup or
database engine initialization so the policy can be tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import secrets
from typing import Protocol


IDLE_TIMEOUT = timedelta(seconds=300)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware UTC timestamp authoritative to the caller."""


class UtcClock:
    """Clock used by non-database callers that need an application timestamp."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """Small deterministic clock for policy tests."""

    def __init__(self, value: datetime):
        self._value = ensure_utc(value)

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        self._value = ensure_utc(value)

    def advance(self, delta: timedelta) -> None:
        self._value += delta


class Outcome(str, Enum):
    CLAIMED = "claimed"
    BUSY = "busy"
    ACCEPTED = "accepted"
    VALIDATED = "validated"
    ATTACHED = "attached"
    RELEASED = "released"
    EXPIRED = "expired"
    INVALID_CAPABILITY = "invalid_capability"
    FENCED = "fenced"
    APPLIED = "applied"


class OperationKind(str, Enum):
    CLAIM = "claim"
    RENEW = "renew"
    HEARTBEAT = "heartbeat"
    REATTACH = "reattach"
    GOODBYE = "goodbye"
    RESULT = "result"


class IdempotencyConflict(ValueError):
    """The same operation key was reused for a different metadata request."""


@dataclass(frozen=True)
class ReservationSnapshot:
    resident_id: str
    state: str
    owner_id: str | None
    ownership_version: int
    last_accepted_visitor_message_at: datetime | None

    def is_expired(self, now: datetime) -> bool:
        return (
            self.state == "active"
            and self.last_accepted_visitor_message_at is not None
            and ensure_utc(now)
            >= ensure_utc(self.last_accepted_visitor_message_at) + IDLE_TIMEOUT
        )


@dataclass(frozen=True)
class OperationResult:
    resident_id: str
    outcome: Outcome
    ownership_version: int
    owner_id: str | None = None
    last_accepted_visitor_message_at: datetime | None = None
    capability: str | None = None
    event_id: int | None = None
    replayed: bool = False


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reservation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def new_capability() -> str:
    """Create an opaque synthetic caller capability.

    The clear token is returned only to the caller that claimed the resident;
    the storage layer persists only its verifier.
    """

    return secrets.token_urlsafe(32)


def capability_verifier(capability: str) -> str:
    if not capability:
        raise ValueError("capability must not be empty")
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def request_fingerprint(*parts: object) -> str:
    """Hash metadata used to detect unsafe idempotency-key reuse."""

    encoded = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_from_record(record: object) -> ReservationSnapshot:
    return ReservationSnapshot(
        resident_id=record.resident_id,
        state=record.state,
        owner_id=record.owner_id,
        ownership_version=record.ownership_version,
        last_accepted_visitor_message_at=record.last_accepted_visitor_message_at,
    )
