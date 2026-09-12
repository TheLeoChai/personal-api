# Conversation reservations: LEO-173 Luna trial

Status: revision round 2 ready for child ticket review, 2026-09-12. The
child is an isolated library trial. It does not add an HTTP route, change
`openapi.json`, deploy anything, or implement LEO-135's parent endpoint.

## Initial plan and scope

The initial plan recorded from LEO-173 and LEO-135 was to:

1. keep the parent Backlog and its blockers unchanged;
2. add a small synchronous SQLAlchemy/PostgreSQL reservation library under
   `src/conversations/`, with no import of production `db.py`, `models.py`, or
   startup `create_all`;
3. author the next numbered migration without applying it to live;
4. prove policy boundaries with a fake clock and prove persistence,
   contention, restart, lifecycle events, and transaction fencing against an
   explicitly supplied disposable PostgreSQL database;
5. document the integration seam and stop the child In Review for independent
   Astra review.

The implementation stores synthetic resident and owner identifiers, a hashed
capability verifier, reservation state, the last accepted visitor message
timestamp, and a monotonic ownership version. It has no raw chat, model
output, visitor data, world state, credentials, Redis integration, or model
calls.

## Contract

`PostgresReservationStore` requires an injected SQLAlchemy engine or session
factory. By default it obtains time from PostgreSQL `clock_timestamp()` after
locking the resident row. This keeps the authoritative clock shared across
processes and avoids transaction-start time becoming stale during a lock wait.
Tests may inject a clock that returns a fixed aware UTC timestamp; callers
cannot supply operation timestamps.

The synthetic adapter uses `claim(resident_id, owner_id, idempotency_key)` as
the first accepted visitor message. This provisional transport convention
starts the five-minute idle window and is not product approval of an initial
claim. A successful claim returns one opaque capability token; only its
SHA-256 verifier is persisted. A retry with the same claim key replays the
metadata result but cannot recreate the clear token, so a caller must retain
the original capability. Lost-capability recovery and rotation remain outside
this child.

`renew` is the only operation that updates
`last_accepted_visitor_message_at`. It requires the resident, owner,
capability, current ownership version, and an idempotency key. The reservation
expires inclusively at `last_accepted_visitor_message_at + 300 seconds`.
Duplicate renewals, rejected or forged capabilities, heartbeats, model result
applications, and capability reattachment do not renew. `goodbye` releases
immediately and is idempotent. Claim, renew, heartbeat, reattach, goodbye, and
result keys are scoped by operation kind and resident; reusing a key with a
different metadata fingerprint raises `IdempotencyConflict`.

Every claim, release, and expiry advances the ownership version. Release and
expiry write one metadata-only lifecycle event in the same transaction as the
state transition. The event carries the resident, prior owner, new fence
version, prior accepted-message timestamp, event type, and authoritative event
time. It is a handoff seam for future pause/resume-or-reassess handlers; task
resume, pause behavior, and world behavior are not implemented here.

All authenticated operations lock the resident row before sampling time and
checking state, owner, capability verifier, version, and expiry. Expiry is
reconciled before an operation can be accepted, even if a cleanup worker was
delayed. The database primary key plus `INSERT ... ON CONFLICT DO NOTHING`
creates a row that can be locked before a first claim, so concurrent processes
linearize on one resident. A stale version or released state returns
`fenced`; a current version with a wrong capability returns
`invalid_capability`.

`apply_result(..., mutation)` is the transaction-aware future integration seam.
The store holds the resident row lock while checking authorization and invokes
the callback with the same SQLAlchemy session inside a nested savepoint. When
the callback returns, it samples the authoritative clock again. At the
inclusive 300-second boundary it rolls back the savepoint, expires and fences
the reservation, records the lifecycle event and `expired` result outcome, and
commits no target mutation. A callback exception rolls back the target and
operation record together. The callback must be database-only and must not
commit, roll back, perform network/file/queue work, or cause other external
side effects that a database savepoint cannot undo.

The post-callback clock sample is the result transaction's authorization and
linearization point. It prevents a callback that crosses the observed deadline
from committing. PostgreSQL can still make a transaction visible after the
sample because of commit latency when the final guard passed before the
deadline; this library does not claim a physical wall-clock visibility bound
after commit. Ownership changes remain serialized by the resident lock, and
the 300-second rule is enforced at every result authorization point.

## Local tests

The production `DATABASE_URL` is never read by the test suite. Without an
explicit `LEO173_TEST_DATABASE_URL`, the three pure policy tests run and the
twelve PostgreSQL tests skip with an explicit reason.

Install the declared dependencies into a disposable environment:

```sh
uv venv /tmp/leo173-venv
uv pip install --python /tmp/leo173-venv/bin/python \
  -r requirements-dev.txt -r src/requirements.txt
```

Start a fresh local-only PostgreSQL 16 container with synthetic credentials,
bind its random port to loopback, and use that port in the URL:

```sh
docker run --detach --name leo173-pg \
  --label codex-ticket=LEO-173 \
  --env POSTGRES_USER=leo173 \
  --env POSTGRES_PASSWORD=local-test-only \
  --env POSTGRES_DB=leo173 \
  --publish 127.0.0.1::5432 postgres:16
docker port leo173-pg 5432/tcp
LEO173_TEST_DATABASE_URL=postgresql+psycopg://leo173:local-test-only@127.0.0.1:PORT/leo173 \
  /tmp/leo173-venv/bin/python -m pytest -q
```

The fixture resets only that explicitly supplied test schema, executes
`migrations/0002_conversation_reservations.sql`, and creates a synthetic
metadata target used by fencing tests. It never imports production startup
code. Remove only the verified test container after the run.

First-pass evidence, retained for calibration:

- `/tmp/leo173-venv/bin/python -m pytest tests/conversations/test_domain.py -q` — 3 passed.
- Without a database URL, `/tmp/leo173-venv/bin/python -m pytest -q` — 3 passed, 8 skipped with the documented explicit-URL reason.
- Against the first-pass labeled localhost-only PostgreSQL 16 container — `11 passed in 2.35s`.

Revision round 2 evidence:

- `env -u LEO173_TEST_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 /tmp/leo173-r2-venv/bin/python -m pytest -q -p no:cacheprovider` — 3 passed, 12 skipped.
- Against a new bounded, labeled localhost-only PostgreSQL 16 container — `15 passed in 5.86s`. This includes independent OS-process claim contention, independent-process reopen before and after expiry with event recovery, time advanced between every non-renewing operation, an observed PostgreSQL lock wait, expiry/reclaim fencing, controlled result/goodbye overlap, savepoint rollback, and both injected and real database-clock callback expiry.
- Independent probes derived from the supplied `/tmp/LEO173-review-evidence/test_review_probes.py` ideas, in a separate `/tmp/LEO173-r2-probes.py` — `2 passed in 4.56s`. Both the 299.999-to-300-second injected callback and the PostgreSQL-clock callback with 1.2 seconds of database sleep returned `expired` and left zero synthetic targets committed.

## Calibration and remaining limits

Initial findings were a clean `main` at `05b4005`, synchronous SQLAlchemy with
separate production engine initialization, no test harness, and no configured
migration runner. The first independent review found and reproduced a
post-callback expiry race and incomplete timing/restart evidence. Revision
round 2 adds the nested-savepoint post-callback guard and the required
regressions; the original first-pass report and commit remain unchanged for
calibration. Independent Claude Opus 5 review is pending. The migration is
authored and unapplied to live. No endpoint, OpenAPI, worker, compose,
frontend, upload, NAS, or production configuration was changed.

The future API still needs explicit decisions and integration for privacy and
visibility, inference admission, reconnect/lost-capability policy, pause and
resume behavior, public response handling, and the parent route contract. The
synthetic result callback proves the storage transaction seam only; it does not
prove world, plan, memory, dialogue, Redis, or model integration. No fairness
cap, one-resident-per-visitor restriction, or absolute maximum duration was
invented.
