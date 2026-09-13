"""Small, import-safe inference adapters.

The package exposes one bounded OpenRouter callable.  Importing it performs no
credential discovery, network access, model call, or application startup.
"""

from .openrouter import (
    AccountingPermit,
    AdmissionRequest,
    AttemptOutcome,
    CompletionResult,
    CredentialSupplier,
    Failure,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    MonotonicClock,
    PermitReservation,
    PermitRejected,
    RedirectRefused,
    SentState,
    TransportCancelled,
    UsageTelemetry,
    UrllibTransport,
    complete,
)

__all__ = [
    "AccountingPermit",
    "AdmissionRequest",
    "AttemptOutcome",
    "CompletionResult",
    "CredentialSupplier",
    "Failure",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "MonotonicClock",
    "PermitReservation",
    "PermitRejected",
    "RedirectRefused",
    "SentState",
    "TransportCancelled",
    "UsageTelemetry",
    "UrllibTransport",
    "complete",
]
