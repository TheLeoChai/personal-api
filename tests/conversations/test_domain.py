from datetime import datetime, timedelta, timezone

from conversations.domain import (
    IDLE_TIMEOUT,
    ManualClock,
    ReservationSnapshot,
    capability_verifier,
    new_capability,
)


START = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def active_snapshot(at: datetime) -> ReservationSnapshot:
    return ReservationSnapshot(
        resident_id="resident-synthetic-1",
        state="active",
        owner_id="visitor-synthetic-1",
        ownership_version=1,
        last_accepted_visitor_message_at=at,
    )


def test_idle_expiry_has_an_inclusive_300_second_boundary() -> None:
    reservation = active_snapshot(START)

    assert not reservation.is_expired(START + timedelta(seconds=299.999))
    assert reservation.is_expired(START + IDLE_TIMEOUT)


def test_manual_clock_is_stable_for_boundary_tests() -> None:
    clock = ManualClock(START)
    reservation = active_snapshot(clock.now())

    clock.advance(timedelta(seconds=299.999))
    assert not reservation.is_expired(clock.now())
    clock.advance(timedelta(microseconds=1000))
    assert reservation.is_expired(clock.now())


def test_capability_verifier_does_not_equal_or_reveal_the_token() -> None:
    token = new_capability()

    assert token
    assert capability_verifier(token) != token
    assert capability_verifier(token) == capability_verifier(token)
