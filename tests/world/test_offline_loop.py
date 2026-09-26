"""Offline tests for the synthetic one-resident world loop.

Every model reply here is a FAKE fixture served by an in-memory transport.  The
fixture bytes still travel through the real persona context assembly, the real
OpenRouter adapter request construction, its wire JSON parsing, and then the
authoritative world engine.  There are no sockets, providers, credentials,
real permits, model calls, persistence, or public access.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from inference.openrouter import (
    REQUESTED_MODEL,
    PermitReservation,
    PermitRejected,
    UrllibTransport,
)
from personas import (
    ApprovedPersonaRegistry,
    ContextBudgets,
    MandatoryContextUnavailable,
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
    OfflineOnlyError,
    Provenance,
    RejectionReason,
    ReplayError,
    Resident,
    WorldError,
    WorldState,
    apply_proposal,
    initial_state,
    possible_actions,
    replay,
    run_offline_turn,
)


FAKE_CREDENTIAL = "fake-offline-credential-leo185"
RECIPIENT = "offline-fake-model"
VISITOR_TEXT = "synthetic visitor prompt"


def fake_wire_body(content: str) -> bytes:
    """A provider-shaped response body; the content is a fake model reply."""

    return json.dumps(
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


def fake_reply(action: str, target: str | None, reply: str = "synthetic reply") -> str:
    return json.dumps({"reply": reply, "action": action, "target": target})


class FakeClock:
    def __call__(self) -> float:
        return 100.0


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.body = body

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def close(self) -> None:
        pass


class FakeTransport:
    """Offline transport replaying one fixed fake model reply per send."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests = []

    def send(self, request, *, timeout: float):
        self.requests.append(request)
        return FakeResponse(fake_wire_body(self.content))


def offers_on_wire(request) -> list[tuple[str, str | None]]:
    """Read the offered actions from the actual outgoing request bytes."""

    wire = json.loads(request.body.decode("utf-8"))
    envelope = json.loads(wire["messages"][0]["content"])
    for layer in envelope["layers"]:
        for item in layer["items"]:
            if item["item_id"] == "offline-world-state":
                offers = json.loads(item["text"])["offered_actions"]
                return [(offer["action"], offer["target"]) for offer in offers]
    raise AssertionError("world state item missing from the wire request")


class OfferChoosingTransport:
    """Fake model that picks its reply FROM the offers it actually received.

    It takes the first preference present in the wire offer list; the choice
    is a fixture policy, not model reasoning.
    """

    def __init__(self, *preferences: tuple[str, str | None]) -> None:
        self.preferences = preferences
        self.requests = []
        self.offers: list[list[tuple[str, str | None]]] = []

    def send(self, request, *, timeout: float):
        self.requests.append(request)
        offers = offers_on_wire(request)
        self.offers.append(offers)
        for choice in self.preferences:
            if choice in offers:
                return FakeResponse(fake_wire_body(fake_reply(*choice)))
        raise AssertionError("no preferred action was offered")


class SyntheticPermit:
    """Synthetic one-use permits; this is not real quota enforcement."""

    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.reserved = []
        self.settled = []
        self._used_request_ids: set[str] = set()

    def reserve(self, request):
        if self.deny:
            raise PermitRejected("denied")
        if request.request_id in self._used_request_ids:
            raise PermitRejected("reused")
        self._used_request_ids.add(request.request_id)
        self.reserved.append(request)
        return PermitReservation(f"synthetic-offline-{len(self.reserved)}")

    def settle(self, reservation, *, outcome, sent_state):
        self.settled.append((reservation, outcome, sent_state))
        return True


class OfflineLoopTests(unittest.TestCase):
    def setUp(self):
        self.registry = ApprovedPersonaRegistry.default()
        self.policy = self.policy_for("leo")
        self.budgets = ContextBudgets(
            high=100_000, medium=100_000, immediate=100_000, total=300_000
        )
        self.permit = SyntheticPermit()

    @staticmethod
    def policy_for(resident_id: str) -> SyntheticVisibilityPolicy:
        # Explicit synthetic test grants only; this is not a privacy approval.
        return SyntheticVisibilityPolicy.for_tests(
            VisibilityGrant(RECIPIENT, resident_id, Visibility.RESIDENT),
            VisibilityGrant(RECIPIENT, resident_id, Visibility.PRIVATE),
        )

    def run_turn(self, state, transport, *, registry=None, policy=None, permit=None):
        return run_offline_turn(
            state,
            registry or self.registry,
            recipient_id=RECIPIENT,
            visibility_policy=policy or self.policy,
            budgets=self.budgets,
            counter=SyntheticTestCounter(),
            visitor_text=VISITOR_TEXT,
            transport=transport,
            permit=permit or self.permit,
            credential_supplier=lambda: FAKE_CREDENTIAL,
            clock=FakeClock(),
        )

    def turn(self, state, content, **kwargs):
        """A fake that replies with a fixed choice, offered or not."""

        transport = FakeTransport(content)
        return self.run_turn(state, transport, **kwargs), transport

    def choose(self, state, *preferences):
        """A fake that must choose from the offers on the wire."""

        transport = OfferChoosingTransport(*preferences)
        return self.run_turn(state, transport), transport

    def assert_rejected(self, state, result, reason):
        self.assertFalse(result.decision.accepted)
        self.assertIs(result.decision.reason, reason)
        self.assertIsNone(result.decision.event)
        self.assertIs(result.decision.state, state)

    def run_refill_then_water(self):
        start = initial_state()
        state = start
        turns = []
        for choice in (
            ("move", WELL),
            ("refill", WELL),
            ("move", HERB_BED),
            ("water", HERB_BED),
        ):
            result, transport = self.choose(state, choice)
            self.assertTrue(result.decision.accepted, choice)
            turns.append((result, transport))
            state = result.decision.state
        return start, turns

    def test_refill_then_water_is_accepted_through_the_wire_path(self):
        start, turns = self.run_refill_then_water()
        states = [result.decision.state for result, _ in turns]
        self.assertEqual(
            [(s.version, s.location, s.can_level, s.herb_bed_watered) for s in states],
            [
                (1, WELL, 0, False),
                (2, WELL, 3, False),
                (3, HERB_BED, 3, False),
                (4, HERB_BED, 2, True),
            ],
        )
        # Each fake choice came from the offers actually sent on the wire.
        self.assertEqual(
            [transport.offers for _, transport in turns],
            [
                [[("move", HERB_BED), ("move", WELL), ("talk", "visitor"), ("wait", None)]],
                [
                    [
                        ("move", ELSEWHERE),
                        ("move", HERB_BED),
                        ("refill", WELL),
                        ("talk", "visitor"),
                        ("wait", None),
                    ]
                ],
                [[("move", ELSEWHERE), ("move", HERB_BED), ("talk", "visitor"), ("wait", None)]],
                [
                    [
                        ("move", ELSEWHERE),
                        ("move", WELL),
                        ("talk", "visitor"),
                        ("wait", None),
                        ("water", HERB_BED),
                    ]
                ],
            ],
        )

        # The fake reply really crossed the adapter wire: the request carried
        # the assembled persona context with the current world state.
        first, first_transport = turns[0]
        self.assertEqual(len(first_transport.requests), 1)
        request = first_transport.requests[0]
        wire = json.loads(request.body.decode("utf-8"))
        self.assertEqual(wire["model"], REQUESTED_MODEL)
        self.assertEqual(wire["messages"][0]["content"], first.context.envelope)
        self.assertEqual(wire["messages"][1]["content"], VISITOR_TEXT)
        envelope = json.loads(first.context.envelope)
        items = {
            item["item_id"]: item
            for layer in envelope["layers"]
            for item in layer["items"]
        }
        self.assertEqual(items["offline-world-state"]["trust"], "provided")
        self.assertEqual(items["offline-world-state"]["source_role"], "system")
        world = json.loads(items["offline-world-state"]["text"])["world"]
        self.assertEqual(world["version"], 0)
        self.assertEqual(world["resident"]["location"], ELSEWHERE)
        self.assertEqual(world["water_can"], {"capacity": 3, "level": 0})
        self.assertIn("artsy-2", items)  # approved registry fact, not invented

        # The synthetic permit was bound to the exact request bytes.
        self.assertEqual(
            self.permit.reserved[0].request_sha256,
            hashlib.sha256(request.body).hexdigest(),
        )
        self.assertEqual(first.completion.accounting_state, "settled_completed")

        # The actor came from the context, not the model reply.
        self.assertEqual(first.proposal.actor_id, "leo")
        events = tuple(result.decision.event for result, _ in turns)
        self.assertEqual(
            [(e.event_id, e.prior_version, e.version, e.action, e.target) for e in events],
            [
                ("world-event-1", 0, 1, "move", WELL),
                ("world-event-2", 1, 2, "refill", WELL),
                ("world-event-3", 2, 3, "move", HERB_BED),
                ("world-event-4", 3, 4, "water", HERB_BED),
            ],
        )
        for event, state in zip(events, states):
            self.assertIs(event.inference_mode, InferenceMode.OFFLINE_FAKE_FIXTURE)
            self.assertEqual(event.state_sha256, state.digest)
        self.assertEqual(
            events[0].context_sha256,
            hashlib.sha256(first.context.envelope.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(replay(start, events), states[-1])

    def test_transitions_and_events_are_deterministic(self):
        _, first_run = self.run_refill_then_water()
        _, second_run = self.run_refill_then_water()
        self.assertEqual(
            [result.decision for result, _ in first_run],
            [result.decision for result, _ in second_run],
        )

    def test_offers_exclude_currently_impossible_actions(self):
        cases = (
            ("empty can at bed", initial_state(location=HERB_BED), ("water", HERB_BED), RejectionReason.CAN_EMPTY),
            ("full can at well", initial_state(location=WELL, can_level=3), ("refill", WELL), RejectionReason.CAN_ALREADY_FULL),
            ("closed well", initial_state(location=WELL, well_available=False), ("refill", WELL), RejectionReason.WELL_UNAVAILABLE),
            (
                "watered bed",
                initial_state(location=HERB_BED, can_level=3, herb_bed_watered=True),
                ("water", HERB_BED),
                RejectionReason.BED_ALREADY_WATERED,
            ),
            ("away from well", initial_state(location=HERB_BED), ("refill", WELL), RejectionReason.WRONG_LOCATION),
            ("away from bed", initial_state(location=WELL, can_level=3), ("water", HERB_BED), RejectionReason.WRONG_LOCATION),
        )
        for name, start, impossible, reason in cases:
            with self.subTest(name=name):
                # A fake choosing from the wire offers cannot pick it.
                chosen, transport = self.choose(start, impossible, ("wait", None))
                self.assertNotIn(impossible, transport.offers[0])
                self.assertEqual(transport.offers[0], list(possible_actions(start)))
                self.assertTrue(chosen.decision.accepted)
                self.assertEqual(chosen.decision.event.action, "wait")

                # A fake ignoring the offers is still rejected by the engine.
                forced, _ = self.turn(start, fake_reply(*impossible))
                self.assert_rejected(start, forced, reason)
                self.assertEqual(start.version, 0)

    def test_offers_include_possible_place_bound_actions(self):
        at_well = initial_state(location=WELL)
        at_bed = initial_state(location=HERB_BED, can_level=1)
        _, well_transport = self.choose(at_well, ("wait", None))
        _, bed_transport = self.choose(at_bed, ("wait", None))
        self.assertIn(("refill", WELL), well_transport.offers[0])
        self.assertIn(("water", HERB_BED), bed_transport.offers[0])

    def test_location_gates_refill_and_water(self):
        for location in (ELSEWHERE, HERB_BED):
            with self.subTest(refill_from=location):
                start = initial_state(location=location)
                result, _ = self.turn(start, fake_reply("refill", WELL))
                self.assert_rejected(start, result, RejectionReason.WRONG_LOCATION)
        for location in (ELSEWHERE, WELL):
            with self.subTest(water_from=location):
                start = initial_state(location=location, can_level=3)
                result, _ = self.turn(start, fake_reply("water", HERB_BED))
                self.assert_rejected(start, result, RejectionReason.WRONG_LOCATION)

        refilled, _ = self.turn(initial_state(location=WELL), fake_reply("refill", WELL))
        self.assertTrue(refilled.decision.accepted)
        self.assertEqual(refilled.decision.state.can_level, 3)
        watered, _ = self.turn(
            initial_state(location=HERB_BED, can_level=3), fake_reply("water", HERB_BED)
        )
        self.assertTrue(watered.decision.accepted)
        self.assertTrue(watered.decision.state.herb_bed_watered)

    def test_move_is_validated(self):
        start = initial_state(location=WELL)
        same, _ = self.turn(start, fake_reply("move", WELL))
        self.assert_rejected(start, same, RejectionReason.ALREADY_THERE)
        for target in ("moon", None, "visitor"):
            with self.subTest(target=target):
                result, _ = self.turn(start, fake_reply("move", target))
                self.assert_rejected(start, result, RejectionReason.INVALID_TARGET)
        moved, _ = self.turn(start, fake_reply("move", HERB_BED))
        self.assertTrue(moved.decision.accepted)
        self.assertEqual(moved.decision.state.location, HERB_BED)
        self.assertEqual(moved.decision.state.can_level, start.can_level)

    def test_water_with_empty_can_is_rejected_without_state_change(self):
        start = initial_state(location=HERB_BED)
        result, transport = self.turn(start, fake_reply("water", HERB_BED))
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn(("water", HERB_BED), offers_on_wire(transport.requests[0]))
        self.assertEqual(result.proposal.action, "water")
        self.assert_rejected(start, result, RejectionReason.CAN_EMPTY)
        self.assertEqual((start.version, start.can_level), (0, 0))

    def test_stale_offer_cannot_mutate(self):
        start = initial_state(location=WELL)
        result, transport = self.choose(start, ("refill", WELL))
        self.assertIn(("refill", WELL), transport.offers[0])
        after = result.decision.state

        again = apply_proposal(after, result.proposal)
        self.assertFalse(again.accepted)
        self.assertIs(again.reason, RejectionReason.STALE_VERSION)
        self.assertIs(again.state, after)

        # Even at a matching version, the engine re-checks the current state
        # rather than trusting the offer that was made earlier.
        full_same_version = initial_state(location=WELL, can_level=3)
        rechecked = apply_proposal(full_same_version, result.proposal)
        self.assertFalse(rechecked.accepted)
        self.assertIs(rechecked.reason, RejectionReason.CAN_ALREADY_FULL)
        self.assertIs(rechecked.state, full_same_version)

    def test_wrong_actor_is_rejected(self):
        other = ApprovedPersonaRegistry.default(resident_id="someone-else")
        start = initial_state(location=WELL)
        result, _ = self.turn(
            start,
            fake_reply("refill", WELL),
            registry=other,
            policy=self.policy_for("someone-else"),
        )
        self.assertEqual(result.proposal.actor_id, "someone-else")
        self.assert_rejected(start, result, RejectionReason.ACTOR_MISMATCH)

    def test_unknown_action_is_rejected(self):
        start = initial_state(location=WELL)
        result, _ = self.turn(start, fake_reply("teleport", WELL))
        self.assert_rejected(start, result, RejectionReason.UNKNOWN_ACTION)

    def test_invalid_target_is_rejected(self):
        start = initial_state(location=HERB_BED, can_level=3)
        for action, target in (("water", WELL), ("refill", None), ("wait", WELL), ("talk", None)):
            with self.subTest(action=action, target=target):
                result, _ = self.turn(start, fake_reply(action, target))
                self.assert_rejected(start, result, RejectionReason.INVALID_TARGET)

    def test_missing_capability_is_rejected_and_not_offered(self):
        start = initial_state(location=HERB_BED, can_level=3, capabilities={"refill", "wait"})
        self.assertEqual(possible_actions(start), (("wait", None),))
        result, _ = self.turn(start, fake_reply("water", HERB_BED))
        self.assert_rejected(start, result, RejectionReason.CAPABILITY_DENIED)

    def test_wait_and_talk_record_events_without_reply_text(self):
        start = initial_state()
        secret_reply = "synthetic reply that must not be stored"
        waited, _ = self.turn(start, fake_reply("wait", None))
        talked, _ = self.turn(
            waited.decision.state, fake_reply("talk", "visitor", reply=secret_reply)
        )
        self.assertTrue(waited.decision.accepted)
        self.assertTrue(talked.decision.accepted)
        self.assertEqual(talked.decision.state.version, 2)
        self.assertEqual(talked.decision.state.location, ELSEWHERE)
        self.assertEqual(talked.decision.state.can_level, 0)
        event = talked.decision.event
        self.assertNotIn(secret_reply, repr(dataclasses.asdict(event)))
        self.assertNotIn(secret_reply, repr(talked.decision.state.as_mapping()))

    def test_direct_fixture_proposal_is_rejected(self):
        start = initial_state(location=WELL)
        forged = ActionProposal(
            actor_id="leo",
            action="refill",
            target=WELL,
            based_on_version=0,
            provenance=Provenance(
                inference_mode=InferenceMode.OFFLINE_FAKE_FIXTURE,
                context_sha256="0" * 64,
                accounting_state="settled_completed",
            ),
        )
        decision = apply_proposal(start, forged)
        self.assertFalse(decision.accepted)
        self.assertIs(decision.reason, RejectionReason.PROVENANCE_REJECTED)
        self.assertIs(decision.state, start)

    def test_model_cannot_supply_actor_or_malformed_json(self):
        start = initial_state(location=WELL)
        smuggled = json.dumps(
            {"reply": "x", "action": "refill", "target": WELL, "actor": "leo"}
        )
        for content in (smuggled, "not json", fake_reply("refill", WELL)[:-1]):
            with self.subTest(content=content):
                result, transport = self.turn(start, content)
                self.assertEqual(len(transport.requests), 1)
                self.assertFalse(result.completion.ok)
                self.assertIsNone(result.proposal)
                self.assert_rejected(start, result, RejectionReason.INFERENCE_FAILED)

    def test_denied_permit_never_reaches_transport(self):
        start = initial_state(location=WELL)
        result, transport = self.turn(
            start, fake_reply("refill", WELL), permit=SyntheticPermit(deny=True)
        )
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.completion.failure.category, "admission_denied")
        self.assert_rejected(start, result, RejectionReason.INFERENCE_FAILED)

    def test_missing_visibility_grant_fails_before_transport(self):
        resident_only = SyntheticVisibilityPolicy.for_tests(
            VisibilityGrant(RECIPIENT, "leo", Visibility.RESIDENT)
        )
        transport = FakeTransport(fake_reply("refill", WELL))
        with self.assertRaises(MandatoryContextUnavailable):
            self.run_turn(initial_state(), transport, policy=resident_only)
        self.assertEqual(transport.requests, [])

    def test_real_or_missing_transport_is_refused(self):
        for transport in (None, UrllibTransport()):
            with self.subTest(transport=type(transport).__name__):
                with self.assertRaises(OfflineOnlyError):
                    self.run_turn(initial_state(), transport)
        self.assertEqual(self.permit.reserved, [])

    def test_replay_rejects_tampered_or_reordered_events(self):
        start, turns = self.run_refill_then_water()
        events = [result.decision.event for result, _ in turns]
        bad_logs = {
            "tampered_digest": [events[0], dataclasses.replace(events[1], state_sha256="0" * 64)],
            "reordered": [events[1], events[0]],
            "duplicated": [events[0], events[0]],
            "altered_target": [dataclasses.replace(events[0], target=HERB_BED)],
            "skipped_move": [events[0], dataclasses.replace(events[2], prior_version=1, version=2, event_id="world-event-2")],
        }
        for name, log in bad_logs.items():
            with self.subTest(name=name):
                with self.assertRaises(ReplayError):
                    replay(start, log)
        self.assertEqual(replay(start, events[:2]), turns[1][0].decision.state)

    def test_world_values_are_validated(self):
        with self.assertRaises(WorldError):
            initial_state(can_level=4)
        with self.assertRaises(WorldError):
            initial_state(capabilities={"fly"})
        with self.assertRaises(WorldError):
            initial_state(location="moon")
        with self.assertRaises(WorldError):
            Resident("", frozenset())
        with self.assertRaises(WorldError):
            WorldState(
                version=-1,
                resident=Resident("leo", frozenset()),
                location=ELSEWHERE,
                can_capacity=1,
                can_level=0,
                well_available=True,
                herb_bed_watered=False,
            )


if __name__ == "__main__":
    unittest.main()
