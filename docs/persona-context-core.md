# Persona context core

This is a small standard-library-only library for producing an immutable,
bounded context value. It does not call a model, execute prompt text, persist
memory, run schedules, or start an autonomous world.

## Approved facts

`ApprovedPersonaRegistry.default()` contains exactly five stable facet profiles:

| Facet | Supplied facts |
| --- | --- |
| Mayor | debate/leadership history |
| Engineer | coding/building obsession; some high-school robotics |
| Teacher | knowledgeable; teaches math; understanding and patient |
| Chef | likes cooking; seeks new ingredients |
| Artsy | listens to music; plays piano; likes anime; strong aesthetic taste |

The registry exposes `facts` as a tuple of immutable revision-one values. Each
profile explicitly has unknown additional biography, schedule, and goals.
Nothing in this package invents dates, degrees, achievements, preferences,
schedules, or opinions.

`supersede()`/`correct()` requires the registry's explicit
`synthetic_authority_for_tests()` handle and returns a new registry snapshot.
The old snapshot is unchanged; its current view excludes the superseded
revision. This authority handle is a test seam, not launch approval. The
marker and authority are trusted internal Python conventions, not a sandbox
against arbitrary Python code. An owner approval service still needs to replace
the helper before production integration.

Visitor text is created with `visitor_item()` and remains `untrusted` in
IMMEDIATE or MEDIUM state. The assembler rejects caller-supplied approved
items; setting a string label such as `approved` does not make caller text
eligible for assembly. The current registry snapshot supplies approved items
to the assembler. Its Python marker is an internal convention, not a sandbox
against arbitrary Python code. Arbitrary text is never parsed into biography
or instructions.

## Layered assembly

`assemble_context()` always selects current registry facts into HIGH. Caller
provided `provided_item()` values may supply explicitly structured schedule or
goals in HIGH, mood/energy/plans/commitments/relationships in MEDIUM, and
surroundings/recent events/conversation in IMMEDIATE. Their trust and source
role remain `provided` or `untrusted`.

Every selected item retains `resident_id`, `source`, `event_id`, `revision`,
`trust`, `visibility`, `source_role`, and its layer in the serialized envelope.
The canonical envelope has stable JSON key ordering and stable HIGH, MEDIUM,
IMMEDIATE ordering. Inputs are copied into tuples and are never mutated.

Assembly requires an explicit `SyntheticVisibilityPolicy` made from exact
`VisibilityGrant` values. There is no inferred public default. A private or
resident optional item without a grant is omitted and never downgraded to
public; a denied mandatory item raises the generic
`MandatoryContextUnavailable` error before an envelope is returned. The error
contains no denied value or metadata, and the optional privacy-safe omission
report contains only layer/reason/count. These policy helpers are synthetic
fixtures for unit tests, not a privacy launch decision. All supplied items must
also match the registry resident; foreign records raise the generic
`ResidentScopeError`, even if a synthetic grant exists.

`ContextBudgets` supplies finite per-layer and total limits. The assembler
checks item count and UTF-8 text size before invoking the injected
`CountingContract`. It then counts the full wrapped layer and envelope strings,
including JSON metadata, separators, schema, and omission wrappers. Exact
boundary values fit; values over a limit omit optional items with a deterministic
report. Mandatory approved facts or a mandatory envelope that cannot fit raise
`ContextBudgetError`; nothing is silently truncated or altered.

Tests use `SyntheticTestCounter` (code points) or `Utf8ByteCounter` (bytes).
Both are deliberately named synthetic counters and are not real model-token
measurement. Chosen-model tokenization, model capacity, persistent memory,
retention, and production privacy integration remain LEO-165/LEO-166/LEO-131
work.

Run the isolated unit suite with:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests/personas -v
```
