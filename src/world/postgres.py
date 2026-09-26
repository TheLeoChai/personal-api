"""Synchronous PostgreSQL persistence for one synthetic offline world.

Contract
--------
``world_states`` holds each world's version-zero initial state (strict JSON,
decoded by ``state_from_mapping``) and its durable head: version and state
digest.  ``world_events`` holds the ordered accepted events.
``world_operations`` holds one idempotent outcome per submit key.

* ``create`` inserts the world with ``ON CONFLICT DO NOTHING``.  Repeating it
  with the same initial state is a no-op; a different initial state raises
  ``WorldConflict``.  Concurrent first creates resolve on the primary key.
* ``submit`` runs in one transaction.  It locks the world row, checks the
  idempotency key's fingerprint, rebuilds the head by verified replay, and
  asks the existing ``apply_proposal`` engine for the decision.  An accepted
  proposal writes the event, the new head, and the operation together.  A
  rejection writes only the operation and changes no state, version, or
  event.  The replay is bounded by ``MAX_WORLD_VERSION`` small events and
  means a submit never builds on a head its log cannot reproduce.
  Retrying a key with the same proposal returns the recorded outcome.
  Reusing a key with a different proposal raises ``WorldConflict``.
* ``load`` replays the stored events from the initial state, verifying every
  digest and the event order, and checks that the result equals the
  persisted head.

Trusted boundary: ``submit`` accepts only an in-process ``ActionProposal``
built by ``run_offline_turn`` from a settled adapter result.  It never parses
proposals from bytes or requests.  The engine still re-checks provenance,
version, actor, capability, target, and preconditions against the stored
state.  Because each proposal is bound to the version it was based on, one
fake inference result can advance the world at most once, whatever keys a
caller uses.  A proposal carries no world id, so pairing it with the right
world is the caller's job.

This module creates no engine from the environment and never imports the
application's ``db`` module; a caller injects an explicit engine.  It takes
no callbacks, stores no visitor or reply text, and does not simulate
elapsed world time or downtime catch-up, which remain undecided.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .offline_loop import (
    MAX_IDENTIFIER_LENGTH,
    MAX_WORLD_VERSION,
    ActionProposal,
    InferenceMode,
    Provenance,
    RejectionReason,
    ReplayError,
    WorldError,
    WorldEvent,
    WorldState,
    apply_proposal,
    replay,
    state_from_mapping,
)


MAX_IDEMPOTENCY_KEY_LENGTH = 255
_SHA256 = re.compile(r"[0-9a-f]{64}")


class WorldNotFound(WorldError):
    """No world with that id has been created."""


class WorldConflict(WorldError):
    """A create or idempotency key was reused with different input."""


class Base(DeclarativeBase):
    pass


class WorldRecord(Base):
    __tablename__ = "world_states"
    __table_args__ = (
        PrimaryKeyConstraint("world_id", name="world_states_pkey"),
        CheckConstraint(
            f"char_length(world_id) BETWEEN 1 AND {MAX_IDENTIFIER_LENGTH}",
            name="world_state_id_ck",
        ),
        CheckConstraint(
            "jsonb_typeof(initial_state) = 'object'", name="world_state_initial_ck"
        ),
        CheckConstraint(
            f"head_version BETWEEN 0 AND {MAX_WORLD_VERSION}",
            name="world_state_head_version_ck",
        ),
        CheckConstraint(
            "initial_sha256 ~ '^[0-9a-f]{64}$' AND head_sha256 ~ '^[0-9a-f]{64}$'",
            name="world_state_sha256_ck",
        ),
    )

    world_id: Mapped[str] = mapped_column(Text)
    initial_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    initial_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    head_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorldEventRecord(Base):
    __tablename__ = "world_events"
    __table_args__ = (
        PrimaryKeyConstraint("world_id", "version", name="world_events_pkey"),
        ForeignKeyConstraint(
            ["world_id"], ["world_states.world_id"], name="world_event_world_fk"
        ),
        CheckConstraint(
            f"version BETWEEN 1 AND {MAX_WORLD_VERSION} AND prior_version = version - 1",
            name="world_event_version_ck",
        ),
        CheckConstraint("event_id = 'world-event-' || version", name="world_event_id_ck"),
        CheckConstraint(
            f"inference_mode = '{InferenceMode.OFFLINE_FAKE_FIXTURE.value}'",
            name="world_event_mode_ck",
        ),
        CheckConstraint(
            "context_sha256 ~ '^[0-9a-f]{64}$' AND state_sha256 ~ '^[0-9a-f]{64}$'",
            name="world_event_sha256_ck",
        ),
    )

    world_id: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(BigInteger)
    prior_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    inference_mode: Mapped[str] = mapped_column(Text, nullable=False)
    context_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    state_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorldOperationRecord(Base):
    __tablename__ = "world_operations"
    __table_args__ = (
        PrimaryKeyConstraint("world_id", "idempotency_key", name="world_operations_pkey"),
        ForeignKeyConstraint(
            ["world_id"], ["world_states.world_id"], name="world_operation_world_fk"
        ),
        ForeignKeyConstraint(
            ["world_id", "event_version"],
            ["world_events.world_id", "world_events.version"],
            name="world_operation_event_fk",
        ),
        UniqueConstraint("world_id", "event_version", name="world_operation_event_uq"),
        CheckConstraint(
            f"char_length(idempotency_key) BETWEEN 1 AND {MAX_IDEMPOTENCY_KEY_LENGTH}",
            name="world_operation_key_ck",
        ),
        CheckConstraint(
            "(outcome = 'accepted' AND reason IS NULL"
            " AND event_version IS NOT NULL AND event_version = version)"
            " OR (outcome = 'rejected' AND reason IS NOT NULL AND event_version IS NULL)",
            name="world_operation_shape_ck",
        ),
    )

    world_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    event_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@dataclass(frozen=True)
class CreateResult:
    world_id: str
    created: bool
    initial_sha256: str


@dataclass(frozen=True)
class SubmitResult:
    """The recorded outcome of one submit key.

    ``version`` and ``state_sha256`` describe the head right after this
    operation, so a replayed rejection reports the head at the time of the
    original rejection, not the current one.
    """

    world_id: str
    accepted: bool
    reason: RejectionReason | None
    version: int
    state_sha256: str
    event: WorldEvent | None
    replayed: bool = False


@dataclass(frozen=True)
class LoadedWorld:
    world_id: str
    initial: WorldState
    state: WorldState
    events: tuple[WorldEvent, ...]


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise WorldError(f"{field_name} must be a non-empty string of at most {maximum} characters")
    return value


def proposal_fingerprint(world_id: str, proposal: ActionProposal) -> str:
    """Digest every proposal field the engine reads, bound to the world id.

    Only type shape is checked here; any semantic problem becomes a recorded
    engine rejection.  ASCII-escaped JSON keeps odd model strings hashable.
    """

    if not isinstance(proposal, ActionProposal) or not isinstance(
        proposal.provenance, Provenance
    ):
        raise WorldError("proposal must be an ActionProposal with Provenance")
    provenance = proposal.provenance
    if (
        not isinstance(proposal.actor_id, str)
        or not isinstance(proposal.action, str)
        or not (proposal.target is None or isinstance(proposal.target, str))
        or isinstance(proposal.based_on_version, bool)
        or not isinstance(proposal.based_on_version, int)
        or not isinstance(provenance.inference_mode, InferenceMode)
        or not isinstance(provenance.context_sha256, str)
        or not isinstance(provenance.accounting_state, str)
    ):
        raise WorldError("proposal fields have the wrong types")
    payload = {
        "accounting_state": provenance.accounting_state,
        "action": proposal.action,
        "actor_id": proposal.actor_id,
        "based_on_version": proposal.based_on_version,
        "context_sha256": provenance.context_sha256,
        "inference_mode": provenance.inference_mode.value,
        "target": proposal.target,
        "world_id": world_id,
    }
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def event_from_record(record: WorldEventRecord) -> WorldEvent:
    """Decode one stored event row; ``replay`` then checks order and digest."""

    try:
        mode = InferenceMode(record.inference_mode)
    except ValueError as exc:
        raise ReplayError("stored event has an unknown inference mode") from exc
    if not all(
        isinstance(value, str) and _SHA256.fullmatch(value)
        for value in (record.context_sha256, record.state_sha256)
    ):
        raise ReplayError("stored event digest is malformed")
    return WorldEvent(
        event_id=record.event_id,
        version=record.version,
        prior_version=record.prior_version,
        actor_id=record.actor_id,
        action=record.action,
        target=record.target,
        inference_mode=mode,
        context_sha256=record.context_sha256,
        state_sha256=record.state_sha256,
    )


class PostgresWorldStore:
    """Durable single-resident world store over an injected engine."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("engine must be an explicitly configured SQLAlchemy Engine")
        self._sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def create(self, world_id: str, initial: WorldState) -> CreateResult:
        world_id = _bounded_text(world_id, "world_id", MAX_IDENTIFIER_LENGTH)
        if not isinstance(initial, WorldState) or initial.version != 0:
            raise WorldError("a world must be created from a version-zero WorldState")
        if "\x00" in initial.resident.resident_id:
            raise WorldError("resident_id cannot be stored")
        digest = initial.digest
        with self._sessions() as session, session.begin():
            inserted = session.execute(
                pg_insert(WorldRecord)
                .values(
                    world_id=world_id,
                    initial_state=initial.as_mapping(),
                    initial_sha256=digest,
                    head_version=0,
                    head_sha256=digest,
                )
                .on_conflict_do_nothing(index_elements=[WorldRecord.world_id])
                .returning(WorldRecord.world_id)
            ).scalar_one_or_none()
            if inserted is None:
                # The conflicting insert has committed by now, so this new
                # statement sees it.
                existing = session.execute(
                    select(WorldRecord.initial_sha256).where(WorldRecord.world_id == world_id)
                ).scalar_one()
                if existing != digest:
                    raise WorldConflict("world already exists with a different initial state")
        return CreateResult(world_id=world_id, created=inserted is not None, initial_sha256=digest)

    def submit(
        self, world_id: str, proposal: ActionProposal, idempotency_key: str
    ) -> SubmitResult:
        world_id = _bounded_text(world_id, "world_id", MAX_IDENTIFIER_LENGTH)
        idempotency_key = _bounded_text(
            idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH
        )
        fingerprint = proposal_fingerprint(world_id, proposal)
        with self._sessions() as session, session.begin():
            record = self._world(session, world_id, lock="update")
            previous = session.execute(
                select(WorldOperationRecord).where(
                    WorldOperationRecord.world_id == world_id,
                    WorldOperationRecord.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if previous is not None:
                if previous.request_fingerprint != fingerprint:
                    raise WorldConflict("idempotency key was reused with a different proposal")
                return self._recorded_result(session, previous)

            _, state, _ = self._verified(session, record)
            decision = apply_proposal(state, proposal)
            event = decision.event
            if decision.accepted:
                if event is None:
                    raise RuntimeError("accepted decision carried no event")
                session.add(
                    WorldEventRecord(
                        world_id=world_id,
                        version=event.version,
                        prior_version=event.prior_version,
                        event_id=event.event_id,
                        actor_id=event.actor_id,
                        action=event.action,
                        target=event.target,
                        inference_mode=event.inference_mode.value,
                        context_sha256=event.context_sha256,
                        state_sha256=event.state_sha256,
                    )
                )
                session.flush()
                record.head_version = decision.state.version
                record.head_sha256 = decision.state.digest
            self._insert_operation(
                session,
                WorldOperationRecord(
                    world_id=world_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    outcome="accepted" if decision.accepted else "rejected",
                    reason=None if decision.reason is None else decision.reason.value,
                    version=decision.state.version,
                    state_sha256=decision.state.digest,
                    event_version=None if event is None else event.version,
                ),
            )
            return SubmitResult(
                world_id=world_id,
                accepted=decision.accepted,
                reason=decision.reason,
                version=decision.state.version,
                state_sha256=decision.state.digest,
                event=event,
            )

    def load(self, world_id: str) -> LoadedWorld:
        """Rebuild the world by verified replay and confirm the stored head."""

        world_id = _bounded_text(world_id, "world_id", MAX_IDENTIFIER_LENGTH)
        with self._sessions() as session, session.begin():
            # FOR SHARE keeps a concurrent submit from committing between the
            # head read and the event read.
            record = self._world(session, world_id, lock="share")
            initial, state, events = self._verified(session, record)
        return LoadedWorld(world_id=world_id, initial=initial, state=state, events=events)

    @staticmethod
    def _world(session: Session, world_id: str, *, lock: str) -> WorldRecord:
        record = session.execute(
            select(WorldRecord)
            .where(WorldRecord.world_id == world_id)
            .with_for_update(read=lock == "share")
        ).scalar_one_or_none()
        if record is None:
            raise WorldNotFound(f"world {world_id!r} does not exist")
        return record

    @staticmethod
    def _insert_operation(session: Session, operation: WorldOperationRecord) -> None:
        session.add(operation)
        session.flush()

    @staticmethod
    def _verified(
        session: Session, record: WorldRecord
    ) -> tuple[WorldState, WorldState, tuple[WorldEvent, ...]]:
        try:
            initial = state_from_mapping(record.initial_state)
        except WorldError as exc:
            raise ReplayError("stored initial state is malformed") from exc
        if initial.version != 0 or initial.digest != record.initial_sha256:
            raise ReplayError("stored initial state does not match its digest")
        rows = session.execute(
            select(WorldEventRecord)
            .where(WorldEventRecord.world_id == record.world_id)
            .order_by(WorldEventRecord.version)
        ).scalars()
        events = tuple(event_from_record(row) for row in rows)
        state = replay(initial, events)
        if state.version != record.head_version or state.digest != record.head_sha256:
            raise ReplayError("persisted head does not match its event log")
        return initial, state, events

    @staticmethod
    def _recorded_result(session: Session, operation: WorldOperationRecord) -> SubmitResult:
        event = None
        reason = None
        if operation.outcome == "accepted":
            row = session.get(WorldEventRecord, (operation.world_id, operation.event_version))
            if row is None:
                raise ReplayError("recorded operation lost its event")
            event = event_from_record(row)
        else:
            try:
                reason = RejectionReason(operation.reason)
            except ValueError as exc:
                raise ReplayError("recorded operation has an unknown reason") from exc
        return SubmitResult(
            world_id=operation.world_id,
            accepted=operation.outcome == "accepted",
            reason=reason,
            version=operation.version,
            state_sha256=operation.state_sha256,
            event=event,
            replayed=True,
        )
