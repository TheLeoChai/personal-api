-- 0002: isolated metadata-only conversation reservation trial (LEO-173).
-- Applied: pending; authored 2026-09-12, intentionally not applied to live.
-- No raw visitor/model content is stored.  The trial has no HTTP endpoint.

CREATE TABLE public.conversation_reservations (
    resident_id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('active', 'released')),
    owner_id text,
    capability_verifier character(64),
    last_accepted_visitor_message_at timestamp with time zone,
    ownership_version bigint NOT NULL DEFAULT 0 CHECK (ownership_version >= 0),
    CONSTRAINT conversation_reservation_shape_ck CHECK (
        (state = 'active'
            AND owner_id IS NOT NULL
            AND capability_verifier IS NOT NULL
            AND last_accepted_visitor_message_at IS NOT NULL)
        OR
        (state = 'released'
            AND owner_id IS NULL
            AND capability_verifier IS NULL
            AND last_accepted_visitor_message_at IS NULL)
    )
);

CREATE TABLE public.conversation_reservation_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resident_id text NOT NULL REFERENCES public.conversation_reservations(resident_id),
    event_type text NOT NULL CHECK (event_type IN ('released', 'expired')),
    owner_id text NOT NULL,
    ownership_version bigint NOT NULL,
    previous_last_accepted_visitor_message_at timestamp with time zone NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    CONSTRAINT conversation_reservation_event_version_uq
        UNIQUE (resident_id, ownership_version)
);

CREATE INDEX conversation_reservation_events_resident_idx
    ON public.conversation_reservation_events (resident_id, id);

CREATE TABLE public.conversation_reservation_operations (
    operation_kind text NOT NULL,
    resident_id text NOT NULL REFERENCES public.conversation_reservations(resident_id),
    idempotency_key text NOT NULL,
    request_fingerprint character(64) NOT NULL,
    outcome text NOT NULL,
    owner_id text,
    ownership_version bigint NOT NULL,
    accepted_at timestamp with time zone,
    event_id bigint REFERENCES public.conversation_reservation_events(id),
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_kind, resident_id, idempotency_key)
);

CREATE INDEX conversation_reservation_operations_resident_idx
    ON public.conversation_reservation_operations (resident_id);
