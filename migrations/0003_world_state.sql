-- 0003: durable synthetic one-resident world head + ordered events (LEO-186).
-- Applied: pending; authored 2026-09-26, intentionally not applied to live.
-- Offline-fixture world only.  No raw visitor or model reply text is stored.
-- No HTTP endpoint.  World time and downtime catch-up are undecided and
-- deliberately absent.  Mirrors the ORM in src/world/postgres.py.

CREATE TABLE public.world_states (
    world_id text NOT NULL,
    initial_state jsonb NOT NULL,
    initial_sha256 character(64) NOT NULL,
    head_version bigint NOT NULL,
    head_sha256 character(64) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT world_states_pkey PRIMARY KEY (world_id),
    CONSTRAINT world_state_id_ck CHECK (char_length(world_id) BETWEEN 1 AND 128),
    CONSTRAINT world_state_initial_ck CHECK (jsonb_typeof(initial_state) = 'object'),
    CONSTRAINT world_state_head_version_ck CHECK (head_version BETWEEN 0 AND 4096),
    CONSTRAINT world_state_sha256_ck CHECK (
        initial_sha256 ~ '^[0-9a-f]{64}$' AND head_sha256 ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE public.world_events (
    world_id text NOT NULL,
    version bigint NOT NULL,
    prior_version bigint NOT NULL,
    event_id text NOT NULL,
    actor_id text NOT NULL,
    action text NOT NULL,
    target text,
    inference_mode text NOT NULL,
    context_sha256 character(64) NOT NULL,
    state_sha256 character(64) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT world_events_pkey PRIMARY KEY (world_id, version),
    CONSTRAINT world_event_world_fk FOREIGN KEY (world_id)
        REFERENCES public.world_states(world_id),
    CONSTRAINT world_event_version_ck CHECK (
        version BETWEEN 1 AND 4096 AND prior_version = version - 1
    ),
    CONSTRAINT world_event_id_ck CHECK (event_id = 'world-event-' || version),
    CONSTRAINT world_event_mode_ck CHECK (inference_mode = 'offline-fake-fixture'),
    CONSTRAINT world_event_sha256_ck CHECK (
        context_sha256 ~ '^[0-9a-f]{64}$' AND state_sha256 ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE public.world_operations (
    world_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_fingerprint character(64) NOT NULL,
    outcome text NOT NULL,
    reason text,
    version bigint NOT NULL,
    state_sha256 character(64) NOT NULL,
    event_version bigint,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT world_operations_pkey PRIMARY KEY (world_id, idempotency_key),
    CONSTRAINT world_operation_world_fk FOREIGN KEY (world_id)
        REFERENCES public.world_states(world_id),
    CONSTRAINT world_operation_event_fk FOREIGN KEY (world_id, event_version)
        REFERENCES public.world_events(world_id, version),
    CONSTRAINT world_operation_event_uq UNIQUE (world_id, event_version),
    CONSTRAINT world_operation_key_ck CHECK (char_length(idempotency_key) BETWEEN 1 AND 255),
    CONSTRAINT world_operation_shape_ck CHECK (
        (outcome = 'accepted' AND reason IS NULL
            AND event_version IS NOT NULL AND event_version = version)
        OR (outcome = 'rejected' AND reason IS NOT NULL AND event_version IS NULL)
    )
);
