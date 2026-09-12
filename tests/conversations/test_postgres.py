from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import multiprocessing
import os
from threading import Event
import time

import pytest
from sqlalchemy import create_engine, text

from conversations.domain import IdempotencyConflict, Outcome
from conversations.postgres import PostgresReservationStore


def claim_in_process(url: str, owner: str, barrier, output) -> None:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        barrier.wait(timeout=10)
        result = PostgresReservationStore(engine).claim(
            "resident-process-contention", owner, f"claim-{owner}"
        )
        output.put(result.outcome.value)
    finally:
        engine.dispose()


def claim(store, resident: str = "resident-1", owner: str = "visitor-1", key: str = "claim-1"):
    result = store.claim(resident, owner, key)
    assert result.outcome is Outcome.CLAIMED
    assert result.capability
    return result


def target_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM conversation_test_result_targets")
        ).scalar_one()


def test_simultaneous_independent_process_claims_have_one_winner(
    postgres_engine, store
):
    del store  # The fixture truncates the explicitly supplied disposable database.
    url = os.environ["LEO173_TEST_DATABASE_URL"]
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    output = context.Queue()
    processes = [
        context.Process(
            target=claim_in_process,
            args=(url, owner, barrier, output),
        )
        for owner in ("visitor-a", "visitor-b")
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert results.count(Outcome.CLAIMED.value) == 1
    assert results.count(Outcome.BUSY.value) == 1
    snapshot = PostgresReservationStore(
        postgres_engine
    ).snapshot("resident-process-contention")
    assert snapshot is not None
    assert snapshot.state == "active"
    assert snapshot.ownership_version == 1


def test_restart_reopen_preserves_timestamp_and_fence_at_boundary(store, reservation_clock):
    first = claim(store)
    before_reopen = store.snapshot("resident-1")
    assert before_reopen is not None

    reopened = PostgresReservationStore(
        session_factory=store._session_factory,
        clock=lambda _session: reservation_clock.now(),
    )
    assert reopened.snapshot("resident-1") == before_reopen

    reservation_clock.advance(timedelta(seconds=299.999))
    heartbeat = reopened.heartbeat(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "heartbeat-1"
    )
    assert heartbeat.outcome is Outcome.VALIDATED
    assert reopened.snapshot("resident-1").last_accepted_visitor_message_at == (
        before_reopen.last_accepted_visitor_message_at
    )

    reservation_clock.advance(timedelta(seconds=0.001))
    expired = reopened.renew(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "renew-1"
    )
    assert expired.outcome is Outcome.EXPIRED
    assert len(reopened.events("resident-1")) == 1


def test_duplicate_rejected_heartbeat_reattach_and_model_do_not_renew(
    store, reservation_clock, postgres_engine
):
    first = claim(store)
    original_timestamp = store.snapshot("resident-1").last_accepted_visitor_message_at
    reservation_clock.advance(timedelta(seconds=10))

    duplicate = store.renew(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "renew-1"
    )
    assert duplicate.outcome is Outcome.ACCEPTED
    renewed_at = store.snapshot("resident-1").last_accepted_visitor_message_at
    assert renewed_at != original_timestamp

    duplicate_retry = store.renew(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "renew-1"
    )
    assert duplicate_retry.outcome is Outcome.ACCEPTED
    assert duplicate_retry.replayed
    assert store.snapshot("resident-1").last_accepted_visitor_message_at == renewed_at

    wrong = store.renew(
        "resident-1", "visitor-1", "forged-capability", first.ownership_version, "renew-wrong"
    )
    assert wrong.outcome is Outcome.INVALID_CAPABILITY
    assert store.snapshot("resident-1").last_accepted_visitor_message_at == renewed_at

    heartbeat = store.heartbeat(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "heartbeat-1"
    )
    reattached = store.reattach(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "reattach-1"
    )
    assert heartbeat.outcome is Outcome.VALIDATED
    assert reattached.outcome is Outcome.ATTACHED
    assert store.snapshot("resident-1").last_accepted_visitor_message_at == renewed_at

    applied = store.apply_result(
        "resident-1",
        "visitor-1",
        first.capability,
        first.ownership_version,
        "model-result-1",
        lambda session, snapshot: session.execute(
            text(
                "INSERT INTO conversation_test_result_targets "
                "(result_id, resident_id, ownership_version, applied_at) "
                "VALUES (:result_id, :resident_id, :version, :applied_at)"
            ),
            {
                "result_id": "model-result-1",
                "resident_id": snapshot.resident_id,
                "version": snapshot.ownership_version,
                "applied_at": renewed_at,
            },
        ),
    )
    assert applied.outcome is Outcome.APPLIED
    assert store.snapshot("resident-1").last_accepted_visitor_message_at == renewed_at
    assert target_count(postgres_engine) == 1


def test_goodbye_is_idempotent_and_fence_survives_reclaim(store):
    first = claim(store)
    released = store.goodbye(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "goodbye-1"
    )
    assert released.outcome is Outcome.RELEASED
    assert len(store.events("resident-1")) == 1

    retry = store.goodbye(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "goodbye-1"
    )
    assert retry.outcome is Outcome.RELEASED
    assert retry.replayed
    assert retry.event_id == released.event_id
    assert len(store.events("resident-1")) == 1

    stale = store.goodbye(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "goodbye-2"
    )
    assert stale.outcome is Outcome.FENCED
    second = claim(store, owner="visitor-2", key="claim-2")
    assert second.ownership_version == first.ownership_version + 2
    assert len(store.events("resident-1")) == 1

    old_claim_retry = store.claim("resident-1", "visitor-1", "claim-1")
    assert old_claim_retry.outcome is Outcome.CLAIMED
    assert old_claim_retry.replayed
    assert old_claim_retry.capability is None
    current = store.snapshot("resident-1")
    assert current.owner_id == "visitor-2"
    assert current.ownership_version == second.ownership_version


def test_expiry_reconciles_once_and_another_resident_remains_claimable(
    store, reservation_clock
):
    first = claim(store)
    other = claim(store, resident="resident-2", owner="visitor-2", key="claim-other")
    assert other.outcome is Outcome.CLAIMED

    reservation_clock.advance(timedelta(seconds=300))
    expired = store.heartbeat(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "heartbeat-expired"
    )
    assert expired.outcome is Outcome.EXPIRED
    assert store.snapshot("resident-1").state == "released"
    assert len(store.events("resident-1")) == 1

    late = store.renew(
        "resident-1", "visitor-1", first.capability, first.ownership_version, "renew-late"
    )
    assert late.outcome is Outcome.FENCED
    assert len(store.events("resident-1")) == 1
    assert store.snapshot("resident-2").state == "active"


def test_expiry_then_reclaim_fences_late_result(store, reservation_clock, postgres_engine):
    first = claim(store)
    reservation_clock.advance(timedelta(seconds=300))
    called = False

    def late_mutation(session, snapshot):
        nonlocal called
        called = True

    expired = store.apply_result(
        "resident-1",
        "visitor-1",
        first.capability,
        first.ownership_version,
        "expired-result",
        late_mutation,
    )
    assert expired.outcome is Outcome.EXPIRED
    assert not called
    assert len(store.events("resident-1")) == 1

    second = claim(store, owner="visitor-2", key="claim-2")
    stale = store.apply_result(
        "resident-1",
        "visitor-1",
        first.capability,
        first.ownership_version,
        "late-after-reclaim",
        late_mutation,
    )
    assert stale.outcome is Outcome.FENCED
    assert not called
    assert second.ownership_version == first.ownership_version + 2
    assert target_count(postgres_engine) == 0


def test_cross_resident_capability_and_idempotency_reuse_are_rejected(store):
    first = claim(store)
    assert claim(store, resident="resident-2", owner="visitor-2", key="claim-2").outcome is Outcome.CLAIMED

    cross = store.heartbeat(
        "resident-2", "visitor-1", first.capability, 1, "cross-heartbeat"
    )
    assert cross.outcome is Outcome.INVALID_CAPABILITY

    with pytest.raises(IdempotencyConflict):
        store.claim("resident-1", "different-owner", "claim-1")


def test_result_guard_serializes_with_goodbye_and_rollback_is_atomic(
    store, postgres_engine
):
    first = claim(store)
    callback_started = Event()
    allow_callback_commit = Event()

    def blocked_mutation(session, snapshot):
        callback_started.set()
        assert allow_callback_commit.wait(timeout=5)
        session.execute(
            text(
                "INSERT INTO conversation_test_result_targets "
                "(result_id, resident_id, ownership_version, applied_at) "
                "VALUES ('race-result', :resident_id, :version, now())"
            ),
            {"resident_id": snapshot.resident_id, "version": snapshot.ownership_version},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        result_future = executor.submit(
            store.apply_result,
            "resident-1",
            "visitor-1",
            first.capability,
            first.ownership_version,
            "race-result",
            blocked_mutation,
        )
        assert callback_started.wait(timeout=5)
        goodbye_future = executor.submit(
            store.goodbye,
            "resident-1",
            "visitor-1",
            first.capability,
            first.ownership_version,
            "goodbye-after-result",
        )
        time.sleep(0.1)
        assert not goodbye_future.done(), "goodbye must wait for the guarded result transaction"
        allow_callback_commit.set()
        result = result_future.result(timeout=5)
        goodbye = goodbye_future.result(timeout=5)

    assert result.outcome is Outcome.APPLIED
    assert goodbye.outcome is Outcome.RELEASED
    assert target_count(postgres_engine) == 1

    stale_called = False

    def stale_mutation(session, snapshot):
        nonlocal stale_called
        stale_called = True

    stale = store.apply_result(
        "resident-1",
        "visitor-1",
        first.capability,
        first.ownership_version,
        "late-result",
        stale_mutation,
    )
    assert stale.outcome is Outcome.FENCED
    assert not stale_called

    def failing_mutation(session, snapshot):
        session.execute(
            text(
                "INSERT INTO conversation_test_result_targets "
                "(result_id, resident_id, ownership_version, applied_at) "
                "VALUES ('rolled-back-result', :resident_id, :version, now())"
            ),
            {"resident_id": snapshot.resident_id, "version": snapshot.ownership_version},
        )
        raise RuntimeError("synthetic mutation failure")

    # The old capability is fenced, so use a fresh claim to exercise callback rollback.
    fresh = claim(store, owner="visitor-2", key="claim-2")
    with pytest.raises(RuntimeError, match="synthetic mutation failure"):
        store.apply_result(
            "resident-1",
            "visitor-2",
            fresh.capability,
            fresh.ownership_version,
            "rolled-back-result",
            failing_mutation,
        )
    assert target_count(postgres_engine) == 1
    assert len(store.events("resident-1")) == 1
