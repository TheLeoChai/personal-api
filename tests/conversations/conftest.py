from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from conversations.domain import ManualClock
from conversations.postgres import PostgresReservationStore


START = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
MIGRATION = Path(__file__).parents[2] / "migrations" / "0002_conversation_reservations.sql"


@pytest.fixture(scope="session")
def postgres_engine():
    """Use only an explicitly supplied disposable test database."""

    url = os.environ.get("LEO173_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "LEO173_TEST_DATABASE_URL is not set; real PostgreSQL evidence is unavailable"
        )
    if not url.startswith("postgresql+psycopg://"):
        pytest.fail("LEO173_TEST_DATABASE_URL must explicitly use postgresql+psycopg://")

    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TABLE IF EXISTS conversation_test_result_targets, "
            "conversation_reservation_operations, conversation_reservation_events, "
            "conversation_reservations CASCADE"
        )
        connection.exec_driver_sql(MIGRATION.read_text())
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS conversation_test_result_targets (
                result_id text PRIMARY KEY,
                resident_id text NOT NULL,
                ownership_version bigint NOT NULL,
                applied_at timestamp with time zone NOT NULL
            )
            """
        )
    yield engine
    engine.dispose()


@pytest.fixture
def reservation_clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def store(postgres_engine, reservation_clock) -> PostgresReservationStore:
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE conversation_reservation_operations, "
            "conversation_reservation_events, conversation_reservations, "
            "conversation_test_result_targets RESTART IDENTITY CASCADE"
        )
    return PostgresReservationStore(
        postgres_engine,
        clock=lambda _session: reservation_clock.now(),
    )
