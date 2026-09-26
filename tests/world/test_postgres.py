"""Tests for durable one-resident world persistence (LEO-186).

The pure tests always run.  The PostgreSQL tests run only against an
explicitly supplied disposable local database in LEO186_TEST_DATABASE_URL;
they skip otherwise.  No SQLite or other stand-in is used for them.

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
import subprocess
import sys
import unittest
from unittest import mock

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

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
    initial_state,
    replay,
    run_offline_turn,
)
from world.postgres import (
    Base,
    PostgresWorldStore,
    WorldConflict,
    WorldEventRecord,
    WorldNotFound,
    event_from_record,
    proposal_fingerprint,
)


URL_ENV = "LEO186_TEST_DATABASE_URL"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
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


def summary(result) -> tuple:
    reason = None if result.reason is None else result.reason.value
    return (result.accepted, reason, result.version, result.replayed)


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

    def test_orm_constraint_names_appear_in_migration(self):
        migration = MIGRATION.read_text()
        self.assertIn("Applied: pending", migration)
        for table in Base.metadata.sorted_tables:
            self.assertIn(f"CREATE TABLE public.{table.name} (", migration)
            for constraint in table.constraints:
                with self.subTest(table=table.name, constraint=constraint.name):
                    self.assertIsNotNone(constraint.name)
                    self.assertIn(f"CONSTRAINT {constraint.name} ", migration)

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


def disposable_url() -> str:
    url = os.environ.get(URL_ENV)
    if not url:
        raise unittest.SkipTest(
            f"{URL_ENV} is not set; real PostgreSQL evidence is unavailable"
        )
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg" or parsed.host not in LOCAL_HOSTS:
        raise RuntimeError(f"{URL_ENV} must be a disposable local postgresql+psycopg:// URL")
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
        cls.url = disposable_url()
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        with cls.engine.begin() as connection:
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
        with self.engine.connect() as connection:
            return tuple(
                connection.execute(
                    text("SELECT head_version, head_sha256 FROM world_states WHERE world_id = :w"),
                    {"w": world_id},
                ).one()
            )

    def advanced_world(self, world_id: str) -> WorldState:
        """Create a world and commit move→refill; returns the committed state."""

        state = initial_state()
        self.store.create(world_id, state)
        for step, (action, target) in enumerate((("move", WELL), ("refill", WELL))):
            result = self.store.submit(
                world_id, adapter_proposal(state, action, target), f"step-{step}"
            )
            self.assertTrue(result.accepted, (action, result.reason))
            state = self.store.load(world_id).state
        return state

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
        committed = self.advanced_world("world-restart")
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

    def test_tampered_storage_is_rejected_on_load_and_submit(self):
        tampering = {
            "event target": "UPDATE world_events SET target = 'herb-bed' "
            "WHERE world_id = :w AND version = 1",
            "event action": "UPDATE world_events SET action = 'wait', target = NULL "
            "WHERE world_id = :w AND version = 2",
            "event digest": "UPDATE world_events SET state_sha256 = repeat('0', 64) "
            "WHERE world_id = :w AND version = 2",
            "head digest": "UPDATE world_states SET head_sha256 = repeat('0', 64) "
            "WHERE world_id = :w",
            "head version": "UPDATE world_states SET head_version = 1 WHERE world_id = :w",
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
                state = self.advanced_world(world_id)
                with self.engine.begin() as connection:
                    for part in statement.split("; "):
                        connection.execute(text(part), {"w": world_id})
                operations = self.count("world_operations", world_id)
                with self.assertRaises(ReplayError):
                    self.store.load(world_id)
                with self.assertRaises(ReplayError):
                    self.store.submit(world_id, adapter_proposal(state, "wait", None), "after")
                self.assertEqual(self.count("world_operations", world_id), operations)

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
        self.assertEqual(self.head("world-rollback"), (0, start.digest))

        retry = self.store.submit("world-rollback", to_well, "key-1")
        self.assertEqual(summary(retry), (True, None, 1, False))

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
