"""Immutable value types for the pure persona/context core.

This module deliberately has no application, database, or model imports.  The
types describe provenance and trust; they do not execute text as instructions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar


MAX_IDENTIFIER_LENGTH = 128
MAX_SOURCE_LENGTH = 256
MAX_EVENT_ID_LENGTH = 128
MAX_ITEM_TEXT_BYTES = 16_384


class PersonaSchemaError(ValueError):
    """An input is not a well-formed persona/context value."""


class ApprovalBoundaryError(PersonaSchemaError):
    """A caller tried to construct or promote an approved fact directly."""


class InputLimitError(PersonaSchemaError):
    """An input exceeded a cheap, pre-assembly bound."""


class ContextBudgetError(ValueError):
    """A mandatory context value cannot fit the requested finite budget."""

    def __init__(
        self,
        message: str,
        *,
        scope: str,
        required: int,
        budget: int,
        mandatory: bool,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.required = required
        self.budget = budget
        self.mandatory = mandatory


class Facet(str, Enum):
    MAYOR = "mayor"
    ENGINEER = "engineer"
    TEACHER = "teacher"
    CHEF = "chef"
    ARTSY = "artsy"


class Layer(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    IMMEDIATE = "immediate"


class Trust(str, Enum):
    APPROVED = "approved"
    PROVIDED = "provided"
    UNTRUSTED = "untrusted"


class Visibility(str, Enum):
    PUBLIC = "public"
    RESIDENT = "resident"
    PRIVATE = "private"


class SourceRole(str, Enum):
    REGISTRY = "registry"
    RESIDENT = "resident"
    SYSTEM = "system"
    VISITOR = "visitor"
    SYNTHETIC_TEST = "synthetic_test"


EnumT = TypeVar("EnumT", bound=Enum)


def _enum_value(value: Any, enum_type: type[EnumT], field_name: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise PersonaSchemaError(
                f"{field_name} has unknown value {value!r}"
            ) from exc
    raise PersonaSchemaError(f"{field_name} must be a known enum value")


def _bounded_string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise PersonaSchemaError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise InputLimitError(f"{field_name} exceeds {maximum} characters")
    return value


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise PersonaSchemaError("text must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_ITEM_TEXT_BYTES:
        raise InputLimitError(
            f"text exceeds the hard limit of {MAX_ITEM_TEXT_BYTES} UTF-8 bytes"
        )
    return value


@dataclass(frozen=True)
class VersionedFact:
    """One approved registry version.

    Instances are created by :class:`ApprovedPersonaRegistry`.  A revision
    replaces the prior revision with the same ``fact_id`` in the active view;
    the prior value remains available only through registry history.
    """

    fact_id: str
    resident_id: str
    facet: Facet
    text: str
    source: str
    event_id: str
    revision: int
    trust: Trust = Trust.APPROVED
    visibility: Visibility = Visibility.RESIDENT
    source_role: SourceRole = SourceRole.REGISTRY
    supersedes_revision: int | None = None
    _registry_marker: object | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fact_id", _bounded_string(self.fact_id, "fact_id", MAX_IDENTIFIER_LENGTH)
        )
        object.__setattr__(
            self,
            "resident_id",
            _bounded_string(self.resident_id, "resident_id", MAX_IDENTIFIER_LENGTH),
        )
        object.__setattr__(self, "facet", _enum_value(self.facet, Facet, "facet"))
        object.__setattr__(self, "text", _bounded_text(self.text))
        object.__setattr__(
            self, "source", _bounded_string(self.source, "source", MAX_SOURCE_LENGTH)
        )
        object.__setattr__(
            self,
            "event_id",
            _bounded_string(self.event_id, "event_id", MAX_EVENT_ID_LENGTH),
        )
        object.__setattr__(self, "trust", _enum_value(self.trust, Trust, "trust"))
        object.__setattr__(
            self, "visibility", _enum_value(self.visibility, Visibility, "visibility")
        )
        object.__setattr__(
            self,
            "source_role",
            _enum_value(self.source_role, SourceRole, "source_role"),
        )
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise PersonaSchemaError("revision must be an integer")
        if self.revision < 1:
            raise PersonaSchemaError("revision must be positive")
        if (
            self.trust is not Trust.APPROVED
            or self.source_role is not SourceRole.REGISTRY
            or self._registry_marker is None
        ):
            raise ApprovalBoundaryError(
                "versioned facts are approved only when issued by the registry"
            )
        if self.supersedes_revision is not None:
            if (
                not isinstance(self.supersedes_revision, int)
                or self.supersedes_revision < 1
                or self.supersedes_revision >= self.revision
            ):
                raise PersonaSchemaError("supersedes_revision must precede revision")


@dataclass(frozen=True)
class PersonaProfile:
    """A facet plus explicit unknown biography and schedule fields."""

    facet: Facet
    facts: tuple[VersionedFact, ...]
    biography_unknown: bool = True
    schedule: tuple[str, ...] | None = None
    goals: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "facet", _enum_value(self.facet, Facet, "facet"))
        facts = tuple(self.facts)
        if any(not isinstance(fact, VersionedFact) for fact in facts):
            raise PersonaSchemaError("profile facts must be VersionedFact values")
        if any(fact.facet is not self.facet for fact in facts):
            raise PersonaSchemaError("profile facts must belong to the profile facet")
        object.__setattr__(self, "facts", facts)
        if not isinstance(self.biography_unknown, bool):
            raise PersonaSchemaError("biography_unknown must be boolean")
        for field_name in ("schedule", "goals"):
            value = getattr(self, field_name)
            if value is not None:
                value = tuple(value)
                if any(not isinstance(entry, str) or not entry for entry in value):
                    raise PersonaSchemaError(f"{field_name} entries must be text")
                object.__setattr__(self, field_name, value)

    @property
    def schedule_unknown(self) -> bool:
        return self.schedule is None

    @property
    def goals_unknown(self) -> bool:
        return self.goals is None


@dataclass(frozen=True)
class ContextItem:
    """Structured context with provenance retained beside its text.

    ``Trust.APPROVED`` cannot be constructed by a caller.  The registry uses
    the private marker when it creates current approved items, and the context
    assembler obtains those items directly from the current registry snapshot.
    """

    item_id: str
    layer: Layer
    text: str
    resident_id: str
    source: str
    event_id: str
    revision: int
    trust: Trust
    visibility: Visibility
    source_role: SourceRole
    mandatory: bool = False
    facet: Facet | None = None
    _registry_marker: object | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _bounded_string(self.item_id, "item_id", MAX_IDENTIFIER_LENGTH))
        object.__setattr__(self, "layer", _enum_value(self.layer, Layer, "layer"))
        object.__setattr__(self, "text", _bounded_text(self.text))
        object.__setattr__(
            self,
            "resident_id",
            _bounded_string(self.resident_id, "resident_id", MAX_IDENTIFIER_LENGTH),
        )
        object.__setattr__(
            self, "source", _bounded_string(self.source, "source", MAX_SOURCE_LENGTH)
        )
        object.__setattr__(
            self,
            "event_id",
            _bounded_string(self.event_id, "event_id", MAX_EVENT_ID_LENGTH),
        )
        object.__setattr__(self, "trust", _enum_value(self.trust, Trust, "trust"))
        object.__setattr__(
            self, "visibility", _enum_value(self.visibility, Visibility, "visibility")
        )
        object.__setattr__(
            self,
            "source_role",
            _enum_value(self.source_role, SourceRole, "source_role"),
        )
        if self.facet is not None:
            object.__setattr__(self, "facet", _enum_value(self.facet, Facet, "facet"))
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise PersonaSchemaError("revision must be an integer")
        if self.revision < 1:
            raise PersonaSchemaError("revision must be positive")
        if not isinstance(self.mandatory, bool):
            raise PersonaSchemaError("mandatory must be boolean")

        if self.trust is Trust.APPROVED:
            if self._registry_marker is None or self.source_role is not SourceRole.REGISTRY:
                raise ApprovalBoundaryError(
                    "approved context items must come from the approved registry"
                )
            if not self.mandatory or self.layer is not Layer.HIGH:
                raise ApprovalBoundaryError(
                    "approved registry facts are mandatory HIGH context"
                )
            if self.facet is None:
                raise PersonaSchemaError("approved facts require a facet")
        elif self._registry_marker is not None:
            raise ApprovalBoundaryError("registry provenance cannot be attached to unapproved text")
        elif self.source_role is SourceRole.REGISTRY:
            raise ApprovalBoundaryError("registry source role is reserved for approved facts")

        if self.layer is Layer.HIGH and self.trust is Trust.UNTRUSTED:
            raise ApprovalBoundaryError("untrusted visitor text cannot enter HIGH context")
        if self.source_role is SourceRole.VISITOR and self.trust is not Trust.UNTRUSTED:
            raise ApprovalBoundaryError("visitor text cannot label itself as trusted")

    def as_mapping(self) -> dict[str, Any]:
        """Return a fresh, canonical-serialization-ready mapping."""

        return {
            "event_id": self.event_id,
            "facet": self.facet.value if self.facet is not None else None,
            "item_id": self.item_id,
            "layer": self.layer.value,
            "mandatory": self.mandatory,
            "resident_id": self.resident_id,
            "revision": self.revision,
            "source": self.source,
            "source_role": self.source_role.value,
            "text": self.text,
            "trust": self.trust.value,
            "visibility": self.visibility.value,
        }


@dataclass(frozen=True)
class ContextBudgets:
    """Finite per-layer and whole-envelope limits in injected counter units."""

    high: int
    medium: int
    immediate: int
    total: int
    max_items: int = 64
    max_item_text_bytes: int = 4_096

    def __post_init__(self) -> None:
        for field_name in ("high", "medium", "immediate", "total", "max_items", "max_item_text_bytes"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise PersonaSchemaError(f"{field_name} must be an integer")
            if value < 1:
                raise PersonaSchemaError(f"{field_name} must be positive")
        if self.max_items > 256:
            raise InputLimitError("max_items cannot exceed the hard limit of 256")
        if self.max_item_text_bytes > MAX_ITEM_TEXT_BYTES:
            raise InputLimitError(
                f"max_item_text_bytes cannot exceed {MAX_ITEM_TEXT_BYTES} UTF-8 bytes"
            )

    def for_layer(self, layer: Layer) -> int:
        layer = _enum_value(layer, Layer, "layer")
        return {
            Layer.HIGH: self.high,
            Layer.MEDIUM: self.medium,
            Layer.IMMEDIATE: self.immediate,
        }[layer]


@dataclass(frozen=True)
class Omission:
    """A privacy-safe omission report; it carries no private source metadata."""

    layer: Layer
    reason: str
    count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "layer", _enum_value(self.layer, Layer, "layer"))
        if self.reason not in {"visibility", "layer_budget", "total_budget"}:
            raise PersonaSchemaError("unknown omission reason")
        if not isinstance(self.count, int) or self.count < 1:
            raise PersonaSchemaError("omission count must be positive")


def visitor_item(
    text: str,
    *,
    resident_id: str,
    event_id: str,
    source: str = "visitor-input",
    visibility: Visibility = Visibility.PRIVATE,
    layer: Layer = Layer.IMMEDIATE,
) -> ContextItem:
    """Create explicitly untrusted visitor text without parsing or promotion."""

    layer = _enum_value(layer, Layer, "layer")
    if layer is Layer.HIGH:
        raise ApprovalBoundaryError("visitor text belongs only to MEDIUM or IMMEDIATE state")
    return ContextItem(
        item_id=f"visitor:{event_id}",
        layer=layer,
        text=text,
        resident_id=resident_id,
        source=source,
        event_id=event_id,
        revision=1,
        trust=Trust.UNTRUSTED,
        visibility=visibility,
        source_role=SourceRole.VISITOR,
    )


def provided_item(
    text: str,
    *,
    item_id: str,
    layer: Layer,
    resident_id: str,
    source: str,
    event_id: str,
    revision: int = 1,
    visibility: Visibility = Visibility.PRIVATE,
    source_role: SourceRole = SourceRole.RESIDENT,
    mandatory: bool = False,
    facet: Facet | None = None,
) -> ContextItem:
    """Create caller-provided structured state with explicit non-approved trust."""

    return ContextItem(
        item_id=item_id,
        layer=layer,
        text=text,
        resident_id=resident_id,
        source=source,
        event_id=event_id,
        revision=revision,
        trust=Trust.PROVIDED,
        visibility=visibility,
        source_role=source_role,
        mandatory=mandatory,
        facet=facet,
    )
