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

    def turn(self, state, content, *, registry=None, policy=None, permit=None):
        transport = FakeTransport(content)
        result = run_offline_turn(
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
        return result, transport

    def assert_rejected(self, state, result, reason):
        self.assertFalse(result.decision.accepted)
        self.assertIs(result.decision.reason, reason)
        self.assertIsNone(result.decision.event)
        self.assertIs(result.decision.state, state)

    def run_refill_then_water(self):
        start = initial_state()
        first, first_transport = self.turn(start, fake_reply("refill", "well"))
        second, second_transport = self.turn(
            first.decision.state, fake_reply("water", "herb-bed")
        )
        return start, (first, first_transport), (second, second_transport)

    def test_refill_then_water_is_accepted_through_the_wire_path(self):
        start, (first, first_transport), (second, _) = self.run_refill_then_water()

        self.assertTrue(first.decision.accepted)
        refilled = first.decision.state
        self.assertEqual((refilled.version, refilled.can_level), (1, 3))
        self.assertFalse(refilled.herb_bed_watered)

        self.assertTrue(second.decision.accepted)
        watered = second.decision.state
        self.assertEqual((watered.version, watered.can_level), (2, 2))
        self.assertTrue(watered.herb_bed_watered)

        # The fake reply really crossed the adapter wire: the request carried
        # the assembled persona context with the current world state.
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
        events = (first.decision.event, second.decision.event)
        self.assertEqual(
            [(e.event_id, e.prior_version, e.version, e.action, e.target) for e in events],
            [
                ("world-event-1", 0, 1, "refill", "well"),
                ("world-event-2", 1, 2, "water", "herb-bed"),
            ],
        )
        for event in events:
            self.assertIs(event.inference_mode, InferenceMode.OFFLINE_FAKE_FIXTURE)
        self.assertEqual(
            events[0].context_sha256,
            hashlib.sha256(first.context.envelope.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(replay(start, events), watered)

    def test_transitions_and_events_are_deterministic(self):
        _, (a1, _), (a2, _) = self.run_refill_then_water()
        _, (b1, _), (b2, _) = self.run_refill_then_water()
        self.assertEqual(a1.decision, b1.decision)
        self.assertEqual(a2.decision, b2.decision)
        self.assertEqual(a2.decision.state.digest, b2.decision.state.digest)

    def test_water_with_empty_can_is_rejected_without_state_change(self):
        start = initial_state()
        result, transport = self.turn(start, fake_reply("water", "herb-bed"))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(result.proposal.action, "water")
        self.assert_rejected(start, result, RejectionReason.CAN_EMPTY)
        self.assertEqual((start.version, start.can_level), (0, 0))

    def test_wrong_actor_is_rejected(self):
        other = ApprovedPersonaRegistry.default(resident_id="someone-else")
        start = initial_state()
        result, _ = self.turn(
            start,
            fake_reply("refill", "well"),
            registry=other,
            policy=self.policy_for("someone-else"),
        )
        self.assertEqual(result.proposal.actor_id, "someone-else")
        self.assert_rejected(start, result, RejectionReason.ACTOR_MISMATCH)

    def test_unknown_action_is_rejected(self):
        start = initial_state()
        result, _ = self.turn(start, fake_reply("teleport", "well"))
        self.assert_rejected(start, result, RejectionReason.UNKNOWN_ACTION)

    def test_invalid_target_is_rejected(self):
        start = initial_state(can_level=3)
        for action, target in (("water", "well"), ("refill", None), ("wait", "well")):
            with self.subTest(action=action, target=target):
                result, _ = self.turn(start, fake_reply(action, target))
                self.assert_rejected(start, result, RejectionReason.INVALID_TARGET)

    def test_missing_capability_is_rejected(self):
        start = initial_state(can_level=3, capabilities={"refill", "wait"})
        result, _ = self.turn(start, fake_reply("water", "herb-bed"))
        self.assert_rejected(start, result, RejectionReason.CAPABILITY_DENIED)

    def test_preconditions_are_enforced(self):
        cases = (
            (initial_state(well_available=False), "refill", "well", RejectionReason.WELL_UNAVAILABLE),
            (initial_state(can_level=3), "refill", "well", RejectionReason.CAN_ALREADY_FULL),
            (
                initial_state(can_level=3, herb_bed_watered=True),
                "water",
                "herb-bed",
                RejectionReason.BED_ALREADY_WATERED,
            ),
        )
        for start, action, target, reason in cases:
            with self.subTest(reason=reason):
                result, _ = self.turn(start, fake_reply(action, target))
                self.assert_rejected(start, result, reason)

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
        self.assertEqual(talked.decision.state.can_level, 0)
        event = talked.decision.event
        self.assertNotIn(secret_reply, repr(dataclasses.asdict(event)))
        self.assertNotIn(secret_reply, repr(talked.decision.state.as_mapping()))

    def test_stale_proposal_cannot_be_applied_twice(self):
        start = initial_state()
        result, _ = self.turn(start, fake_reply("refill", "well"))
        after = result.decision.state
        again = apply_proposal(after, result.proposal)
        self.assertFalse(again.accepted)
        self.assertIs(again.reason, RejectionReason.STALE_VERSION)
        self.assertIs(again.state, after)

    def test_direct_fixture_proposal_is_rejected(self):
        start = initial_state()
        forged = ActionProposal(
            actor_id="leo",
            action="refill",
            target="well",
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
        start = initial_state()
        smuggled = json.dumps(
            {"reply": "x", "action": "refill", "target": "well", "actor": "leo"}
        )
        for content in (smuggled, "not json", fake_reply("refill", "well")[:-1]):
            with self.subTest(content=content):
                result, transport = self.turn(start, content)
                self.assertEqual(len(transport.requests), 1)
                self.assertFalse(result.completion.ok)
                self.assertIsNone(result.proposal)
                self.assert_rejected(start, result, RejectionReason.INFERENCE_FAILED)

    def test_denied_permit_never_reaches_transport(self):
        start = initial_state()
        result, transport = self.turn(
            start, fake_reply("refill", "well"), permit=SyntheticPermit(deny=True)
        )
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.completion.failure.category, "admission_denied")
        self.assert_rejected(start, result, RejectionReason.INFERENCE_FAILED)

    def test_missing_visibility_grant_fails_before_transport(self):
        start = initial_state()
        resident_only = SyntheticVisibilityPolicy.for_tests(
            VisibilityGrant(RECIPIENT, "leo", Visibility.RESIDENT)
        )
        transport = FakeTransport(fake_reply("refill", "well"))
        with self.assertRaises(MandatoryContextUnavailable):
            run_offline_turn(
                start,
                self.registry,
                recipient_id=RECIPIENT,
                visibility_policy=resident_only,
                budgets=self.budgets,
                counter=SyntheticTestCounter(),
                visitor_text=VISITOR_TEXT,
                transport=transport,
                permit=self.permit,
                credential_supplier=lambda: FAKE_CREDENTIAL,
                clock=FakeClock(),
            )
        self.assertEqual(transport.requests, [])

    def test_real_or_missing_transport_is_refused(self):
        for transport in (None, UrllibTransport()):
            with self.subTest(transport=type(transport).__name__):
                with self.assertRaises(OfflineOnlyError):
                    run_offline_turn(
                        initial_state(),
                        self.registry,
                        recipient_id=RECIPIENT,
                        visibility_policy=self.policy,
                        budgets=self.budgets,
                        counter=SyntheticTestCounter(),
                        visitor_text=VISITOR_TEXT,
                        transport=transport,
                        permit=self.permit,
                        credential_supplier=lambda: FAKE_CREDENTIAL,
                        clock=FakeClock(),
                    )
        self.assertEqual(self.permit.reserved, [])

    def test_replay_rejects_tampered_or_reordered_events(self):
        start, (first, _), (second, _) = self.run_refill_then_water()
        events = [first.decision.event, second.decision.event]
        bad_logs = {
            "tampered_digest": [events[0], dataclasses.replace(events[1], state_sha256="0" * 64)],
            "reordered": [events[1], events[0]],
            "duplicated": [events[0], events[0]],
            "altered_target": [dataclasses.replace(events[0], target="herb-bed")],
        }
        for name, log in bad_logs.items():
            with self.subTest(name=name):
                with self.assertRaises(ReplayError):
                    replay(start, log)
        self.assertEqual(replay(start, events[:1]), first.decision.state)

    def test_world_values_are_validated(self):
        with self.assertRaises(WorldError):
            initial_state(can_level=4)
        with self.assertRaises(WorldError):
            initial_state(capabilities={"fly"})
        with self.assertRaises(WorldError):
            Resident("", frozenset())
        with self.assertRaises(WorldError):
            WorldState(
                version=-1,
                resident=Resident("leo", frozenset()),
                can_capacity=1,
                can_level=0,
                well_available=True,
                herb_bed_watered=False,
            )


if __name__ == "__main__":
    unittest.main()
