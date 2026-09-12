"""Pure, immutable approved-persona registry.

The registry API is the canonical issuer of approved fact values. A correction
returns a new registry snapshot, leaving the old snapshot and its inputs
untouched. No visitor text, model output, or persistent storage is accepted
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    ApprovalBoundaryError,
    ContextItem,
    Facet,
    PersonaProfile,
    SourceRole,
    Trust,
    VersionedFact,
    Visibility,
)


class ApprovalRequired(ApprovalBoundaryError):
    """A registry-changing operation lacks its explicit authority handle."""


class RegistryAuthority:
    """Internal-convention handle for explicit owner/test approval workflows.

    Ordinary callers obtain a handle from a registry snapshot's explicitly
    named synthetic test helper. This handle and its marker are trusted Python
    conventions, not a sandbox against arbitrary Python code. A future owner
    approval service should replace the helper before launch.
    """

    __slots__ = ("_marker",)

    def __init__(self, marker: object) -> None:
        if marker is None:
            raise ApprovalRequired("an explicit registry authority is required")
        self._marker = marker

    def _matches(self, marker: object) -> bool:
        return self._marker is marker


_FACT_TEXTS: tuple[tuple[Facet, tuple[str, ...]], ...] = (
    (Facet.MAYOR, ("debate/leadership history",)),
    (Facet.ENGINEER, ("coding/building obsession", "some high-school robotics")),
    (Facet.TEACHER, ("knowledgeable", "teaches math", "understanding and patient")),
    (Facet.CHEF, ("likes cooking", "seeks new ingredients")),
    (
        Facet.ARTSY,
        ("listens to music", "plays piano", "likes anime", "strong aesthetic taste"),
    ),
)


@dataclass(frozen=True)
class ApprovedPersonaRegistry:
    """An immutable current view of the five approved persona facets."""

    _resident_id: str
    _active: tuple[VersionedFact, ...]
    _history: tuple[VersionedFact, ...]
    _authority_marker: object

    @classmethod
    def default(cls, *, resident_id: str = "leo") -> "ApprovedPersonaRegistry":
        """Build the supplied five-facet registry at revision one."""

        marker = object()
        active: list[VersionedFact] = []
        for facet, texts in _FACT_TEXTS:
            for number, text in enumerate(texts, start=1):
                active.append(
                    VersionedFact(
                        fact_id=f"{facet.value}-{number}",
                        resident_id=resident_id,
                        facet=facet,
                        text=text,
                        source="leo-approved",
                        event_id="persona-registry-v1",
                        revision=1,
                        trust=Trust.APPROVED,
                        visibility=Visibility.RESIDENT,
                        source_role=SourceRole.REGISTRY,
                        _registry_marker=marker,
                    )
                )
        return cls(
            _resident_id=resident_id,
            _active=tuple(active),
            _history=tuple(active),
            _authority_marker=marker,
        )

    @property
    def resident_id(self) -> str:
        return self._resident_id

    @property
    def facts(self) -> tuple[VersionedFact, ...]:
        """Current facts only; superseded versions are excluded."""

        return self._active

    @property
    def profiles(self) -> tuple[PersonaProfile, ...]:
        """Return the five profiles in their stable facet order."""

        return tuple(
            PersonaProfile(
                facet=facet,
                facts=tuple(fact for fact in self._active if fact.facet is facet),
            )
            for facet, _ in _FACT_TEXTS
        )

    def profile(self, facet: Facet | str) -> PersonaProfile:
        facet = Facet(facet)
        for profile in self.profiles:
            if profile.facet is facet:
                return profile
        raise KeyError(facet.value)

    @property
    def history(self) -> tuple[VersionedFact, ...]:
        """All immutable versions, including superseded historical values."""

        return self._history

    def versions(self, fact_id: str) -> tuple[VersionedFact, ...]:
        return tuple(fact for fact in self._history if fact.fact_id == fact_id)

    def get_fact(self, fact_id: str) -> VersionedFact:
        for fact in self._active:
            if fact.fact_id == fact_id:
                return fact
        raise KeyError(fact_id)

    def synthetic_authority_for_tests(self) -> RegistryAuthority:
        """Return an explicit synthetic authority for deterministic unit tests.

        This helper is a test seam, not an owner identity or launch approval
        mechanism.  Production approval integration is outside this ticket.
        """

        return RegistryAuthority(self._authority_marker)

    def supersede(
        self,
        fact_id: str,
        replacement_text: str,
        *,
        authority: RegistryAuthority,
        source: str = "leo-approved-correction",
        event_id: str = "persona-correction",
    ) -> "ApprovedPersonaRegistry":
        """Return a snapshot with one explicit replacement revision."""

        if not isinstance(authority, RegistryAuthority) or not authority._matches(
            self._authority_marker
        ):
            raise ApprovalRequired("supersession requires the registry authority handle")
        old = self.get_fact(fact_id)
        if replacement_text == old.text:
            raise ValueError("a supersession must change the fact text")
        replacement = VersionedFact(
            fact_id=old.fact_id,
            resident_id=old.resident_id,
            facet=old.facet,
            text=replacement_text,
            source=source,
            event_id=event_id,
            revision=old.revision + 1,
            trust=Trust.APPROVED,
            visibility=old.visibility,
            source_role=SourceRole.REGISTRY,
            supersedes_revision=old.revision,
            _registry_marker=self._authority_marker,
        )
        active = tuple(replacement if fact.fact_id == fact_id else fact for fact in self._active)
        return ApprovedPersonaRegistry(
            _resident_id=self._resident_id,
            _active=active,
            _history=self._history + (replacement,),
            _authority_marker=self._authority_marker,
        )

    def correct(
        self,
        fact_id: str,
        replacement_text: str,
        *,
        authority: RegistryAuthority,
        source: str = "leo-approved-correction",
        event_id: str = "persona-correction",
    ) -> "ApprovedPersonaRegistry":
        """Readable alias for :meth:`supersede`."""

        return self.supersede(
            fact_id,
            replacement_text,
            authority=authority,
            source=source,
            event_id=event_id,
        )

    def context_items(self) -> tuple[ContextItem, ...]:
        """Issue current approved facts as mandatory HIGH context values."""

        return tuple(
            ContextItem(
                item_id=fact.fact_id,
                layer="high",
                text=fact.text,
                resident_id=fact.resident_id,
                source=fact.source,
                event_id=fact.event_id,
                revision=fact.revision,
                trust=fact.trust,
                visibility=fact.visibility,
                source_role=fact.source_role,
                mandatory=True,
                facet=fact.facet,
                _registry_marker=self._authority_marker,
            )
            for fact in self._active
        )
