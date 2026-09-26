"""Tests for durable one-resident world persistence (LEO-186).

The pure tests always run.  The PostgreSQL tests run only against an
explicitly supplied disposable database in LEO186_TEST_DATABASE_URL and skip
otherwise.  ``disposable_url`` refuses, before any connection, migration, or
DROP, anything but a loopback ``postgresql+psycopg`` URL on an explicit
non-5432 port, with no query overrides and a ``leo186_test_<suffix>``
database.  No SQLite or other stand-in is used.

Every proposal is built by ``run_offline_turn`` from a FAKE fixture reply over
an in-memory transport with a synthetic permit.  There are no sockets,
providers, credentials, model calls, or public access.
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

from sqlalchemy import CheckConstraint, create_engine, func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError

from inference.openrouter import PermitReservation
from personas import (
    ApprovedPersonaRegistry,
    ContextBudgets,
    SyntheticTestCounter,
    SyntheticVisibilityPolicy,
    Visibility,
    VisibilityGrant,
)
from world import (
    ELSEWHERE,
    HERB_BED,
    WELL,
    ActionProposal,
    InferenceMode,
    Provenance,
    RejectionReason,
    ReplayError,
    WorldError,
    WorldState,
    apply_proposal,
    initial_state,
    replay,
    run_offline_turn,
)
from world.offline_loop import SCHEMA
from world.postgres import (
    Base,
    PostgresWorldStore,
    WorldConflict,
    WorldEventRecord,
    WorldNotFound,
    WorldOperationRecord,
    WorldRecord,
    event_from_record,
    event_to_record,
    proposal_fingerprint,
    recorded_result,
    verify_world,
)


URL_ENV = "LEO186_TEST_DATABASE_URL"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_POSTGRES_PORT = 5432
TEST_DATABASE_NAME = re.compile(r"leo186_test_[a-z0-9_]{1,40}")
ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations" / "0003_world_state.sql"
TABLES = ("world_states", "world_events", "world_operations")
ORM_SCHEMA = "leo186_orm_parity"
RECIPIENT = "offline-fake-model"
VISITOR_TEXT = "synthetic visitor prompt leo186"
SECRET_REPLY = "synthetic reply that must never be stored leo186"


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def close(self) -> None:
        pass


class _FixtureTransport:
    """Offline fake transport replaying one FAKE model choice."""

    def __init__(self, action: str, target: str | None, reply: str) -> None:
        content = json.dumps({"reply": reply, "action": action, "target": target})
        self.body = json.dumps(
            {
                "id": "gen-offline-fake",
                "object": "chat.completion",
                "created": 1,
                "model": "offline/fake-fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode("utf-8")

    def send(self, request, *, timeout: float):
        return _Response(self.body)


class _SyntheticPermit:
    """Synthetic permit; not real quota enforcement."""

    def reserve(self, request):
        return PermitReservation(f"synthetic-{request.request_id}")

    def settle(self, reservation, *, outcome, sent_state):
        return True


def adapter_proposal(
    state: WorldState, action: str, target: str | None, *, reply: str = "synthetic reply"
) -> ActionProposal:
    """A proposal from the trusted offline adapter path, as ``submit`` expects."""

    resident_id = state.resident.resident_id
    result = run_offline_turn(
        state,
        ApprovedPersonaRegistry.default(resident_id=resident_id),
        recipient_id=RECIPIENT,
        visibility_policy=SyntheticVisibilityPolicy.for_tests(
            VisibilityGrant(RECIPIENT, resident_id, Visibility.RESIDENT),
            VisibilityGrant(RECIPIENT, resident_id, Visibility.PRIVATE),
        ),
        budgets=ContextBudgets(high=100_000, medium=100_000, immediate=100_000, total=300_000),
        counter=SyntheticTestCounter(),
        visitor_text=VISITOR_TEXT,
        transport=_FixtureTransport(action, target, reply),
        permit=_SyntheticPermit(),
        credential_supplier=lambda: "fake-offline-credential-leo186",
        clock=lambda: 100.0,
    )
    if result.proposal is None:
        raise AssertionError("fixture reply did not produce a proposal")
    return result.proposal


def forged_proposal(state: WorldState, action: str, target: str | None) -> ActionProposal:
    """Looks like a proposal but never passed through the adapter."""

    return ActionProposal(
        actor_id=state.resident.resident_id,
        action=action,
        target=target,
        based_on_version=state.version,
        provenance=Provenance(
            inference_mode=InferenceMode.OFFLINE_FAKE_FIXTURE,
            context_sha256="0" * 64,
            accounting_state="settled_completed",
        ),
    )


def normalized_sql(sql: str) -> str:
    return " ".join(sql.split()).replace("( ", "(").replace(" )", ")")


def migration_check(migration: str, name: str) -> str:
    """The full expression inside ``CONSTRAINT <name> CHECK (...)``."""

    start = migration.index(f"CONSTRAINT {name} CHECK (") + len(f"CONSTRAINT {name} CHECK (")
    depth, quoted = 1, False
    for index in range(start, len(migration)):
        char = migration[index]
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                return normalized_sql(migration[start:index])
    raise AssertionError(f"unbalanced CHECK {name}")


def wrapped_in_is_true(expression: str) -> bool:
    """True when the WHOLE expression is ``(...) IS TRUE``, not one conjunct."""

    if not expression.startswith("(") or not expression.endswith(") IS TRUE"):
        return False
    body = expression[: -len(" IS TRUE")]
    depth, quoted = 0, False
    for index, char in enumerate(body):
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0 and index != len(body) - 1:
                return False
    return depth == 0


def summary(result) -> tuple:
    reason = None if result.reason is None else result.reason.value
    return (result.accepted, reason, result.version, result.replayed)


FULL_SEQUENCE = (("move", WELL), ("refill", WELL), ("move", HERB_BED), ("water", HERB_BED))


def stored_world(steps=FULL_SEQUENCE):
    """Rows exactly as ``submit`` would store them, built with no database."""

    start = initial_state()
    state = start
    events = []
    for action, target in steps:
        decision = apply_proposal(state, adapter_proposal(state, action, target))
        if not decision.accepted:
            raise AssertionError((action, target, decision.reason))
        events.append(decision.event)
        state = decision.state
    record = WorldRecord(
        world_id="w",
        state_schema=SCHEMA,
        initial_state=json.loads(json.dumps(start.as_mapping())),
        initial_sha256=start.digest,
        head_state=json.loads(json.dumps(state.as_mapping())),
        head_version=state.version,
        head_sha256=state.digest,
    )
    rows = [event_to_record("w", event) for event in events]
    return start, state, tuple(events), record, rows


def operation(**fields) -> WorldOperationRecord:
    base = dict(world_id="w", idempotency_key="k", request_fingerprint="f" * 64)
    return WorldOperationRecord(**{**base, **fields})


class VerificationTests(unittest.TestCase):
    """No database: whole-world and retry integrity checks on stored rows."""

    def setUp(self):
        self.start, self.head, self.events, self.record, self.rows = stored_world()

    def tampered(self, *, record=None, rows=None):
        stored = WorldRecord(
            **{
                column: getattr(self.record, column)
                for column in WorldRecord.__table__.columns.keys()
                if column != "created_at"
            }
        )
        for name, value in (record or {}).items():
            setattr(stored, name, value)
        return stored, self.rows if rows is None else rows

    def test_full_sequence_verifies_to_watered_bed_and_nonempty_can(self):
        initial, head, events = verify_world(self.record, self.rows)
        self.assertEqual((initial, head, events), (self.start, self.head, self.events))
        self.assertEqual(
            (head.version, head.location, head.can_level, head.herb_bed_watered),
            (4, HERB_BED, 2, True),
        )

    def test_tampered_world_is_refused(self):
        other_head = dataclasses.replace(self.head, can_level=3)
        other_start = initial_state(can_level=1)
        bad_row = dataclasses.replace(self.events[1], target=HERB_BED)
        wrong_digest = dataclasses.replace(self.events[2], state_sha256=self.events[1].state_sha256)
        bool_as_int = json.loads(json.dumps(self.record.head_state))
        bool_as_int["herb_bed"]["watered"] = 1
        cases = {
            "consistent other head snapshot": self.tampered(
                record={"head_state": other_head.as_mapping(), "head_sha256": other_head.digest}
            ),
            "head digest only": self.tampered(record={"head_sha256": other_head.digest}),
            "head version only": self.tampered(record={"head_version": 3}),
            "head int for bool": self.tampered(record={"head_state": bool_as_int}),
            "head missing": self.tampered(record={"head_state": None}),
            "consistent other initial": self.tampered(
                record={
                    "initial_state": other_start.as_mapping(),
                    "initial_sha256": other_start.digest,
                }
            ),
            "schema tag": self.tampered(record={"state_schema": "offline-world/v1"}),
            "event target": self.tampered(
                rows=[self.rows[0], event_to_record("w", bad_row), *self.rows[2:]]
            ),
            "event digest from another event": self.tampered(
                rows=[*self.rows[:2], event_to_record("w", wrong_digest), self.rows[3]]
            ),
            "dropped tail event": self.tampered(rows=self.rows[:-1]),
            "reordered events": self.tampered(
                rows=[self.rows[1], self.rows[0], *self.rows[2:]]
            ),
        }
        for name, (record, rows) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ReplayError):
                    verify_world(record, rows)

    def test_recorded_outcomes_must_match_the_verified_log(self):
        accepted = dict(
            outcome="accepted",
            reason=None,
            version=2,
            state_sha256=self.events[1].state_sha256,
            event_version=2,
        )
        result = recorded_result(operation(**accepted), self.start, self.events)
        self.assertEqual(summary(result), (True, None, 2, True))
        self.assertEqual(result.event, self.events[1])

        rejected = dict(
            outcome="rejected",
            reason="can_empty",
            version=0,
            state_sha256=self.start.digest,
            event_version=None,
        )
        self.assertEqual(
            summary(recorded_result(operation(**rejected), self.start, self.events)),
            (False, "can_empty", 0, True),
        )

        bad = {
            "accepted digest of another event": {
                **accepted, "state_sha256": self.events[2].state_sha256
            },
            "accepted beyond log": {**accepted, "version": 5, "event_version": 5},
            "accepted without event": {**accepted, "event_version": None},
            "accepted with reason": {**accepted, "reason": "can_empty"},
            "rejected digest mismatch": {
                **rejected, "state_sha256": self.events[0].state_sha256
            },
            "rejected beyond log": {**rejected, "version": 9},
            "rejected unknown reason": {**rejected, "reason": "because"},
            "rejected with event": {**rejected, "event_version": 0},
            "unknown outcome": {**rejected, "outcome": "maybe"},
            "bool version": {**rejected, "version": False},
        }
        for name, fields in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(ReplayError):
                    recorded_result(operation(**fields), self.start, self.events)


class PureTests(unittest.TestCase):
    """No database: fingerprints, decoding, validation, import hygiene."""

    def setUp(self):
        self.start = initial_state(location=WELL)
        self.proposal = adapter_proposal(self.start, "refill", WELL)
        # Engines connect lazily; any test reaching the DB here fails loudly.
        self.unreachable = PostgresWorldStore(
            create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
        )

    def test_fingerprint_binds_every_engine_input(self):
        base = proposal_fingerprint("world-a", self.proposal)
        self.assertEqual(base, proposal_fingerprint("world-a", self.proposal))
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        provenance = self.proposal.provenance
        variants = {
            "world": ("world-b", self.proposal),
            "actor": ("world-a", dataclasses.replace(self.proposal, actor_id="other")),
            "action": ("world-a", dataclasses.replace(self.proposal, action="wait")),
            "target": ("world-a", dataclasses.replace(self.proposal, target=None)),
            "version": ("world-a", dataclasses.replace(self.proposal, based_on_version=1)),
            "context": (
                "world-a",
                dataclasses.replace(
                    self.proposal,
                    provenance=dataclasses.replace(provenance, context_sha256="1" * 64),
                ),
            ),
            "accounting": (
                "world-a",
                dataclasses.replace(
                    self.proposal,
                    provenance=dataclasses.replace(provenance, accounting_state="pending"),
                ),
            ),
        }
        for name, (world_id, proposal) in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(proposal_fingerprint(world_id, proposal), base)
        # Odd model strings (lone surrogates) still fingerprint deterministically.
        odd = dataclasses.replace(self.proposal, action="\ud800", target="\x00")
        self.assertEqual(proposal_fingerprint("w", odd), proposal_fingerprint("w", odd))

    def test_fingerprint_rejects_untyped_input(self):
        cases = {
            "not a proposal": {"action": "refill"},
            "bool version": dataclasses.replace(self.proposal, based_on_version=True),
            "int action": dataclasses.replace(self.proposal, action=1),
            "list target": dataclasses.replace(self.proposal, target=[WELL]),
            "raw provenance": dataclasses.replace(self.proposal, provenance={"mode": "x"}),
            "string mode": dataclasses.replace(
                self.proposal,
                provenance=dataclasses.replace(
                    self.proposal.provenance, inference_mode="offline-fake-fixture"
                ),
            ),
        }
        for name, proposal in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(WorldError):
                    proposal_fingerprint("w", proposal)

    def test_inputs_are_validated_before_any_database_access(self):
        calls = {
            "empty world": lambda: self.unreachable.create("", self.start),
            "nul world": lambda: self.unreachable.create("w\x00", self.start),
            "long world": lambda: self.unreachable.load("w" * 129),
            "non-zero version": lambda: self.unreachable.create(
                "w", dataclasses.replace(self.start, version=1)
            ),
            "raw mapping": lambda: self.unreachable.create("w", self.start.as_mapping()),
            "nul resident": lambda: self.unreachable.create(
                "w", initial_state(resident_id="le\x00o")
            ),
            "empty key": lambda: self.unreachable.submit("w", self.proposal, ""),
            "long key": lambda: self.unreachable.submit("w", self.proposal, "k" * 256),
            "untyped proposal": lambda: self.unreachable.submit("w", {"action": "wait"}, "k"),
        }
        for name, call in calls.items():
            with self.subTest(name=name):
                with self.assertRaises(WorldError):
                    call()

    def test_store_requires_an_injected_engine(self):
        for engine in (None, "postgresql+psycopg://nobody@127.0.0.1:1/none"):
            with self.subTest(engine=engine):
                with self.assertRaises(TypeError):
                    PostgresWorldStore(engine)

    def test_event_rows_are_decoded_strictly(self):
        good = dict(
            world_id="w",
            version=1,
            prior_version=0,
            event_id="world-event-1",
            actor_id="leo",
            action="wait",
            target=None,
            inference_mode=InferenceMode.OFFLINE_FAKE_FIXTURE.value,
            context_sha256="a" * 64,
            state_sha256="b" * 64,
        )
        event = event_from_record(WorldEventRecord(**good))
        self.assertIs(event.inference_mode, InferenceMode.OFFLINE_FAKE_FIXTURE)
        for name, change in {
            "real mode": {"inference_mode": "real"},
            "short digest": {"state_sha256": "b" * 63},
            "upper digest": {"context_sha256": "A" * 64},
        }.items():
            with self.subTest(name=name):
                with self.assertRaises(ReplayError):
                    event_from_record(WorldEventRecord(**{**good, **change}))

    def test_orm_constraints_appear_in_migration(self):
        migration = MIGRATION.read_text()
        self.assertIn("Applied: pending", migration)
        for table in Base.metadata.sorted_tables:
            self.assertIn(f"CREATE TABLE public.{table.name} (", migration)
            for constraint in table.constraints:
                with self.subTest(table=table.name, constraint=constraint.name):
                    self.assertIsNotNone(constraint.name)
                    self.assertIn(f"CONSTRAINT {constraint.name} ", migration)
                    if isinstance(constraint, CheckConstraint):
                        # The whole CHECK expression matches, modulo whitespace.
                        self.assertEqual(
                            migration_check(migration, constraint.name),
                            normalized_sql(str(constraint.sqltext)),
                        )

    def test_json_snapshot_checks_fail_closed_on_null(self):
        # ``->`` yields SQL NULL for a missing key or non-object, and a CHECK
        # passes on NULL; each JSON check must be wrapped whole in IS TRUE.
        migration = MIGRATION.read_text()
        json_checks = [
            constraint
            for table in Base.metadata.sorted_tables
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and "->" in str(constraint.sqltext)
        ]
        self.assertEqual(
            sorted(constraint.name for constraint in json_checks),
            ["world_state_head_ck", "world_state_initial_ck"],
        )
        for constraint in json_checks:
            orm = normalized_sql(str(constraint.sqltext))
            for source, expression in (
                ("orm", orm),
                ("migration", migration_check(migration, constraint.name)),
            ):
                with self.subTest(constraint=constraint.name, source=source):
                    self.assertTrue(wrapped_in_is_true(expression), expression)
                    self.assertIn("jsonb_typeof(", expression)
        # The helper itself refuses a wrap around only the last conjunct.
        self.assertFalse(wrapped_in_is_true("(a -> 'x') = b AND (c = d) IS TRUE"))
        self.assertFalse(wrapped_in_is_true("a -> 'x' = b"))
        self.assertTrue(wrapped_in_is_true("(a -> 'x' = b AND (c) = d) IS TRUE"))

    def test_orm_columns_appear_in_migration(self):
        migration = MIGRATION.read_text()
        for table in Base.metadata.sorted_tables:
            block = migration.split(f"CREATE TABLE public.{table.name} (", 1)[1].split("\n);", 1)[0]
            declared = re.findall(r"^    ([a-z_0-9]+) (?!KEY)", block, re.MULTILINE)
            with self.subTest(table=table.name):
                self.assertEqual(sorted(declared), sorted(table.columns.keys()))

    def test_database_guard_refuses_unsafe_urls_without_connecting(self):
        unsafe = {
            "postgres default db": "postgresql+psycopg://u:p@127.0.0.1:55432/postgres",
            "template1": "postgresql+psycopg://u:p@127.0.0.1:55432/template1",
            "template0": "postgresql+psycopg://u:p@127.0.0.1:55432/template0",
            "app-like db": "postgresql+psycopg://u:p@127.0.0.1:55432/server",
            "bare prefix": "postgresql+psycopg://u:p@127.0.0.1:55432/leo186_test_",
            "prefix lookalike": "postgresql+psycopg://u:p@127.0.0.1:55432/leo186_testx",
            "uppercase": "postgresql+psycopg://u:p@127.0.0.1:55432/LEO186_TEST_A",
            "suffix injection": "postgresql+psycopg://u:p@127.0.0.1:55432/leo186_test_a;drop",
            "no database": "postgresql+psycopg://u:p@127.0.0.1:55432",
            "default port": "postgresql+psycopg://u:p@127.0.0.1:5432/leo186_test_a",
            "implicit port": "postgresql+psycopg://u:p@127.0.0.1/leo186_test_a",
            "docker db host": "postgresql+psycopg://u:p@db:55432/leo186_test_a",
            "lan host": "postgresql+psycopg://u:p@192.168.1.5:55432/leo186_test_a",
            "no host": "postgresql+psycopg://u:p@/leo186_test_a",
            "socket override": "postgresql+psycopg://u:p@127.0.0.1:55432/leo186_test_a?host=/run/pg",
            "options override": (
                "postgresql+psycopg://u:p@127.0.0.1:55432/leo186_test_a?options=-csearch_path%3Dx"
            ),
            "other driver": "postgresql://u:p@127.0.0.1:55432/leo186_test_a",
            "sqlite": "sqlite:///leo186_test_a",
            "garbage": "not a url",
        }
        with mock.patch(f"{__name__}.create_engine", side_effect=AssertionError("connected")):
            for name, value in unsafe.items():
                with self.subTest(name=name):
                    with self.assertRaises(UnsafeTestDatabase):
                        disposable_url(value)
            for value in (None, ""):
                with self.assertRaises(unittest.SkipTest):
                    disposable_url(value)
            accepted = disposable_url(
                "postgresql+psycopg://leo186:synthetic@127.0.0.1:55432/leo186_test_run1"
            )
        self.assertEqual(accepted.database, "leo186_test_run1")

    def test_import_creates_no_engine_or_app_database(self):
        probe = (
            "import sys, world.postgres; "
            "print(sorted(m for m in ('db', 'main', 'models', 'worker') if m in sys.modules))"
        )
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
        output = subprocess.run(
            [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
        )
        self.assertEqual(output.stdout.strip(), "[]")


class UnsafeTestDatabase(RuntimeError):
    """The configured URL is not a designated disposable LEO-186 database."""


def disposable_url(value: str | None) -> URL:
    """Accept only a designated disposable local test database, offline.

    This runs before any engine, connection, migration, or DROP.  It needs
    the psycopg driver, a loopback host, an explicit non-default port (the
    live stack's 5432 is refused), no query overrides such as ``host=`` or
    ``options=``, and a database named ``leo186_test_<suffix>``.  So
    ``postgres``, ``template*``, and any application database are refused.
    """

    if not value:
        raise unittest.SkipTest(
            f"{URL_ENV} is not set; real PostgreSQL evidence is unavailable"
        )
    try:
        url = make_url(value)
    except Exception as exc:
        raise UnsafeTestDatabase(f"{URL_ENV} is not a parseable database URL") from exc
    if url.drivername != "postgresql+psycopg":
        raise UnsafeTestDatabase(f"{URL_ENV} must use postgresql+psycopg://")
    if url.host not in LOCAL_HOSTS:
        raise UnsafeTestDatabase(f"{URL_ENV} host must be loopback")
    if url.port is None or url.port == DEFAULT_POSTGRES_PORT:
        raise UnsafeTestDatabase(f"{URL_ENV} needs an explicit non-default port")
    if url.query:
        raise UnsafeTestDatabase(f"{URL_ENV} must not carry query parameters")
    if not isinstance(url.database, str) or not TEST_DATABASE_NAME.fullmatch(url.database):
        raise UnsafeTestDatabase(f"{URL_ENV} database must match {TEST_DATABASE_NAME.pattern}")
    return url


def _run_in_child(url, barrier, output, work) -> None:
    engine = create_engine(url)
    try:
        if barrier is not None:
            barrier.wait(timeout=10)
        try:
            output.put(("ok", work(PostgresWorldStore(engine))))
        except Exception as exc:  # reported to the parent for assertion
            output.put(("error", type(exc).__name__))
    finally:
        engine.dispose()


class PostgresWorldStoreTests(unittest.TestCase):
    """Real PostgreSQL 16; skipped without LEO186_TEST_DATABASE_URL."""

    @classmethod
    def setUpClass(cls):
        # Guard first: nothing below connects, migrates, or drops until the
        # URL names a designated disposable local database.
        cls.url = disposable_url(os.environ.get(URL_ENV))
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        with cls.engine.begin() as connection:
            connected_to = connection.exec_driver_sql("SELECT current_database()").scalar_one()
            if connected_to != cls.url.database:
                cls.engine.dispose()
                raise UnsafeTestDatabase("connected database differs from the guarded name")
            connection.exec_driver_sql(
                "DROP TABLE IF EXISTS world_operations, world_events, world_states CASCADE"
            )
            connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {ORM_SCHEMA} CASCADE")
            connection.exec_driver_sql(MIGRATION.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "TRUNCATE world_operations, world_events, world_states"
            )
        self.store = PostgresWorldStore(self.engine)

    def run_processes(self, works, *, simultaneous=True) -> list:
        # Forked children inherit adapter-built proposals and use their own
        # engines; the parent pool is emptied first so no socket is shared.
        self.engine.dispose()
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(len(works)) if simultaneous else None
        output = context.Queue()
        processes = [
            context.Process(target=_run_in_child, args=(self.url, barrier, output, work))
            for work in works
        ]
        for process in processes:
            process.start()
        results = [output.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        return results

    def count(self, table: str, world_id: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE world_id = :w"), {"w": world_id}
            ).scalar_one()

    def head(self, world_id: str) -> tuple:
        """(version, digest, canonical snapshot) as stored."""

        with self.engine.connect() as connection:
            return tuple(
                connection.execute(
                    text(
                        "SELECT head_version, head_sha256, head_state "
                        "FROM world_states WHERE world_id = :w"
                    ),
                    {"w": world_id},
                ).one()
            )

    def advanced_world(self, world_id: str, steps=(("move", WELL), ("refill", WELL))):
        """Create a world and commit ``steps``; return the state and proposals."""

        state = initial_state()
        self.store.create(world_id, state)
        proposals = {}
        for step, (action, target) in enumerate(steps):
            key = f"step-{step}"
            proposals[key] = adapter_proposal(state, action, target)
            result = self.store.submit(world_id, proposals[key], key)
            self.assertTrue(result.accepted, (action, result.reason))
            state = self.store.load(world_id).state
            self.assertEqual(self.head(world_id), (state.version, state.digest, state.as_mapping()))
        return state, proposals

    def test_full_sequence_is_restored_by_a_fresh_process(self):
        committed, proposals = self.advanced_world("world-full", FULL_SEQUENCE)
        self.assertEqual(
            (committed.version, committed.location, committed.can_level, committed.herb_bed_watered),
            (4, HERB_BED, 2, True),
        )
        [(status, restored)] = self.run_processes(
            [
                lambda s: (
                    lambda w: (
                        w.state.version,
                        w.state.digest,
                        w.state.location,
                        w.state.can_level,
                        w.state.herb_bed_watered,
                        [event.event_id for event in w.events],
                    )
                )(s.load("world-full"))
            ],
            simultaneous=False,
        )
        self.assertEqual(status, "ok")
        self.assertEqual(
            restored,
            (4, committed.digest, HERB_BED, 2, True, [f"world-event-{v}" for v in range(1, 5)]),
        )
        # A retry after restart replays the recorded outcome with no new event.
        retry = self.store.submit("world-full", proposals["step-3"], "step-3")
        self.assertEqual(summary(retry), (True, None, 4, True))
        self.assertEqual(self.count("world_events", "world-full"), 4)
        self.assertEqual(self.store.load("world-full").state, committed)

    def test_create_is_idempotent_and_conflicts_on_different_input(self):
        start = initial_state()
        first = self.store.create("world-create", start)
        again = self.store.create("world-create", start)
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(first.initial_sha256, start.digest)
        with self.assertRaises(WorldConflict):
            self.store.create("world-create", initial_state(location=WELL))
        loaded = self.store.load("world-create")
        self.assertEqual((loaded.initial, loaded.state, loaded.events), (start, start, ()))
        with self.assertRaises(WorldNotFound):
            self.store.load("world-missing")
        with self.assertRaises(WorldNotFound):
            self.store.submit("world-missing", adapter_proposal(start, "wait", None), "k")

    def test_snapshot_checks_reject_missing_null_and_wrong_types(self):
        insert = text(
            "INSERT INTO world_states (world_id, state_schema, initial_state, "
            "initial_sha256, head_state, head_version, head_sha256) VALUES "
            "(:w, :schema, CAST(:initial AS jsonb), :d, CAST(:head AS jsonb), 0, :d)"
        )
        minimal = json.dumps({"schema": SCHEMA, "version": 0})

        def row(world_id, **snapshots):
            return {
                "w": world_id,
                "schema": SCHEMA,
                "d": "a" * 64,
                "initial": snapshots.get("initial", minimal),
                "head": snapshots.get("head", minimal),
            }

        # The DB check is a tripwire on schema tag and version only; the
        # strict decoder still refuses this minimal snapshot on load.
        with self.engine.begin() as connection:
            connection.execute(insert, row("world-check-minimal"))

        bad = {
            "empty object": {},
            "json null": None,  # JSON literal null, not SQL NULL
            "array": [],
            "string": "offline-world/v2",
            "number": 0,
            "missing schema": {"version": 0},
            "missing version": {"schema": SCHEMA},
            "null schema": {"schema": None, "version": 0},
            "null version": {"schema": SCHEMA, "version": None},
            "string version": {"schema": SCHEMA, "version": "0"},
            "bool version": {"schema": SCHEMA, "version": False},
            "wrong schema": {"schema": "offline-world/v1", "version": 0},
            "nested schema": {"schema": {"schema": SCHEMA}, "version": 0},
        }
        for column, constraint in (
            ("initial", "world_state_initial_ck"),
            ("head", "world_state_head_ck"),
        ):
            for index, (name, snapshot) in enumerate(bad.items()):
                with self.subTest(column=column, name=name):
                    with self.assertRaises(IntegrityError) as caught:
                        with self.engine.begin() as connection:
                            connection.execute(
                                insert,
                                row(f"world-check-{column}-{index}", **{column: json.dumps(snapshot)}),
                            )
                    self.assertEqual(caught.exception.orig.sqlstate, "23514")
                    self.assertEqual(caught.exception.orig.diag.constraint_name, constraint)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT count(*) FROM world_states")).scalar_one(), 1
            )

    def test_concurrent_first_create_has_one_creator(self):
        start = initial_state()
        same = self.run_processes([lambda s: s.create("world-race", start).created] * 2)
        self.assertEqual(sorted(same), [("ok", False), ("ok", True)])

        at_well = initial_state(location=WELL)
        different = self.run_processes(
            [
                lambda s: s.create("world-race-2", start).created,
                lambda s: s.create("world-race-2", at_well).created,
            ]
        )
        self.assertEqual(sorted(different), [("error", "WorldConflict"), ("ok", True)])
        self.assertIn(self.store.load("world-race-2").initial, (start, at_well))

    def test_simultaneous_writers_one_accepted_other_stale(self):
        start = initial_state()
        self.store.create("world-writers", start)
        to_well = adapter_proposal(start, "move", WELL)
        to_bed = adapter_proposal(start, "move", HERB_BED)
        results = self.run_processes(
            [
                lambda s: summary(s.submit("world-writers", to_well, "writer-a")),
                lambda s: summary(s.submit("world-writers", to_bed, "writer-b")),
            ]
        )
        self.assertEqual(
            sorted(result for _, result in results),
            [(False, "stale_version", 1, False), (True, None, 1, False)],
        )
        loaded = self.store.load("world-writers")
        self.assertEqual(loaded.state.version, 1)
        self.assertEqual(len(loaded.events), 1)
        self.assertEqual(self.count("world_operations", "world-writers"), 2)

    def test_duplicate_submit_returns_same_outcome_without_extra_event(self):
        start = initial_state()
        self.store.create("world-dup", start)
        to_well = adapter_proposal(start, "move", WELL)
        first = self.store.submit("world-dup", to_well, "key-1")
        again = self.store.submit("world-dup", to_well, "key-1")
        self.assertTrue(first.accepted)
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(
            (again.accepted, again.version, again.state_sha256, again.event),
            (first.accepted, first.version, first.state_sha256, first.event),
        )
        with self.assertRaises(WorldConflict):
            self.store.submit("world-dup", adapter_proposal(start, "move", HERB_BED), "key-1")
        # A fresh key cannot count the same fake inference result twice.
        other_key = self.store.submit("world-dup", to_well, "key-2")
        self.assertIs(other_key.reason, RejectionReason.STALE_VERSION)
        self.assertEqual(self.count("world_events", "world-dup"), 1)
        self.assertEqual(self.head("world-dup")[0], 1)

        # The same key raced from two processes still yields one event.
        self.store.create("world-dup-race", start)
        results = self.run_processes(
            [lambda s: summary(s.submit("world-dup-race", to_well, "same-key"))] * 2
        )
        self.assertEqual(
            sorted(result for _, result in results),
            [(True, None, 1, False), (True, None, 1, True)],
        )
        self.assertEqual(self.count("world_events", "world-dup-race"), 1)
        self.assertEqual(self.count("world_operations", "world-dup-race"), 1)

    def test_process_restart_restores_state_and_continues(self):
        committed, _ = self.advanced_world("world-restart")
        [(status, restored)] = self.run_processes(
            [
                lambda s: (
                    lambda w: (w.state.version, w.state.digest, w.state.location, w.state.can_level)
                )(s.load("world-restart"))
            ],
            simultaneous=False,
        )
        self.assertEqual(status, "ok")
        self.assertEqual(
            restored, (2, committed.digest, WELL, committed.can_capacity)
        )

        fresh_engine = create_engine(self.url)
        try:
            fresh = PostgresWorldStore(fresh_engine)
            loaded = fresh.load("world-restart")
            self.assertEqual(loaded.state, committed)
            self.assertEqual(replay(loaded.initial, loaded.events), committed)
            result = fresh.submit(
                "world-restart", adapter_proposal(loaded.state, "move", HERB_BED), "after-restart"
            )
            self.assertTrue(result.accepted)
            self.assertEqual(result.version, 3)
            self.assertEqual(fresh.load("world-restart").state.location, HERB_BED)
        finally:
            fresh_engine.dispose()

    def test_rejection_records_only_the_outcome(self):
        start = initial_state(location=HERB_BED)  # empty can
        self.store.create("world-reject", start)
        head_before = self.head("world-reject")
        water = adapter_proposal(start, "water", HERB_BED)
        rejected = self.store.submit("world-reject", water, "water-1")
        self.assertEqual(summary(rejected), (False, "can_empty", 0, False))
        self.assertIsNone(rejected.event)
        forged = self.store.submit("world-reject", forged_proposal(start, "wait", None), "forged-1")
        self.assertIs(forged.reason, RejectionReason.PROVENANCE_REJECTED)
        self.assertEqual(self.head("world-reject"), head_before)
        self.assertEqual(self.count("world_events", "world-reject"), 0)
        self.assertEqual(self.count("world_operations", "world-reject"), 2)
        self.assertEqual(self.store.load("world-reject").state, start)

        # After the world moves on, the key still reports its original outcome.
        moved = self.store.submit("world-reject", adapter_proposal(start, "move", WELL), "move-1")
        self.assertTrue(moved.accepted)
        self.assertEqual(
            summary(self.store.submit("world-reject", water, "water-1")),
            (False, "can_empty", 0, True),
        )

    def test_tampered_storage_is_refused_on_load_submit_and_retry(self):
        tampering = {
            "event target": "UPDATE world_events SET target = 'herb-bed' "
            "WHERE world_id = :w AND version = 1",
            "event action": "UPDATE world_events SET action = 'wait', target = NULL "
            "WHERE world_id = :w AND version = 2",
            "event digest": "UPDATE world_events SET state_sha256 = repeat('0', 64) "
            "WHERE world_id = :w AND version = 2",
            "event digest from another valid event": "UPDATE world_events SET state_sha256 = "
            "(SELECT state_sha256 FROM world_events WHERE world_id = :w AND version = 1) "
            "WHERE world_id = :w AND version = 2",
            "head digest": "UPDATE world_states SET head_sha256 = repeat('0', 64) "
            "WHERE world_id = :w",
            "head version and snapshot version": "UPDATE world_states SET head_version = 1, "
            "head_state = jsonb_set(head_state, '{version}', '1') WHERE world_id = :w",
            "consistent other head snapshot": "UPDATE world_states SET "
            "head_state = CAST(:s AS jsonb), head_sha256 = :d WHERE world_id = :w",
            "head int for bool": "UPDATE world_states SET head_state = "
            "jsonb_set(head_state, '{well,available}', '1') WHERE world_id = :w",
            "dropped tail": "DELETE FROM world_operations WHERE world_id = :w "
            "AND event_version = 2; DELETE FROM world_events WHERE world_id = :w AND version = 2",
            "initial int for bool": "UPDATE world_states SET initial_state = "
            "jsonb_set(initial_state, '{well,available}', '1') WHERE world_id = :w",
            "initial changed": "UPDATE world_states SET initial_state = "
            "jsonb_set(initial_state, '{water_can,level}', '1') WHERE world_id = :w",
        }
        for index, (name, statement) in enumerate(tampering.items()):
            world_id = f"world-tamper-{index}"
            with self.subTest(name=name):
                state, proposals = self.advanced_world(world_id)
                # Same version and schema, valid digest, but not what the log says.
                other = dataclasses.replace(state, can_level=0)
                params = {"w": world_id, "s": json.dumps(other.as_mapping()), "d": other.digest}
                with self.engine.begin() as connection:
                    for part in statement.split("; "):
                        connection.execute(
                            text(part), {k: v for k, v in params.items() if f":{k}" in part}
                        )
                operations = self.count("world_operations", world_id)
                events = self.count("world_events", world_id)
                head = self.head(world_id)
                with self.assertRaises(ReplayError):
                    self.store.load(world_id)
                with self.assertRaises(ReplayError):
                    self.store.submit(world_id, adapter_proposal(state, "wait", None), "after")
                # A retry of a recorded acceptance is not reported as accepted.
                with self.assertRaises(ReplayError):
                    self.store.submit(world_id, proposals["step-1"], "step-1")
                self.assertEqual(self.count("world_operations", world_id), operations)
                self.assertEqual(self.count("world_events", world_id), events)
                self.assertEqual(self.head(world_id), head)

    def test_failure_mid_transaction_rolls_back_event_and_head(self):
        start = initial_state()
        self.store.create("world-rollback", start)
        to_well = adapter_proposal(start, "move", WELL)
        seen = []

        def fail_after_event(session, operation):
            seen.append(
                session.execute(
                    select(func.count()).select_from(WorldEventRecord)
                ).scalar_one()
            )
            raise RuntimeError("injected failure before operation insert")

        with mock.patch.object(
            PostgresWorldStore, "_insert_operation", staticmethod(fail_after_event)
        ):
            with self.assertRaises(RuntimeError):
                self.store.submit("world-rollback", to_well, "key-1")
        self.assertEqual(seen, [1])  # the event row was written, then rolled back
        self.assertEqual(self.count("world_events", "world-rollback"), 0)
        self.assertEqual(self.count("world_operations", "world-rollback"), 0)
        self.assertEqual(self.head("world-rollback"), (0, start.digest, start.as_mapping()))
        self.assertEqual(self.store.load("world-rollback").state, start)

        retry = self.store.submit("world-rollback", to_well, "key-1")
        self.assertEqual(summary(retry), (True, None, 1, False))
        moved = self.store.load("world-rollback").state
        self.assertEqual(self.head("world-rollback"), (1, moved.digest, moved.as_mapping()))

    def test_no_visitor_or_reply_text_is_stored(self):
        start = initial_state()
        self.store.create("world-text", start)
        talk = adapter_proposal(start, "talk", "visitor", reply=SECRET_REPLY)
        self.assertTrue(self.store.submit("world-text", talk, "talk-1").accepted)
        with self.engine.connect() as connection:
            dumped = [
                connection.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars().all()
                for table in TABLES
            ]
        stored = "\n".join(row for rows in dumped for row in rows)
        self.assertIn("talk", stored)
        self.assertNotIn(SECRET_REPLY, stored)
        self.assertNotIn(VISITOR_TEXT, stored)

    def test_migration_matches_orm(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {ORM_SCHEMA}")
        try:
            Base.metadata.create_all(
                self.engine.execution_options(schema_translate_map={None: ORM_SCHEMA}),
                checkfirst=False,
            )
            self.assertEqual(self.describe("public"), self.describe(ORM_SCHEMA))
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f"DROP SCHEMA {ORM_SCHEMA} CASCADE")

    def describe(self, schema: str) -> tuple:
        with self.engine.connect() as connection:
            columns = connection.execute(
                text(
                    "SELECT table_name, column_name, data_type, character_maximum_length, "
                    "is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = ANY(:t) ORDER BY 1, 2"
                ),
                {"s": schema, "t": list(TABLES)},
            ).all()
            constraints = connection.execute(
                text(
                    "SELECT cl.relname, co.conname, co.contype, pg_get_constraintdef(co.oid) "
                    "FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                    "WHERE n.nspname = :s AND cl.relname = ANY(:t) ORDER BY 1, 2"
                ),
                {"s": schema, "t": list(TABLES)},
            ).all()
        return (
            [tuple(row) for row in columns],
            [
                (table, name, kind, definition.replace(f"{ORM_SCHEMA}.", ""))
                for table, name, kind, definition in constraints
            ],
        )


if __name__ == "__main__":
    unittest.main()
