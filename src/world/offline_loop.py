"""Offline, private, synthetic one-resident world loop.

Contract
--------
This module demonstrates one bounded decision cycle for a single resident:

    world state -> persona context -> adapter wire request -> fake transport
    -> adapter JSON parsing -> untrusted proposal -> engine validation
    -> new versioned state + event (or a rejection with the state unchanged)

The world is deliberately tiny: one resident at one of three places
(``well``, ``herb-bed``, ``elsewhere``), one water can with a finite capacity,
one well that may be unavailable, and one herb bed.  The actions are ``move``
(target: another place), ``refill`` (target ``well``, only while at the well),
``water`` (target ``herb-bed``, only while at the bed), ``wait`` (no target),
and ``talk`` (target ``visitor``).  Moving is one direct step between named
places; there is no pathfinding, travel time, or schedule.  ``talk`` records
only that the action occurred; no reply text or transcript is kept.

Offers and validation share one pure precondition check.  The context lists
only actions that are currently possible for the resident's capabilities,
place, and state.  The engine re-runs the same check at commit time and never
trusts the offer list, so a stale or unoffered choice cannot mutate state.

The engine is authoritative.  A model reply supplies only an action name and
target; the actor is bound from the assembled persona context and the state
version is bound by the loop.  Proposals are accepted only when created by
this module from a successful, settled adapter result.  That origin marker is
an internal Python convention, like the persona registry marker, and not a
sandbox against arbitrary Python code.

Fake versus real
----------------
``run_offline_turn`` refuses a missing transport and the real
``UrllibTransport``.  Every accepted event is labelled
``InferenceMode.OFFLINE_FAKE_FIXTURE``: the model reply came from a caller's
offline fixture, not from real inference, and the permit is synthetic rather
than real quota enforcement.  Real inference, provider disclosure of persona
context, durable admission accounting, world time or downtime catch-up, shared
or multi-resident worlds, and any public endpoint remain deferred launch gates.

State and events are pure immutable values.  Nothing here stores data, reads
the environment, opens sockets, or runs on a clock of its own.  Durable
storage of one world and its events lives in ``world.postgres``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Callable, Iterable

from inference.openrouter import (
    AccountingPermit,
    CompletionResult,
    CredentialSupplier,
    HttpTransport,
    MonotonicClock,
    UrllibTransport,
    complete,
)
from personas import (
    ApprovedPersonaRegistry,
    AssembledContext,
    ContextBudgets,
    ContextItem,
    CountingContract,
    Layer,
    SourceRole,
    SyntheticVisibilityPolicy,
    Visibility,
    assemble_context,
    provided_item,
)


SCHEMA = "offline-world/v2"
WELL = "well"
HERB_BED = "herb-bed"
ELSEWHERE = "elsewhere"
VISITOR = "visitor"
LOCATIONS = (ELSEWHERE, HERB_BED, WELL)
MAX_IDENTIFIER_LENGTH = 128
MAX_CAN_CAPACITY = 100
MAX_WORLD_VERSION = 4_096
WORLD_STATE_ITEM_ID = "offline-world-state"

# Each action and the targets it can ever accept; preconditions narrow these.
ACTION_TARGETS: dict[str, tuple[str | None, ...]] = {
    "move": LOCATIONS,
    "refill": (WELL,),
    "talk": (VISITOR,),
    "wait": (None,),
    "water": (HERB_BED,),
}
ACTIONS = frozenset(ACTION_TARGETS)
# Where the resident must stand for a place-bound action.
REQUIRED_LOCATION: dict[str, str] = {"refill": WELL, "water": HERB_BED}

# Internal convention marking proposals produced by the adapter path below.
_ADAPTER_ORIGIN = object()


class WorldError(ValueError):
    """A world value is malformed or out of bounds."""


class ReplayError(WorldError):
    """An event log does not deterministically reproduce its recorded states."""


class OfflineOnlyError(WorldError):
    """The offline loop was asked to use a real or missing transport."""


class InferenceMode(str, Enum):
    """Where a proposal came from; only the offline fixture exists here."""

    OFFLINE_FAKE_FIXTURE = "offline-fake-fixture"


class RejectionReason(str, Enum):
    INFERENCE_FAILED = "inference_failed"
    PROVENANCE_REJECTED = "provenance_rejected"
    STALE_VERSION = "stale_version"
    ACTOR_MISMATCH = "actor_mismatch"
    UNKNOWN_ACTION = "unknown_action"
    CAPABILITY_DENIED = "capability_denied"
    INVALID_TARGET = "invalid_target"
    ALREADY_THERE = "already_there"
    WRONG_LOCATION = "wrong_location"
    WELL_UNAVAILABLE = "well_unavailable"
    CAN_ALREADY_FULL = "can_already_full"
    CAN_EMPTY = "can_empty"
    BED_ALREADY_WATERED = "bed_already_watered"
    VERSION_EXHAUSTED = "version_exhausted"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise WorldError(f"{field_name} must be a bounded non-empty string")
    return value


def _integer(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorldError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise WorldError(f"{field_name} must be within {minimum}..{maximum}")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise WorldError(f"{field_name} must be boolean")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Resident:
    """The single resident's identity and granted action capabilities."""

    resident_id: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        _identifier(self.resident_id, "resident_id")
        capabilities = self.capabilities
        if isinstance(capabilities, str):
            raise WorldError("capabilities must be a collection of action names")
        capabilities = frozenset(capabilities)
        if not capabilities <= ACTIONS:
            raise WorldError("capabilities must name allowed actions")
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True)
class WorldState:
    """One immutable, versioned snapshot of the synthetic world."""

    version: int
    resident: Resident
    location: str
    can_capacity: int
    can_level: int
    well_available: bool
    herb_bed_watered: bool

    def __post_init__(self) -> None:
        _integer(self.version, "version", 0, MAX_WORLD_VERSION)
        if not isinstance(self.resident, Resident):
            raise WorldError("resident must be a Resident")
        if self.location not in LOCATIONS:
            raise WorldError("location must be a known place")
        _integer(self.can_capacity, "can_capacity", 1, MAX_CAN_CAPACITY)
        _integer(self.can_level, "can_level", 0, self.can_capacity)
        _boolean(self.well_available, "well_available")
        _boolean(self.herb_bed_watered, "herb_bed_watered")

    def as_mapping(self) -> dict[str, object]:
        return {
            "herb_bed": {"watered": self.herb_bed_watered},
            "resident": {
                "capabilities": sorted(self.resident.capabilities),
                "location": self.location,
                "resident_id": self.resident.resident_id,
            },
            "schema": SCHEMA,
            "version": self.version,
            "water_can": {"capacity": self.can_capacity, "level": self.can_level},
            "well": {"available": self.well_available},
        }

    @property
    def digest(self) -> str:
        return _sha256(_canonical(self.as_mapping()))


def _exact_mapping(value: object, keys: frozenset[str], field_name: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorldError(f"{field_name} must be an object with exactly {sorted(keys)}")
    return value


def state_from_mapping(value: object) -> WorldState:
    """Strictly decode ``WorldState.as_mapping`` output, e.g. stored JSON.

    Only the exact known keys and JSON types are accepted.  Nothing is
    defaulted, coerced, or evaluated; ``WorldState`` re-checks every bound
    (so ``True`` is not an integer and ``1`` is not a boolean).
    """

    root = _exact_mapping(
        value,
        frozenset({"herb_bed", "resident", "schema", "version", "water_can", "well"}),
        "world",
    )
    if root["schema"] != SCHEMA:
        raise WorldError("world schema is not supported")
    resident = _exact_mapping(
        root["resident"], frozenset({"capabilities", "location", "resident_id"}), "resident"
    )
    capabilities = resident["capabilities"]
    if (
        not isinstance(capabilities, list)
        or not all(isinstance(name, str) for name in capabilities)
        or capabilities != sorted(set(capabilities))
    ):
        raise WorldError("capabilities must be a sorted list of unique action names")
    if not isinstance(resident["location"], str):
        raise WorldError("location must be a known place")
    can = _exact_mapping(root["water_can"], frozenset({"capacity", "level"}), "water_can")
    well = _exact_mapping(root["well"], frozenset({"available"}), "well")
    bed = _exact_mapping(root["herb_bed"], frozenset({"watered"}), "herb_bed")
    return WorldState(
        version=root["version"],
        resident=Resident(resident["resident_id"], frozenset(capabilities)),
        location=resident["location"],
        can_capacity=can["capacity"],
        can_level=can["level"],
        well_available=well["available"],
        herb_bed_watered=bed["watered"],
    )


def initial_state(
    *,
    resident_id: str = "leo",
    location: str = ELSEWHERE,
    can_capacity: int = 3,
    can_level: int = 0,
    well_available: bool = True,
    herb_bed_watered: bool = False,
    capabilities: Iterable[str] = ACTIONS,
) -> WorldState:
    """Build a synthetic version-zero world; it asserts no real biography."""

    return WorldState(
        version=0,
        resident=Resident(resident_id, frozenset(capabilities)),
        location=location,
        can_capacity=can_capacity,
        can_level=can_level,
        well_available=well_available,
        herb_bed_watered=herb_bed_watered,
    )


@dataclass(frozen=True)
class Provenance:
    """How an untrusted proposal reached the engine."""

    inference_mode: InferenceMode
    context_sha256: str
    accounting_state: str


@dataclass(frozen=True)
class ActionProposal:
    """An untrusted proposal: model-chosen action/target, loop-bound actor."""

    actor_id: str
    action: str
    target: str | None
    based_on_version: int
    provenance: Provenance
    _origin: object | None = field(default=None, repr=False, compare=False, hash=False)


@dataclass(frozen=True)
class WorldEvent:
    """One committed action.  Rejections never produce events."""

    event_id: str
    version: int
    prior_version: int
    actor_id: str
    action: str
    target: str | None
    inference_mode: InferenceMode
    context_sha256: str
    state_sha256: str


@dataclass(frozen=True)
class Decision:
    """The engine's authoritative answer to one proposal."""

    accepted: bool
    state: WorldState
    event: WorldEvent | None
    reason: RejectionReason | None


def _reject(state: WorldState, reason: RejectionReason) -> Decision:
    return Decision(accepted=False, state=state, event=None, reason=reason)


def _precondition(
    state: WorldState, actor_id: str, action: str, target: str | None
) -> RejectionReason | None:
    """The single pure check shared by offer generation and commit validation."""

    if actor_id != state.resident.resident_id:
        return RejectionReason.ACTOR_MISMATCH
    if state.version >= MAX_WORLD_VERSION:
        return RejectionReason.VERSION_EXHAUSTED
    if action not in ACTIONS:
        return RejectionReason.UNKNOWN_ACTION
    if action not in state.resident.capabilities:
        return RejectionReason.CAPABILITY_DENIED
    if target not in ACTION_TARGETS[action]:
        return RejectionReason.INVALID_TARGET
    if action == "move" and target == state.location:
        return RejectionReason.ALREADY_THERE
    if action in REQUIRED_LOCATION and state.location != REQUIRED_LOCATION[action]:
        return RejectionReason.WRONG_LOCATION
    if action == "refill":
        if not state.well_available:
            return RejectionReason.WELL_UNAVAILABLE
        if state.can_level == state.can_capacity:
            return RejectionReason.CAN_ALREADY_FULL
    elif action == "water":
        if state.can_level == 0:
            return RejectionReason.CAN_EMPTY
        if state.herb_bed_watered:
            return RejectionReason.BED_ALREADY_WATERED
    return None


def possible_actions(state: WorldState) -> tuple[tuple[str, str | None], ...]:
    """Every (action, target) the resident could commit right now, in order."""

    if not isinstance(state, WorldState):
        raise WorldError("state must be a WorldState")
    actor_id = state.resident.resident_id
    return tuple(
        (action, target)
        for action in sorted(state.resident.capabilities)
        for target in ACTION_TARGETS[action]
        if _precondition(state, actor_id, action, target) is None
    )


def _transition(
    state: WorldState, actor_id: str, action: str, target: str | None
) -> WorldState | RejectionReason:
    """Re-check preconditions authoritatively, then derive the next state."""

    reason = _precondition(state, actor_id, action, target)
    if reason is not None:
        return reason
    location = state.location
    can_level = state.can_level
    watered = state.herb_bed_watered
    if action == "move":
        location = target
    elif action == "refill":
        can_level = state.can_capacity
    elif action == "water":
        can_level -= 1
        watered = True
    return WorldState(
        version=state.version + 1,
        resident=state.resident,
        location=location,
        can_capacity=state.can_capacity,
        can_level=can_level,
        well_available=state.well_available,
        herb_bed_watered=watered,
    )


def apply_proposal(state: WorldState, proposal: ActionProposal) -> Decision:
    """Validate an untrusted proposal and commit it, or reject without change."""

    if not isinstance(state, WorldState):
        raise WorldError("state must be a WorldState")
    if (
        not isinstance(proposal, ActionProposal)
        or proposal._origin is not _ADAPTER_ORIGIN
        or not isinstance(proposal.provenance, Provenance)
        or proposal.provenance.inference_mode is not InferenceMode.OFFLINE_FAKE_FIXTURE
        or proposal.provenance.accounting_state != "settled_completed"
    ):
        return _reject(state, RejectionReason.PROVENANCE_REJECTED)
    if proposal.based_on_version != state.version:
        return _reject(state, RejectionReason.STALE_VERSION)

    outcome = _transition(state, proposal.actor_id, proposal.action, proposal.target)
    if isinstance(outcome, RejectionReason):
        return _reject(state, outcome)
    event = WorldEvent(
        event_id=f"world-event-{outcome.version}",
        version=outcome.version,
        prior_version=state.version,
        actor_id=proposal.actor_id,
        action=proposal.action,
        target=proposal.target,
        inference_mode=proposal.provenance.inference_mode,
        context_sha256=proposal.provenance.context_sha256,
        state_sha256=outcome.digest,
    )
    return Decision(accepted=True, state=outcome, event=event, reason=None)


def replay(initial: WorldState, events: Iterable[WorldEvent]) -> WorldState:
    """Re-derive state from an event log, verifying every recorded digest."""

    if not isinstance(initial, WorldState):
        raise ReplayError("initial must be a WorldState")
    state = initial
    for count, event in enumerate(events, start=1):
        if count > MAX_WORLD_VERSION:
            raise ReplayError(f"event log exceeds {MAX_WORLD_VERSION} events")
        if not isinstance(event, WorldEvent):
            raise ReplayError("event log must contain WorldEvent values")
        if (
            event.prior_version != state.version
            or event.version != state.version + 1
            or event.event_id != f"world-event-{event.version}"
            or event.inference_mode is not InferenceMode.OFFLINE_FAKE_FIXTURE
        ):
            raise ReplayError("event sequence mismatch")
        outcome = _transition(state, event.actor_id, event.action, event.target)
        if isinstance(outcome, RejectionReason) or outcome.digest != event.state_sha256:
            raise ReplayError("event does not reproduce its recorded state")
        state = outcome
    return state


def world_state_item(state: WorldState, *, resident_id: str) -> ContextItem:
    """Serialize world state and currently possible offers as context.

    The offers are advice for the model only; the engine re-validates any
    choice at commit and does not consult this list.
    """

    payload = {
        "offered_actions": [
            {"action": action, "target": target}
            for action, target in possible_actions(state)
        ],
        "reply_format": {"action": "string", "reply": "string", "target": "string|null"},
        "world": state.as_mapping(),
    }
    return provided_item(
        _canonical(payload),
        item_id=WORLD_STATE_ITEM_ID,
        layer=Layer.IMMEDIATE,
        resident_id=resident_id,
        source="offline-world-engine",
        event_id=f"world-state-v{state.version}",
        revision=state.version + 1,
        visibility=Visibility.PRIVATE,
        source_role=SourceRole.SYSTEM,
        mandatory=True,
    )


def _proposal_from_completion(
    result: CompletionResult,
    *,
    context: AssembledContext,
    state: WorldState,
) -> ActionProposal | None:
    if (
        not isinstance(result, CompletionResult)
        or not result.ok
        or not isinstance(result.action, str)
    ):
        return None
    return ActionProposal(
        actor_id=context.resident_id,
        action=result.action,
        target=result.target,
        based_on_version=state.version,
        provenance=Provenance(
            inference_mode=InferenceMode.OFFLINE_FAKE_FIXTURE,
            context_sha256=_sha256(context.envelope),
            accounting_state=result.accounting_state,
        ),
        _origin=_ADAPTER_ORIGIN,
    )


@dataclass(frozen=True)
class TurnResult:
    """Everything one offline turn produced, for inspection by tests."""

    context: AssembledContext
    completion: CompletionResult
    proposal: ActionProposal | None
    decision: Decision


def run_offline_turn(
    state: WorldState,
    registry: ApprovedPersonaRegistry,
    *,
    recipient_id: str,
    visibility_policy: SyntheticVisibilityPolicy,
    budgets: ContextBudgets,
    counter: CountingContract,
    visitor_text: str,
    transport: HttpTransport,
    permit: AccountingPermit,
    credential_supplier: CredentialSupplier | Callable[[], str],
    clock: MonotonicClock,
) -> TurnResult:
    """Run one decision through the real context and adapter code offline.

    The transport must be a caller-supplied offline fake; the real urllib
    transport is refused so that no event can be mislabelled as fake.
    """

    if not isinstance(state, WorldState):
        raise WorldError("state must be a WorldState")
    if transport is None or isinstance(transport, UrllibTransport):
        raise OfflineOnlyError("the offline loop requires an injected offline transport")

    context = assemble_context(
        registry,
        recipient_id=recipient_id,
        visibility_policy=visibility_policy,
        budgets=budgets,
        counter=counter,
        immediate=(world_state_item(state, resident_id=registry.resident_id),),
    )
    completion = complete(
        context.envelope,
        visitor_text,
        credential_supplier=credential_supplier,
        permit=permit,
        clock=clock,
        transport=transport,
    )
    proposal = _proposal_from_completion(completion, context=context, state=state)
    if proposal is None:
        decision = _reject(state, RejectionReason.INFERENCE_FAILED)
    else:
        decision = apply_proposal(state, proposal)
    return TurnResult(
        context=context,
        completion=completion,
        proposal=proposal,
        decision=decision,
    )
