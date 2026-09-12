"""Synchronous PostgreSQL persistence for conversation reservations.

This module creates no engine from environment variables.  A caller must
provide an explicitly configured engine or session factory, which keeps tests
and the future API integration separate from the production startup path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import hmac
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import (
    IdempotencyConflict,
    OperationKind,
    OperationResult,
    Outcome,
    ReservationSnapshot,
    capability_verifier,
    ensure_utc,
    new_capability,
    request_fingerprint,
    snapshot_from_record,
)


class Base(DeclarativeBase):
    pass


class ReservationRecord(Base):
    __tablename__ = "conversation_reservations"
    __table_args__ = (
        CheckConstraint("state IN ('active', 'released')", name="reservation_state_ck"),
        CheckConstraint("ownership_version >= 0", name="reservation_version_ck"),
        CheckConstraint(
            "(state = 'active' AND owner_id IS NOT NULL AND capability_verifier IS NOT NULL "
            "AND last_accepted_visitor_message_at IS NOT NULL) OR "
            "(state = 'released' AND owner_id IS NULL AND capability_verifier IS NULL "
            "AND last_accepted_visitor_message_at IS NULL)",
            name="reservation_shape_ck",
        ),
    )

    resident_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(255))
    capability_verifier: Mapped[str | None] = mapped_column(String(64))
    last_accepted_visitor_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    ownership_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )


class ReservationOperation(Base):
    __tablename__ = "conversation_reservation_operations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "operation_kind",
            "resident_id",
            "idempotency_key",
            name="reservation_operation_pk",
        ),
        Index("reservation_operation_resident_idx", "resident_id"),
    )

    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    resident_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("conversation_reservations.resident_id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(255))
    ownership_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LifecycleEvent(Base):
    __tablename__ = "conversation_reservation_events"
    __table_args__ = (
        UniqueConstraint(
            "resident_id", "ownership_version", name="reservation_event_version_uq"
        ),
        CheckConstraint("event_type IN ('released', 'expired')", name="reservation_event_type_ck"),
        Index("reservation_event_resident_idx", "resident_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    resident_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("conversation_reservations.resident_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    ownership_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_last_accepted_visitor_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PostgresReservationStore:
    """Transactional reservation store.

    By default, every transaction samples PostgreSQL ``clock_timestamp()``
    after acquiring the resident row lock.  Tests may inject a timestamp
    provider, but callers cannot pass timestamps to reservation operations.
    """

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        clock: Callable[[Session], datetime] | None = None,
    ) -> None:
        if session_factory is None:
            if engine is None:
                raise ValueError("provide an engine or session_factory")
            session_factory = sessionmaker(
                bind=engine, autoflush=False, expire_on_commit=False
            )
        self._session_factory = session_factory
        self._clock = clock or self._database_clock

    @staticmethod
    def _database_clock(session: Session) -> datetime:
        value = session.execute(select(func.clock_timestamp())).scalar_one()
        return ensure_utc(value)

    @staticmethod
    def _validate_metadata(*values: str) -> None:
        for value in values:
            if not value or len(value) > 255:
                raise ValueError("synthetic metadata identifiers must be 1..255 characters")

    @staticmethod
    def _safe_verifier(capability: str) -> str:
        try:
            return capability_verifier(capability)
        except ValueError:
            return "invalid"

    @staticmethod
    def _require_expected_version(expected_version: int) -> None:
        if expected_version < 1:
            raise ValueError("expected ownership version must be positive")

    def _begin(self) -> tuple[Session, Any]:
        session = self._session_factory()
        return session, session.begin()

    @staticmethod
    def _ensure_reservation(session: Session, resident_id: str) -> ReservationRecord:
        session.execute(
            pg_insert(ReservationRecord)
            .values(resident_id=resident_id, state="released", ownership_version=0)
            .on_conflict_do_nothing(index_elements=[ReservationRecord.resident_id])
        )
        return session.execute(
            select(ReservationRecord)
            .where(ReservationRecord.resident_id == resident_id)
            .with_for_update()
        ).scalar_one()

    @staticmethod
    def _find_operation(
        session: Session,
        operation_kind: OperationKind,
        resident_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> ReservationOperation | None:
        operation = session.execute(
            select(ReservationOperation).where(
                ReservationOperation.operation_kind == operation_kind.value,
                ReservationOperation.resident_id == resident_id,
                ReservationOperation.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if operation is not None and operation.request_fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"{operation_kind.value} idempotency key was reused with different metadata"
            )
        return operation

    @staticmethod
    def _operation_result(
        operation: ReservationOperation,
    ) -> OperationResult:
        return OperationResult(
            resident_id=operation.resident_id,
            outcome=Outcome(operation.outcome),
            ownership_version=operation.ownership_version,
            owner_id=operation.owner_id,
            last_accepted_visitor_message_at=operation.accepted_at,
            event_id=operation.event_id,
            replayed=True,
        )

    @staticmethod
    def _record_operation(
        session: Session,
        *,
        kind: OperationKind,
        resident_id: str,
        idempotency_key: str,
        fingerprint: str,
        outcome: Outcome,
        ownership_version: int,
        owner_id: str | None,
        accepted_at: datetime | None,
        event_id: int | None = None,
    ) -> ReservationOperation:
        operation = ReservationOperation(
            operation_kind=kind.value,
            resident_id=resident_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            outcome=outcome.value,
            owner_id=owner_id,
            ownership_version=ownership_version,
            accepted_at=accepted_at,
            event_id=event_id,
        )
        session.add(operation)
        session.flush()
        return operation

    @staticmethod
    def _capability_matches(record: ReservationRecord, owner_id: str, capability: str) -> bool:
        return (
            record.owner_id == owner_id
            and record.capability_verifier is not None
            and hmac.compare_digest(
                record.capability_verifier,
                PostgresReservationStore._safe_verifier(capability),
            )
        )

    @staticmethod
    def _authorization_outcome(
        record: ReservationRecord,
        owner_id: str,
        capability: str,
        expected_version: int,
    ) -> Outcome:
        if expected_version != record.ownership_version or record.state != "active":
            return Outcome.FENCED
        if not PostgresReservationStore._capability_matches(record, owner_id, capability):
            return Outcome.INVALID_CAPABILITY
        return Outcome.ACCEPTED

    def _reconcile_expiry(
        self,
        session: Session,
        record: ReservationRecord,
        now: datetime,
    ) -> LifecycleEvent | None:
        if not snapshot_from_record(record).is_expired(now):
            return None
        previous_owner = record.owner_id
        previous_last_message = record.last_accepted_visitor_message_at
        if previous_owner is None or previous_last_message is None:
            raise RuntimeError("active reservation shape was invalid")
        next_version = record.ownership_version + 1
        record.state = "released"
        record.owner_id = None
        record.capability_verifier = None
        record.last_accepted_visitor_message_at = None
        record.ownership_version = next_version
        session.flush()
        event = LifecycleEvent(
            resident_id=record.resident_id,
            event_type="expired",
            owner_id=previous_owner,
            ownership_version=next_version,
            previous_last_accepted_visitor_message_at=previous_last_message,
            occurred_at=now,
        )
        session.add(event)
        session.flush()
        return event

    def claim(self, resident_id: str, owner_id: str, idempotency_key: str) -> OperationResult:
        """Atomically claim a resident with the synthetic adapter's first message.

        The initial claim timestamp is the accepted first visitor message for
        this trial.  It is a transport convention, not product initial-claim
        approval.
        """

        self._validate_metadata(resident_id, owner_id, idempotency_key)
        fingerprint = request_fingerprint(OperationKind.CLAIM.value, resident_id, owner_id)
        session, transaction = self._begin()
        try:
            with transaction:
                record = self._ensure_reservation(session, resident_id)
                previous = self._find_operation(
                    session, OperationKind.CLAIM, resident_id, idempotency_key, fingerprint
                )
                if previous is not None:
                    return self._operation_result(previous)

                now = ensure_utc(self._clock(session))
                expired_event = self._reconcile_expiry(session, record, now)
                if expired_event is not None:
                    # The expired row is immediately eligible for the new claim.
                    pass
                if record.state == "active":
                    operation = self._record_operation(
                        session,
                        kind=OperationKind.CLAIM,
                        resident_id=resident_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        outcome=Outcome.BUSY,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=Outcome.BUSY,
                        ownership_version=record.ownership_version,
                    )

                capability = new_capability()
                next_version = record.ownership_version + 1
                record.state = "active"
                record.owner_id = owner_id
                record.capability_verifier = capability_verifier(capability)
                record.last_accepted_visitor_message_at = now
                record.ownership_version = next_version
                self._record_operation(
                    session,
                    kind=OperationKind.CLAIM,
                    resident_id=resident_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    outcome=Outcome.CLAIMED,
                    ownership_version=next_version,
                    owner_id=owner_id,
                    accepted_at=now,
                )
                return OperationResult(
                    resident_id=resident_id,
                    outcome=Outcome.CLAIMED,
                    ownership_version=next_version,
                    owner_id=owner_id,
                    last_accepted_visitor_message_at=now,
                    capability=capability,
                )
        finally:
            session.close()

    def _authorized_operation(
        self,
        *,
        kind: OperationKind,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        idempotency_key: str,
        renew_timestamp: bool,
        outcome_on_success: Outcome,
    ) -> OperationResult:
        self._validate_metadata(resident_id, owner_id, idempotency_key)
        self._require_expected_version(expected_version)
        fingerprint = request_fingerprint(
            kind.value,
            resident_id,
            owner_id,
            self._safe_verifier(capability),
            expected_version,
        )
        session, transaction = self._begin()
        try:
            with transaction:
                record = self._ensure_reservation(session, resident_id)
                previous = self._find_operation(
                    session, kind, resident_id, idempotency_key, fingerprint
                )
                if previous is not None:
                    return self._operation_result(previous)

                now = ensure_utc(self._clock(session))
                expired_event = self._reconcile_expiry(session, record, now)
                if expired_event is not None:
                    operation = self._record_operation(
                        session,
                        kind=kind,
                        resident_id=resident_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                        event_id=expired_event.id,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        event_id=operation.event_id,
                    )

                authorization = self._authorization_outcome(
                    record, owner_id, capability, expected_version
                )
                if authorization != Outcome.ACCEPTED:
                    operation = self._record_operation(
                        session,
                        kind=kind,
                        resident_id=resident_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                        event_id=operation.event_id,
                    )

                if renew_timestamp:
                    record.last_accepted_visitor_message_at = now
                self._record_operation(
                    session,
                    kind=kind,
                    resident_id=resident_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    outcome=outcome_on_success,
                    ownership_version=record.ownership_version,
                    owner_id=owner_id,
                    accepted_at=now if renew_timestamp else None,
                )
                return OperationResult(
                    resident_id=resident_id,
                    outcome=outcome_on_success,
                    ownership_version=record.ownership_version,
                    owner_id=owner_id,
                    last_accepted_visitor_message_at=(
                        now if renew_timestamp else record.last_accepted_visitor_message_at
                    ),
                )
        finally:
            session.close()

    def renew(
        self,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OperationResult:
        """Accept one visitor message and renew exactly once."""

        return self._authorized_operation(
            kind=OperationKind.RENEW,
            resident_id=resident_id,
            owner_id=owner_id,
            capability=capability,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            renew_timestamp=True,
            outcome_on_success=Outcome.ACCEPTED,
        )

    def heartbeat(
        self,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OperationResult:
        """Validate a transport heartbeat without renewing the reservation."""

        return self._authorized_operation(
            kind=OperationKind.HEARTBEAT,
            resident_id=resident_id,
            owner_id=owner_id,
            capability=capability,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            renew_timestamp=False,
            outcome_on_success=Outcome.VALIDATED,
        )

    def reattach(
        self,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OperationResult:
        """Validate capability reattachment without renewing or releasing."""

        return self._authorized_operation(
            kind=OperationKind.REATTACH,
            resident_id=resident_id,
            owner_id=owner_id,
            capability=capability,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            renew_timestamp=False,
            outcome_on_success=Outcome.ATTACHED,
        )

    def goodbye(
        self,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        idempotency_key: str,
    ) -> OperationResult:
        """Release immediately and emit one durable metadata event."""

        self._validate_metadata(resident_id, owner_id, idempotency_key)
        self._require_expected_version(expected_version)
        fingerprint = request_fingerprint(
            OperationKind.GOODBYE.value,
            resident_id,
            owner_id,
            self._safe_verifier(capability),
            expected_version,
        )
        session, transaction = self._begin()
        try:
            with transaction:
                record = self._ensure_reservation(session, resident_id)
                previous = self._find_operation(
                    session,
                    OperationKind.GOODBYE,
                    resident_id,
                    idempotency_key,
                    fingerprint,
                )
                if previous is not None:
                    return self._operation_result(previous)

                now = ensure_utc(self._clock(session))
                expired_event = self._reconcile_expiry(session, record, now)
                if expired_event is not None:
                    operation = self._record_operation(
                        session,
                        kind=OperationKind.GOODBYE,
                        resident_id=resident_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                        event_id=expired_event.id,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        event_id=operation.event_id,
                    )

                authorization = self._authorization_outcome(
                    record, owner_id, capability, expected_version
                )
                if authorization != Outcome.ACCEPTED:
                    self._record_operation(
                        session,
                        kind=OperationKind.GOODBYE,
                        resident_id=resident_id,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                    )

                previous_owner = record.owner_id
                previous_last_message = record.last_accepted_visitor_message_at
                if previous_owner is None or previous_last_message is None:
                    raise RuntimeError("active reservation shape was invalid")
                next_version = record.ownership_version + 1
                record.state = "released"
                record.owner_id = None
                record.capability_verifier = None
                record.last_accepted_visitor_message_at = None
                record.ownership_version = next_version
                session.flush()
                event = LifecycleEvent(
                    resident_id=resident_id,
                    event_type="released",
                    owner_id=previous_owner,
                    ownership_version=next_version,
                    previous_last_accepted_visitor_message_at=previous_last_message,
                    occurred_at=now,
                )
                session.add(event)
                session.flush()
                operation = self._record_operation(
                    session,
                    kind=OperationKind.GOODBYE,
                    resident_id=resident_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    outcome=Outcome.RELEASED,
                    ownership_version=next_version,
                    owner_id=owner_id,
                    accepted_at=None,
                    event_id=event.id,
                )
                return OperationResult(
                    resident_id=resident_id,
                    outcome=Outcome.RELEASED,
                    ownership_version=next_version,
                    owner_id=owner_id,
                    event_id=operation.event_id,
                )
        finally:
            session.close()

    def apply_result(
        self,
        resident_id: str,
        owner_id: str,
        capability: str,
        expected_version: int,
        result_id: str,
        mutation: Callable[[Session, ReservationSnapshot], object],
    ) -> OperationResult:
        """Authorize and apply a synthetic result in one storage transaction.

        ``mutation`` receives the same SQLAlchemy session whose locked
        reservation row was checked.  It may insert/update a metadata-only
        integration target, but must not call commit/rollback.  A failed
        callback rolls back both the operation record and target mutation.
        """

        self._validate_metadata(resident_id, owner_id, result_id)
        self._require_expected_version(expected_version)
        fingerprint = request_fingerprint(
            OperationKind.RESULT.value,
            resident_id,
            owner_id,
            self._safe_verifier(capability),
            expected_version,
            result_id,
        )
        session, transaction = self._begin()
        try:
            with transaction:
                record = self._ensure_reservation(session, resident_id)
                previous = self._find_operation(
                    session, OperationKind.RESULT, resident_id, result_id, fingerprint
                )
                if previous is not None:
                    return self._operation_result(previous)

                now = ensure_utc(self._clock(session))
                expired_event = self._reconcile_expiry(session, record, now)
                if expired_event is not None:
                    operation = self._record_operation(
                        session,
                        kind=OperationKind.RESULT,
                        resident_id=resident_id,
                        idempotency_key=result_id,
                        fingerprint=fingerprint,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                        event_id=expired_event.id,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        event_id=operation.event_id,
                    )

                authorization = self._authorization_outcome(
                    record, owner_id, capability, expected_version
                )
                if authorization != Outcome.ACCEPTED:
                    self._record_operation(
                        session,
                        kind=OperationKind.RESULT,
                        resident_id=resident_id,
                        idempotency_key=result_id,
                        fingerprint=fingerprint,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=authorization,
                        ownership_version=record.ownership_version,
                    )

                mutation_savepoint = session.begin_nested()
                try:
                    mutation(session, snapshot_from_record(record))
                    # Flush while the savepoint is active so ORM target writes
                    # are undone together with direct SQL writes below.
                    session.flush()
                except BaseException:
                    mutation_savepoint.rollback()
                    raise

                # The callback may have waited or performed bounded database
                # work. Re-sample after it returns so a result that crosses
                # the inclusive idle boundary cannot commit its target.
                completed_at = ensure_utc(self._clock(session))
                if snapshot_from_record(record).is_expired(completed_at):
                    mutation_savepoint.rollback()
                    session.refresh(record)
                    expired_event = self._reconcile_expiry(session, record, completed_at)
                    if expired_event is None:
                        raise RuntimeError("result expiry recheck lost the active reservation")
                    operation = self._record_operation(
                        session,
                        kind=OperationKind.RESULT,
                        resident_id=resident_id,
                        idempotency_key=result_id,
                        fingerprint=fingerprint,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        owner_id=None,
                        accepted_at=None,
                        event_id=expired_event.id,
                    )
                    return OperationResult(
                        resident_id=resident_id,
                        outcome=Outcome.EXPIRED,
                        ownership_version=record.ownership_version,
                        event_id=operation.event_id,
                    )

                mutation_savepoint.commit()
                self._record_operation(
                    session,
                    kind=OperationKind.RESULT,
                    resident_id=resident_id,
                    idempotency_key=result_id,
                    fingerprint=fingerprint,
                    outcome=Outcome.APPLIED,
                    ownership_version=record.ownership_version,
                    owner_id=owner_id,
                    accepted_at=None,
                )
                return OperationResult(
                    resident_id=resident_id,
                    outcome=Outcome.APPLIED,
                    ownership_version=record.ownership_version,
                    owner_id=owner_id,
                )
        finally:
            session.close()

    def snapshot(self, resident_id: str) -> ReservationSnapshot | None:
        self._validate_metadata(resident_id)
        session = self._session_factory()
        try:
            record = session.execute(
                select(ReservationRecord).where(ReservationRecord.resident_id == resident_id)
            ).scalar_one_or_none()
            return None if record is None else snapshot_from_record(record)
        finally:
            session.close()

    def events(self, resident_id: str) -> list[LifecycleEvent]:
        self._validate_metadata(resident_id)
        session = self._session_factory()
        try:
            return list(
                session.execute(
                    select(LifecycleEvent)
                    .where(LifecycleEvent.resident_id == resident_id)
                    .order_by(LifecycleEvent.id)
                ).scalars()
            )
        finally:
            session.close()
