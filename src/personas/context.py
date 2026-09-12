"""Pure deterministic layered context assembly.

Assembly produces structured JSON text for a caller to inspect or pass on to a
later integration.  It never invokes a model, runs a prompt, parses arbitrary
text, or stores durable state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Protocol

from .registry import ApprovedPersonaRegistry
from .schema import (
    ApprovalBoundaryError,
    ContextBudgetError,
    ContextBudgets,
    ContextItem,
    InputLimitError,
    Layer,
    Omission,
    PersonaSchemaError,
    SourceRole,
    Trust,
    Visibility,
)


class CountingContract(Protocol):
    """Injected counter for serialized units; this is not a model tokenizer."""

    def count(self, serialized: str) -> int:
        """Return deterministic units for one complete serialized value."""


class SyntheticTestCounter:
    """Synthetic code-point counter for unit tests and local examples only.

    The returned number is deliberately not presented as model-token usage.
    Chosen-model tokenizer measurements belong to the parent/model-capacity
    work and are outside this pure library.
    """

    name = "synthetic-code-point-counter"

    def count(self, serialized: str) -> int:
        if not isinstance(serialized, str):
            raise PersonaSchemaError("counter input must be serialized text")
        return len(serialized)


class Utf8ByteCounter:
    """Deterministic UTF-8 byte counter useful for boundary tests, not tokens."""

    name = "synthetic-utf8-byte-counter"

    def count(self, serialized: str) -> int:
        if not isinstance(serialized, str):
            raise PersonaSchemaError("counter input must be serialized text")
        return len(serialized.encode("utf-8"))


@dataclass(frozen=True)
class VisibilityGrant:
    """One explicit recipient/resident/visibility permission."""

    recipient_id: str
    resident_id: str
    visibility: Visibility

    def __post_init__(self) -> None:
        for field_name in ("recipient_id", "resident_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise PersonaSchemaError(f"{field_name} must be a bounded non-empty string")
        if not isinstance(self.visibility, Visibility):
            try:
                object.__setattr__(self, "visibility", Visibility(self.visibility))
            except (TypeError, ValueError) as exc:
                raise PersonaSchemaError("visibility grant has an unknown value") from exc


class SyntheticVisibilityPolicy:
    """Explicit test-only visibility grants.

    No public/private behavior is inferred.  Every item requires an exact
    grant for its resident owner, recipient, and declared visibility.
    """

    __slots__ = ("_grants",)

    def __init__(self, grants: Iterable[VisibilityGrant]) -> None:
        grants = tuple(grants)
        if len(grants) > 256:
            raise InputLimitError("visibility grants exceed the hard limit of 256")
        if any(not isinstance(grant, VisibilityGrant) for grant in grants):
            raise PersonaSchemaError("visibility policy requires VisibilityGrant values")
        self._grants = frozenset(grants)

    @classmethod
    def for_tests(cls, *grants: VisibilityGrant) -> "SyntheticVisibilityPolicy":
        """Create a policy explicitly marked as a synthetic test fixture."""

        return cls(grants)

    @classmethod
    def allow_for_tests(
        cls,
        *,
        recipient_id: str,
        resident_id: str,
        visibility: Visibility,
    ) -> "SyntheticVisibilityPolicy":
        """Convenience helper; this is not launch privacy approval."""

        return cls.for_tests(
            VisibilityGrant(
                recipient_id=recipient_id,
                resident_id=resident_id,
                visibility=visibility,
            )
        )

    def allows(self, item: ContextItem, recipient_id: str) -> bool:
        return VisibilityGrant(
            recipient_id=recipient_id,
            resident_id=item.resident_id,
            visibility=item.visibility,
        ) in self._grants


@dataclass(frozen=True)
class LayerContext:
    """One selected layer and its fully wrapped serialized representation."""

    layer: Layer
    items: tuple[ContextItem, ...]
    serialized: str
    units: int


@dataclass(frozen=True)
class AssembledContext:
    """Deterministic result containing selected metadata and omission evidence."""

    recipient_id: str
    resident_id: str
    layers: tuple[LayerContext, ...]
    envelope: str
    units: int
    omissions: tuple[Omission, ...]

    def layer(self, layer: Layer) -> LayerContext:
        layer = Layer(layer)
        for selected in self.layers:
            if selected.layer is layer:
                return selected
        raise KeyError(layer.value)


def _counter_units(counter: CountingContract, serialized: str) -> int:
    count = getattr(counter, "count", None)
    if not callable(count):
        raise PersonaSchemaError("counter must implement count(serialized)")
    units = count(serialized)
    if not isinstance(units, int) or isinstance(units, bool) or units < 0:
        raise PersonaSchemaError("counter must return a non-negative integer")
    return units


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_collection(
    values: Iterable[ContextItem],
    *,
    layer: Layer,
    max_items: int,
    max_item_text_bytes: int,
) -> tuple[ContextItem, ...]:
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise PersonaSchemaError(f"{layer.value} inputs must be iterable ContextItem values") from exc

    collected: list[ContextItem] = []
    for _ in range(max_items + 1):
        try:
            item = next(iterator)
        except StopIteration:
            break
        if len(collected) >= max_items:
            raise InputLimitError(f"context item count exceeds {max_items}")
        if not isinstance(item, ContextItem):
            raise PersonaSchemaError("context inputs must be ContextItem values")
        if item.layer is not layer:
            raise PersonaSchemaError(
                f"context item {item.item_id!r} is in {item.layer.value}, expected {layer.value}"
            )
        # This cheap bound is intentionally checked before any injected counter
        # or full envelope serialization occurs.
        if len(item.text.encode("utf-8")) > max_item_text_bytes:
            raise InputLimitError(
                f"context item {item.item_id!r} exceeds {max_item_text_bytes} UTF-8 bytes"
            )
        if item.trust is Trust.APPROVED:
            raise ApprovalBoundaryError(
                "approved context inputs must be selected from the current registry"
            )
        collected.append(item)
    return tuple(collected)


def _item_sort_key(item: ContextItem) -> tuple[object, ...]:
    return (
        0 if item.mandatory else 1,
        item.item_id,
        item.revision,
        item.event_id,
        item.source,
        item.text,
    )


def _layer_payload(layer: Layer, items: tuple[ContextItem, ...]) -> dict[str, object]:
    return {
        "layer": layer.value,
        "items": [item.as_mapping() for item in items],
    }


def _render_layer(
    layer: Layer,
    items: tuple[ContextItem, ...],
    counter: CountingContract,
) -> LayerContext:
    serialized = _canonical(_layer_payload(layer, items))
    return LayerContext(
        layer=layer,
        items=items,
        serialized=serialized,
        units=_counter_units(counter, serialized),
    )


def _summarize_omissions(omissions: tuple[Omission, ...]) -> list[dict[str, object]]:
    grouped: dict[tuple[Layer, str], int] = {}
    for omission in omissions:
        key = (omission.layer, omission.reason)
        grouped[key] = grouped.get(key, 0) + omission.count
    layer_order = {Layer.HIGH: 0, Layer.MEDIUM: 1, Layer.IMMEDIATE: 2}
    return [
        {"count": count, "layer": layer.value, "reason": reason}
        for (layer, reason), count in sorted(
            grouped.items(), key=lambda entry: (layer_order[entry[0][0]], entry[0][1])
        )
    ]


def _envelope_payload(
    *,
    recipient_id: str,
    resident_id: str,
    selected: dict[Layer, tuple[ContextItem, ...]],
    omissions: tuple[Omission, ...],
) -> dict[str, object]:
    return {
        "layers": [
            _layer_payload(layer, selected[layer])
            for layer in (Layer.HIGH, Layer.MEDIUM, Layer.IMMEDIATE)
        ],
        "omissions": _summarize_omissions(omissions),
        "recipient_id": recipient_id,
        "resident_id": resident_id,
        "schema": "persona-context-core/v1",
    }


def _render_envelope(
    *,
    recipient_id: str,
    resident_id: str,
    selected: dict[Layer, tuple[ContextItem, ...]],
    omissions: tuple[Omission, ...],
    counter: CountingContract,
) -> tuple[str, int]:
    serialized = _canonical(
        _envelope_payload(
            recipient_id=recipient_id,
            resident_id=resident_id,
            selected=selected,
            omissions=omissions,
        )
    )
    return serialized, _counter_units(counter, serialized)


def _new_omission(omissions: list[Omission], layer: Layer, reason: str) -> None:
    omissions.append(Omission(layer=layer, reason=reason))


def assemble_context(
    registry: ApprovedPersonaRegistry,
    *,
    recipient_id: str,
    visibility_policy: SyntheticVisibilityPolicy,
    budgets: ContextBudgets,
    counter: CountingContract,
    high: Iterable[ContextItem] = (),
    medium: Iterable[ContextItem] = (),
    immediate: Iterable[ContextItem] = (),
) -> AssembledContext:
    """Select current approved facts and bounded state into three layers.

    Approved facts are always obtained from the current registry argument.
    Caller-provided values may supply structured schedule, mood, commitment,
    surroundings, event, or conversation state, but are never promoted to
    approved trust by their text or labels.
    """

    if not isinstance(registry, ApprovedPersonaRegistry):
        raise PersonaSchemaError("registry must be an ApprovedPersonaRegistry")
    if not isinstance(recipient_id, str) or not recipient_id or len(recipient_id) > 128:
        raise PersonaSchemaError("recipient_id must be a bounded non-empty string")
    if not isinstance(visibility_policy, SyntheticVisibilityPolicy):
        raise PersonaSchemaError(
            "assembly requires an explicit SyntheticVisibilityPolicy in this test-only slice"
        )
    if not isinstance(budgets, ContextBudgets):
        raise PersonaSchemaError("budgets must be ContextBudgets")

    raw_by_layer = {
        Layer.HIGH: high,
        Layer.MEDIUM: medium,
        Layer.IMMEDIATE: immediate,
    }
    supplied: dict[Layer, tuple[ContextItem, ...]] = {}
    for layer in (Layer.HIGH, Layer.MEDIUM, Layer.IMMEDIATE):
        supplied[layer] = _bounded_collection(
            raw_by_layer[layer],
            layer=layer,
            max_items=budgets.max_items,
            max_item_text_bytes=budgets.max_item_text_bytes,
        )

    approved = registry.context_items()
    for item in approved:
        # Registry facts are bounded too.  Check them before any counter call,
        # just as for caller-provided inputs.
        if len(item.text.encode("utf-8")) > budgets.max_item_text_bytes:
            raise InputLimitError(
                f"registry item {item.item_id!r} exceeds {budgets.max_item_text_bytes} UTF-8 bytes"
            )
    if len(approved) + sum(len(items) for items in supplied.values()) > budgets.max_items:
        raise InputLimitError(f"context item count exceeds {budgets.max_items}")

    candidates: dict[Layer, tuple[ContextItem, ...]] = {
        Layer.HIGH: approved + supplied[Layer.HIGH],
        Layer.MEDIUM: supplied[Layer.MEDIUM],
        Layer.IMMEDIATE: supplied[Layer.IMMEDIATE],
    }
    seen_ids: set[str] = set()
    visible: dict[Layer, list[ContextItem]] = {
        Layer.HIGH: [],
        Layer.MEDIUM: [],
        Layer.IMMEDIATE: [],
    }
    omissions: list[Omission] = []
    for layer in (Layer.HIGH, Layer.MEDIUM, Layer.IMMEDIATE):
        for item in candidates[layer]:
            if item.item_id in seen_ids:
                raise PersonaSchemaError(f"duplicate context item id {item.item_id!r}")
            seen_ids.add(item.item_id)
            allowed = visibility_policy.allows(item, recipient_id)
            if not isinstance(allowed, bool):
                raise PersonaSchemaError("visibility policy must return boolean")
            if not allowed:
                _new_omission(omissions, layer, "visibility")
                continue
            visible[layer].append(item)

    selected: dict[Layer, tuple[ContextItem, ...]] = {
        Layer.HIGH: (),
        Layer.MEDIUM: (),
        Layer.IMMEDIATE: (),
    }
    for layer in (Layer.HIGH, Layer.MEDIUM, Layer.IMMEDIATE):
        chosen: list[ContextItem] = []
        for item in sorted(visible[layer], key=_item_sort_key):
            candidate = tuple(chosen + [item])
            rendered = _render_layer(layer, candidate, counter)
            if rendered.units <= budgets.for_layer(layer):
                chosen.append(item)
                continue
            if item.mandatory:
                raise ContextBudgetError(
                    f"mandatory {layer.value} context item {item.item_id!r} exceeds its layer budget",
                    scope=layer.value,
                    required=rendered.units,
                    budget=budgets.for_layer(layer),
                    mandatory=True,
                )
            _new_omission(omissions, layer, "layer_budget")
        selected[layer] = tuple(chosen)

    # A layer can fit independently while the complete envelope cannot.  Drop
    # optional state by lowest priority, re-rendering the complete envelope
    # after each change so wrappers and omission evidence are counted too.
    mutable_selected = {layer: list(items) for layer, items in selected.items()}
    omission_tuple = tuple(omissions)
    envelope, envelope_units = _render_envelope(
        recipient_id=recipient_id,
        resident_id=registry.resident_id,
        selected={layer: tuple(items) for layer, items in mutable_selected.items()},
        omissions=omission_tuple,
        counter=counter,
    )
    drop_order = (Layer.IMMEDIATE, Layer.MEDIUM, Layer.HIGH)
    while envelope_units > budgets.total:
        drop: tuple[Layer, int] | None = None
        for layer in drop_order:
            for index in range(len(mutable_selected[layer]) - 1, -1, -1):
                if not mutable_selected[layer][index].mandatory:
                    drop = (layer, index)
                    break
            if drop is not None:
                break
        if drop is None:
            raise ContextBudgetError(
                "mandatory context envelope exceeds the total budget",
                scope="total",
                required=envelope_units,
                budget=budgets.total,
                mandatory=True,
            )
        layer, index = drop
        mutable_selected[layer].pop(index)
        _new_omission(omissions, layer, "total_budget")
        omission_tuple = tuple(omissions)
        envelope, envelope_units = _render_envelope(
            recipient_id=recipient_id,
            resident_id=registry.resident_id,
            selected={layer: tuple(items) for layer, items in mutable_selected.items()},
            omissions=omission_tuple,
            counter=counter,
        )

    final_selected = {layer: tuple(items) for layer, items in mutable_selected.items()}
    layers = tuple(
        _render_layer(layer, final_selected[layer], counter)
        for layer in (Layer.HIGH, Layer.MEDIUM, Layer.IMMEDIATE)
    )
    # Re-render once from the exact final selected values.  This is the counted
    # envelope returned to the caller, including all metadata and wrappers.
    envelope, envelope_units = _render_envelope(
        recipient_id=recipient_id,
        resident_id=registry.resident_id,
        selected=final_selected,
        omissions=tuple(omissions),
        counter=counter,
    )
    if envelope_units > budgets.total:
        raise ContextBudgetError(
            "mandatory context envelope exceeds the total budget",
            scope="total",
            required=envelope_units,
            budget=budgets.total,
            mandatory=True,
        )
    return AssembledContext(
        recipient_id=recipient_id,
        resident_id=registry.resident_id,
        layers=layers,
        envelope=envelope,
        units=envelope_units,
        omissions=tuple(omissions),
    )
