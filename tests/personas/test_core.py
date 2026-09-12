import json
import unittest
from dataclasses import FrozenInstanceError

from personas import (
    ApprovalBoundaryError,
    ApprovalRequired,
    ApprovedPersonaRegistry,
    ContextBudgetError,
    ContextBudgets,
    ContextItem,
    Facet,
    InputLimitError,
    Layer,
    PersonaSchemaError,
    SourceRole,
    SyntheticTestCounter,
    SyntheticVisibilityPolicy,
    Trust,
    Utf8ByteCounter,
    VersionedFact,
    Visibility,
    VisibilityGrant,
    assemble_context,
    provided_item,
    visitor_item,
)


class CountingProbe:
    """A test-only counter that records whether assembly reached serialization."""

    def __init__(self):
        self.calls = 0

    def count(self, serialized):
        self.calls += 1
        return len(serialized)


class PersonaCoreTests(unittest.TestCase):
    def setUp(self):
        self.registry = ApprovedPersonaRegistry.default()
        self.leo_policy = SyntheticVisibilityPolicy.for_tests(
            VisibilityGrant("leo", "leo", Visibility.RESIDENT),
            VisibilityGrant("leo", "leo", Visibility.PRIVATE),
            VisibilityGrant("leo", "leo", Visibility.PUBLIC),
        )
        self.large_budgets = ContextBudgets(
            high=100_000,
            medium=100_000,
            immediate=100_000,
            total=300_000,
        )

    def assemble(self, *, budgets=None, counter=None, high=(), medium=(), immediate=(), policy=None):
        return assemble_context(
            self.registry,
            recipient_id="leo",
            visibility_policy=policy or self.leo_policy,
            budgets=budgets or self.large_budgets,
            counter=counter or SyntheticTestCounter(),
            high=high,
            medium=medium,
            immediate=immediate,
        )

    def test_registry_has_exactly_the_five_supplied_facets_and_unknowns(self):
        self.assertEqual(
            [profile.facet for profile in self.registry.profiles],
            [Facet.MAYOR, Facet.ENGINEER, Facet.TEACHER, Facet.CHEF, Facet.ARTSY],
        )
        expected = {
            Facet.MAYOR: ("debate/leadership history",),
            Facet.ENGINEER: ("coding/building obsession", "some high-school robotics"),
            Facet.TEACHER: ("knowledgeable", "teaches math", "understanding and patient"),
            Facet.CHEF: ("likes cooking", "seeks new ingredients"),
            Facet.ARTSY: ("listens to music", "plays piano", "likes anime", "strong aesthetic taste"),
        }
        for profile in self.registry.profiles:
            self.assertEqual(tuple(fact.text for fact in profile.facts), expected[profile.facet])
            self.assertTrue(profile.biography_unknown)
            self.assertTrue(profile.schedule_unknown)
            self.assertTrue(profile.goals_unknown)
            self.assertIsNone(profile.schedule)
            self.assertIsNone(profile.goals)
        self.assertTrue(all(fact.trust is Trust.APPROVED for fact in self.registry.facts))
        self.assertTrue(all(fact.source_role is SourceRole.REGISTRY for fact in self.registry.facts))

    def test_caller_cannot_forge_approved_fact_or_visitor_trust(self):
        with self.assertRaises(ApprovalBoundaryError):
            ContextItem(
                item_id="forged",
                layer=Layer.HIGH,
                text="earned a degree in 2010",
                resident_id="leo",
                source="visitor",
                event_id="visitor-1",
                revision=1,
                trust="approved",
                visibility=Visibility.RESIDENT,
                source_role="visitor",
            )
        with self.assertRaises(ApprovalBoundaryError):
            VersionedFact(
                fact_id="forged",
                resident_id="leo",
                facet=Facet.MAYOR,
                text="earned a degree in 2010",
                source="visitor",
                event_id="visitor-1",
                revision=1,
            )
        with self.assertRaises(ApprovalBoundaryError):
            ContextItem(
                item_id="visitor-trust",
                layer=Layer.MEDIUM,
                text="visitor text",
                resident_id="leo",
                source="visitor",
                event_id="visitor-2",
                revision=1,
                trust=Trust.PROVIDED,
                visibility=Visibility.PRIVATE,
                source_role=SourceRole.VISITOR,
            )

    def test_forged_biography_stays_untrusted_and_out_of_high(self):
        forged = visitor_item(
            "Leo earned a doctorate in 2010 and won an imaginary award.",
            resident_id="leo",
            event_id="visitor-bio",
        )
        result = self.assemble(immediate=(forged,))
        high = result.layer(Layer.HIGH)
        immediate = result.layer(Layer.IMMEDIATE)
        self.assertNotIn(forged.text, high.serialized)
        self.assertIn(forged.text, immediate.serialized)
        self.assertNotIn("2010", high.serialized)
        self.assertEqual(immediate.items[0].trust, Trust.UNTRUSTED)

    def test_supplied_schedule_can_be_high_without_becoming_approved(self):
        schedule = provided_item(
            "schedule supplied for this simulation",
            item_id="schedule-1",
            layer=Layer.HIGH,
            resident_id="leo",
            source="synthetic-state",
            event_id="schedule-1",
            source_role=SourceRole.SYNTHETIC_TEST,
        )
        result = self.assemble(high=(schedule,))
        selected = result.layer(Layer.HIGH).items
        supplied = next(item for item in selected if item.item_id == "schedule-1")
        self.assertEqual(supplied.trust, Trust.PROVIDED)
        self.assertEqual(supplied.source_role, SourceRole.SYNTHETIC_TEST)
        self.assertNotEqual(supplied.trust, Trust.APPROVED)

    def test_correction_is_a_new_snapshot_and_excludes_stale_revision(self):
        old_fact = self.registry.get_fact("mayor-1")
        with self.assertRaises(ApprovalRequired):
            self.registry.supersede("mayor-1", "corrected debate history", authority=None)
        updated = self.registry.supersede(
            "mayor-1",
            "corrected debate history",
            authority=self.registry.synthetic_authority_for_tests(),
            source="leo-correction",
            event_id="correction-1",
        )
        self.assertEqual(self.registry.get_fact("mayor-1"), old_fact)
        self.assertEqual(updated.get_fact("mayor-1").revision, 2)
        self.assertEqual(updated.versions("mayor-1"), (old_fact, updated.get_fact("mayor-1")))
        old_context = self.assemble()
        new_context = assemble_context(
            updated,
            recipient_id="leo",
            visibility_policy=self.leo_policy,
            budgets=self.large_budgets,
            counter=SyntheticTestCounter(),
        )
        self.assertIn(old_fact.text, old_context.envelope)
        self.assertNotIn(old_fact.text, new_context.envelope)
        self.assertIn("corrected debate history", new_context.envelope)

    def test_inputs_are_immutable_and_output_is_deterministic(self):
        supplied = [
            provided_item(
                "energy: steady",
                item_id="energy",
                layer=Layer.MEDIUM,
                resident_id="leo",
                source="resident-state",
                event_id="state-1",
            ),
            visitor_item("recent conversation", resident_id="leo", event_id="conversation-1"),
        ]
        before = list(supplied)
        first = self.assemble(medium=(supplied[0],), immediate=(supplied[1],))
        second = self.assemble(medium=(supplied[0],), immediate=(supplied[1],))
        self.assertEqual(first.envelope, second.envelope)
        self.assertEqual(first.units, SyntheticTestCounter().count(first.envelope))
        self.assertEqual(supplied, before)
        with self.assertRaises(FrozenInstanceError):
            supplied[0].text = "changed"

    def test_metadata_survives_serialization_and_private_denial_has_no_leak(self):
        state = provided_item(
            "commitment: prepare ingredients",
            item_id="commitment-1",
            layer=Layer.MEDIUM,
            resident_id="leo",
            source="resident-state",
            event_id="event-77",
            revision=4,
            visibility=Visibility.PRIVATE,
        )
        result = self.assemble(medium=(state,))
        payload = json.loads(result.envelope)
        selected = [
            item
            for layer in payload["layers"]
            for item in layer["items"]
            if item["item_id"] == "commitment-1"
        ][0]
        self.assertEqual(
            {selected[key] for key in ("resident_id", "source", "event_id", "revision", "trust", "visibility")},
            {"leo", "resident-state", "event-77", 4, "provided", "private"},
        )

        private_bob = provided_item(
            "secret bob memory",
            item_id="bob-private",
            layer=Layer.IMMEDIATE,
            resident_id="bob",
            source="bob-private-source",
            event_id="bob-event",
            visibility=Visibility.PRIVATE,
        )
        denied = self.assemble(immediate=(private_bob,))
        self.assertNotIn("secret bob memory", denied.envelope)
        self.assertNotIn("bob-private-source", denied.envelope)
        self.assertEqual(denied.layer(Layer.IMMEDIATE).items, ())
        self.assertTrue(any(omission.reason == "visibility" for omission in denied.omissions))

    def test_unknown_trust_visibility_and_explicit_policy_are_rejected(self):
        with self.assertRaises(PersonaSchemaError):
            ContextItem(
                item_id="bad-trust",
                layer=Layer.IMMEDIATE,
                text="text",
                resident_id="leo",
                source="source",
                event_id="event",
                revision=1,
                trust="not-a-trust",
                visibility=Visibility.PRIVATE,
                source_role=SourceRole.SYSTEM,
            )
        with self.assertRaises(PersonaSchemaError):
            VisibilityGrant("leo", "leo", "not-a-visibility")
        with self.assertRaises(PersonaSchemaError):
            assemble_context(
                self.registry,
                recipient_id="leo",
                visibility_policy=None,
                budgets=self.large_budgets,
                counter=SyntheticTestCounter(),
            )

    def test_cheap_bounds_run_before_counter_and_multibyte_size_is_exact(self):
        probe = CountingProbe()
        oversized = provided_item(
            "界" * 100,
            item_id="oversized",
            layer=Layer.IMMEDIATE,
            resident_id="leo",
            source="test",
            event_id="oversized-1",
        )
        budgets = ContextBudgets(
            high=100_000,
            medium=100_000,
            immediate=100_000,
            total=300_000,
            max_item_text_bytes=200,
        )
        with self.assertRaises(InputLimitError):
            self.assemble(budgets=budgets, counter=probe, immediate=(oversized,))
        self.assertEqual(probe.calls, 0)

        exact = "é" * 20
        exact_item = provided_item(
            exact,
            item_id="utf8-exact",
            layer=Layer.IMMEDIATE,
            resident_id="leo",
            source="test",
            event_id="utf8-1",
        )
        result = self.assemble(immediate=(exact_item,), counter=Utf8ByteCounter())
        self.assertIn(exact, result.envelope)
        self.assertEqual(result.units, len(result.envelope.encode("utf-8")))

    def test_item_count_is_bounded_before_serialization(self):
        probe = CountingProbe()
        items = tuple(
            visitor_item(f"event {number}", resident_id="leo", event_id=f"event-{number}")
            for number in range(4)
        )
        budgets = ContextBudgets(
            high=100_000,
            medium=100_000,
            immediate=100_000,
            total=300_000,
            max_items=3,
        )
        with self.assertRaises(InputLimitError):
            self.assemble(budgets=budgets, counter=probe, immediate=items)
        self.assertEqual(probe.calls, 0)

    def test_exact_layer_and_total_boundaries_fit(self):
        base = self.assemble()
        exact_budgets = ContextBudgets(
            high=base.layer(Layer.HIGH).units,
            medium=base.layer(Layer.MEDIUM).units,
            immediate=base.layer(Layer.IMMEDIATE).units,
            total=base.units,
        )
        exact = self.assemble(budgets=exact_budgets)
        self.assertEqual(exact.envelope, base.envelope)
        self.assertEqual(exact.units, exact_budgets.total)

        with self.assertRaises(ContextBudgetError) as layer_error:
            self.assemble(
                budgets=ContextBudgets(
                    high=base.layer(Layer.HIGH).units - 1,
                    medium=100_000,
                    immediate=100_000,
                    total=300_000,
                )
            )
        self.assertEqual(layer_error.exception.scope, "high")

        with self.assertRaises(ContextBudgetError) as total_error:
            self.assemble(
                budgets=ContextBudgets(
                    high=100_000,
                    medium=100_000,
                    immediate=100_000,
                    total=base.units - 1,
                )
            )
        self.assertEqual(total_error.exception.scope, "total")
        self.assertTrue(total_error.exception.mandatory)

    def test_optional_overflow_is_reported_and_full_envelope_is_counted(self):
        optional = provided_item(
            "plan: " + "x" * 200,
            item_id="optional-plan",
            layer=Layer.MEDIUM,
            resident_id="leo",
            source="resident-state",
            event_id="plan-1",
        )
        with_optional = self.assemble(medium=(optional,))
        limited_layer = self.assemble(
            medium=(optional,),
            budgets=ContextBudgets(
                high=100_000,
                medium=with_optional.layer(Layer.MEDIUM).units - 1,
                immediate=100_000,
                total=300_000,
            ),
        )
        self.assertNotIn("optional-plan", limited_layer.envelope)
        self.assertTrue(any(omission.reason == "layer_budget" for omission in limited_layer.omissions))

        base = self.assemble()
        total_limited = self.assemble(
            medium=(optional,),
            budgets=ContextBudgets(
                high=100_000,
                medium=100_000,
                immediate=100_000,
                total=with_optional.units - 1,
            ),
        )
        self.assertNotIn("optional-plan", total_limited.envelope)
        self.assertTrue(any(omission.reason == "total_budget" for omission in total_limited.omissions))
        self.assertLessEqual(total_limited.units, with_optional.units - 1)
        self.assertEqual(total_limited.units, SyntheticTestCounter().count(total_limited.envelope))
        self.assertLess(base.units, with_optional.units)

    def test_mandatory_approved_facts_cannot_be_silently_dropped(self):
        base = self.assemble()
        with self.assertRaises(ContextBudgetError) as error:
            self.assemble(
                budgets=ContextBudgets(
                    high=base.layer(Layer.HIGH).units - 1,
                    medium=100_000,
                    immediate=100_000,
                    total=300_000,
                )
            )
        self.assertTrue(error.exception.mandatory)
        self.assertIn("mandatory", str(error.exception))


if __name__ == "__main__":
    unittest.main()
