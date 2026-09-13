"""Bounded synchronous OpenRouter/free HTTP adapter.

Contract
--------
complete accepts one caller-prepared context envelope and one visitor text
value.  It constructs exactly one non-streaming POST request to
https://openrouter.ai/api/v1/chat/completions with the fixed requested model
openrouter/free and the upstream hint max_tokens=500.  The credential
supplier, monotonic clock, HTTP transport, and accounting seam are injected
at call time.  No credentials or prompts are read from the environment,
files, history, or module/object state.

The accounting protocol is deliberately a seam for the durable shared owner
(LEO-169).  reserve must atomically validate a request-bound, one-use,
unexpired authorized attempt and return a typed PermitReservation.  settle must
record completed, provider_failed, unknown_sent, or cancelled.  A missing,
denied, expired, exhausted, reused, or failing admission fails closed before
HTTP.  A timeout or ambiguous transport result is unknown_sent and is never
refunded or retried by this adapter.  Synthetic permits in tests are not real
quota enforcement.

The request is capped at 65,536 UTF-8 bytes, each context and visitor value
has a finite input cap, responses (including error bodies) are read once with
a 262,144-byte limit plus one sentinel byte, parsed reply JSON is capped at
16,384 UTF-8 bytes, and the attempt deadline is 30 seconds.  The urllib
transport uses an explicit no-environment-proxy opener and a per-operation
socket timeout set to the remaining attempt budget, up to 30 seconds.  Python's
blocking DNS and socket operations cannot be forcibly interrupted by a
monotonic check, so the deadline is checked between operations and is not
advertised as a guaranteed hard wall-clock interrupt.  Injected credential,
permit, and transport callbacks are likewise caller-owned blocking boundaries.

Known optional provider metadata such as a null refusal, bounded reasoning,
annotations, citations, and a nullable system fingerprint is shape-checked
and discarded.  A non-null refusal, missing assistant content, unsupported
tool call, or malformed required action proposal remains a safe failure.

Response model/provider names and numeric usage are bounded, allowlisted
telemetry only.  Usage anomalies are reported as flags and never authorize,
refund, or prove token/cost accounting.  The returned reply, action, and
target are an untrusted proposal.  This module never executes actions or
mutates world state, leases, plans, biographies, or memories; downstream
LEO-168 owns action validation and authority.

This slice does not provide the real durable admission ledger, privacy or
provider-disclosure approval, a production credential, a provider tokenizer,
or the LEO-168 action/integration layer.  Those remain explicit launch gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import socket
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUESTED_MODEL = "openrouter/free"
REQUESTED_MAX_TOKENS = 500

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 262_144
MAX_REPLY_BYTES = 16_384
MAX_CONTEXT_BYTES = 32_768
MAX_VISITOR_TEXT_BYTES = 32_768
MAX_CREDENTIAL_BYTES = 512
MAX_TELEMETRY_STRING_BYTES = 512
MAX_ACTION_BYTES = 256
MAX_TARGET_BYTES = 256
MAX_ATTEMPT_SECONDS = 30.0
MAX_SOCKET_WAIT_SECONDS = MAX_ATTEMPT_SECONDS
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 4_096
MAX_OPTIONAL_METADATA_ITEMS = 64
MAX_REASONING_BYTES = 16_384
MAX_TELEMETRY_COUNT = 1_000_000_000
MAX_TELEMETRY_COST = 1_000_000_000.0


class AttemptOutcome(str, Enum):
    """Terminal accounting outcomes required from the shared owner."""

    COMPLETED = "completed"
    PROVIDER_FAILED = "provider_failed"
    UNKNOWN_SENT = "unknown_sent"
    CANCELLED = "cancelled"


class SentState(str, Enum):
    """What the adapter can safely say about the provider request."""

    NOT_SENT = "not_sent"
    SENT = "sent"
    UNKNOWN = "unknown"


class PermitRejected(Exception):
    """Safe, allowlisted admission rejection for an injected permit."""

    _REASONS = frozenset({"denied", "expired", "exhausted", "reused"})

    def __init__(self, reason: str = "denied") -> None:
        self.reason = reason if reason in self._REASONS else "denied"
        super().__init__()


class TransportCancelled(Exception):
    """Injected transport cancellation with explicit accounting semantics."""

    def __init__(self) -> None:
        super().__init__()


class RedirectRefused(Exception):
    """Raised by the real transport when a provider response redirects."""

    def __init__(self) -> None:
        super().__init__()


@dataclass(frozen=True)
class PermitReservation:
    """Typed opaque result of one durable, request-bound admission reserve."""

    reservation_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reservation_id, str)
            or not self.reservation_id
            or len(self.reservation_id.encode("utf-8")) > MAX_CREDENTIAL_BYTES
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in self.reservation_id
            )
        ):
            raise ValueError()

    def __repr__(self) -> str:
        """Do not expose an accounting token in ordinary diagnostics."""

        return "PermitReservation(<redacted>)"


@dataclass(frozen=True)
class AdmissionRequest:
    """Safe request metadata passed to the durable accounting owner.

    The SHA-256 digest binds the reservation without exposing prompt,
    credential, or serialized request content to accounting diagnostics.
    """

    request_id: str
    request_bytes: int
    request_sha256: str
    deadline: float


class MonotonicClock(Protocol):
    """Injected monotonic clock used for deadline and latency checks."""

    def __call__(self) -> float:
        """Return a finite monotonic timestamp."""


class CredentialSupplier(Protocol):
    """Ephemeral credential source owned by the caller."""

    def get(self) -> str:
        """Return one short-lived credential for one invocation."""


class AccountingPermit(Protocol):
    """Durable admission and terminal accounting seam.

    Implementations must reject missing, expired, exhausted, reused, or
    unauthorized attempts and must not treat a false or absent reservation as
    authorization.  The adapter never implements the shared ledger.
    """

    def reserve(self, request: AdmissionRequest) -> PermitReservation:
        """Atomically reserve one request-bound attempt or reject it."""

    def settle(
        self,
        reservation: PermitReservation,
        *,
        outcome: AttemptOutcome,
        sent_state: SentState,
    ) -> object:
        """Record the terminal outcome without refunding unknown sends."""


@dataclass(frozen=True)
class HttpRequest:
    """The complete bounded wire request given to an injected transport."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def __repr__(self) -> str:
        return (
            "HttpRequest("
            f"method={self.method!r}, url={self.url!r}, "
            f"body_bytes={len(self.body)}, headers=<redacted>)"
        )


class HttpResponse(Protocol):
    """Minimal response shape required by the bounded adapter."""

    status: int

    def read(self, limit: int) -> bytes:
        """Read at most limit bytes from the non-streaming response."""

    def close(self) -> object:
        """Release the underlying response resource."""


class HttpTransport(Protocol):
    """Injectable one-shot HTTP transport."""

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        """Send exactly the supplied request once."""


@dataclass(frozen=True)
class Failure:
    """Allowlisted failure metadata safe for callers to expose."""

    category: str
    http_status: int | None
    latency_ms: int
    sent_state: str
    accounting_state: str
    original_category: str | None = None


@dataclass(frozen=True)
class UsageTelemetry:
    """Bounded provider-reported usage; it is explicitly untrusted."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    cost: int | float | None


@dataclass(frozen=True)
class CompletionResult:
    """Safe result containing a proposal and bounded untrusted telemetry."""

    ok: bool
    reply: str | None
    action: str | None
    target: str | None
    reported_model: str | None
    reported_provider: str | None
    usage: UsageTelemetry | None
    telemetry_anomalies: tuple[str, ...]
    failure: Failure | None
    sent_state: str
    accounting_state: str
    latency_ms: int

    def __repr__(self) -> str:
        """Keep ordinary diagnostics free of reply, prompt, and raw response."""

        return (
            "CompletionResult("
            f"ok={self.ok}, reply_present={self.reply is not None}, "
            f"action_present={self.action is not None}, "
            f"target_present={self.target is not None}, "
            f"model_present={self.reported_model is not None}, "
            f"provider_present={self.reported_provider is not None}, "
            f"usage_present={self.usage is not None}, "
            f"anomaly_count={len(self.telemetry_anomalies)}, "
            f"failure_category={self.failure.category if self.failure else None!r}, "
            f"sent_state={self.sent_state!r}, "
            f"accounting_state={self.accounting_state!r})"
        )

    @property
    def success(self) -> bool:
        """Alias that makes the terminal status readable to callers."""

        return self.ok

    @property
    def error(self) -> Failure | None:
        """Safe failure alias; it never contains provider or prompt text."""

        return self.failure

    @property
    def resolved_model(self) -> str | None:
        """The provider-reported model, retained as untrusted telemetry."""

        return self.reported_model


class _Reject(Exception):
    """Internal control flow carrying only an allowlisted category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__()


class _JsonReject(Exception):
    """Internal JSON hook rejection carrying no parsed or raw data."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__()


@dataclass(frozen=True)
class _ParsedReply:
    reply: str
    action: str
    target: str | None
    model: str | None
    provider: str | None
    usage: UsageTelemetry | None
    anomalies: tuple[str, ...]


def _clock_value(clock: MonotonicClock) -> float:
    try:
        value = clock()
    except Exception:
        raise _Reject("clock_failure") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Reject("clock_failure")
    value = float(value)
    if not math.isfinite(value):
        raise _Reject("clock_failure")
    return value


def _remaining(clock: MonotonicClock, deadline: float) -> float:
    remaining = deadline - _clock_value(clock)
    if not math.isfinite(remaining):
        raise _Reject("clock_failure")
    if remaining <= 0:
        raise _Reject("deadline_exceeded")
    return min(remaining, MAX_SOCKET_WAIT_SECONDS)


def _latency_ms(clock: MonotonicClock | None, started: float | None) -> int:
    if clock is None or started is None:
        return 0
    try:
        elapsed = _clock_value(clock) - started
    except _Reject:
        return 0
    if elapsed < 0 or not math.isfinite(elapsed):
        return 0
    return min(int(elapsed * 1000), int(MAX_ATTEMPT_SECONDS * 1000))


def _status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 100 or value > 599:
        return None
    return value


def _string_size(
    value: object,
    limit: int,
    category: str,
    *,
    type_category: str | None = None,
) -> int:
    if not isinstance(value, str):
        raise _Reject(type_category if type_category is not None else category)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _Reject(category) from None
    if size > limit:
        raise _Reject(category)
    return size


def _credential(supplier: object) -> str:
    if supplier is None:
        raise _Reject("missing_credential")
    try:
        getter = getattr(supplier, "get", None)
        if callable(getter):
            value = getter()
        elif callable(supplier):
            value = supplier()
        else:
            raise _Reject("invalid_credential_supplier")
    except _Reject:
        raise
    except Exception:
        raise _Reject("credential_failure") from None
    _string_size(value, MAX_CREDENTIAL_BYTES, "invalid_credential")
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _Reject("invalid_credential")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise _Reject("invalid_credential") from None
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonReject("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    del value
    raise _JsonReject("nonfinite_json_number")


def _check_json_tree(value: object, depth: int = 0, nodes: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise _Reject("json_too_deep")
    nodes += 1
    if nodes > MAX_JSON_NODES:
        raise _Reject("json_too_large")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _Reject("malformed_json")
            nodes = _check_json_tree(child, depth + 1, nodes)
    elif isinstance(value, list):
        for child in value:
            nodes = _check_json_tree(child, depth + 1, nodes)
    return nodes


def _parse_json(body: bytes) -> object:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject("invalid_utf8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except _JsonReject as exc:
        raise _Reject(exc.category) from None
    except (TypeError, ValueError, RecursionError):
        raise _Reject("malformed_json") from None
    _check_json_tree(value)
    return value


def _bounded_telemetry_string(value: object, category: str) -> str:
    _string_size(value, MAX_TELEMETRY_STRING_BYTES, category)
    return value


def _optional_bounded_string(value: object, limit: int, category: str) -> None:
    if value is not None:
        _string_size(value, limit, category)


def _optional_metadata_list(value: object, category: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) > MAX_OPTIONAL_METADATA_ITEMS:
        raise _Reject(category)


def _usage_number(raw: Mapping[str, object], key: str) -> tuple[int | None, bool]:
    if key not in raw:
        return None, False
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None, True
    if value < 0 or value > MAX_TELEMETRY_COUNT:
        return None, True
    return value, False


def _usage_cost(raw: Mapping[str, object]) -> tuple[int | float | None, bool]:
    if "cost" not in raw:
        return None, False
    value = raw["cost"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, True
    if not math.isfinite(float(value)) or value < 0 or value > MAX_TELEMETRY_COST:
        return None, True
    return value, False


def _parse_usage(raw: object) -> tuple[UsageTelemetry | None, tuple[str, ...]]:
    if raw is None:
        return None, ("usage_missing",)
    if not isinstance(raw, dict):
        return None, ("usage_malformed",)

    anomalies: list[str] = []
    prompt, prompt_bad = _usage_number(raw, "prompt_tokens")
    completion, completion_bad = _usage_number(raw, "completion_tokens")
    total, total_bad = _usage_number(raw, "total_tokens")
    cost, cost_bad = _usage_cost(raw)
    if prompt_bad or completion_bad or total_bad or cost_bad:
        anomalies.append("usage_malformed")
    if prompt is None:
        anomalies.append("usage_prompt_tokens_missing")
    if completion is None:
        anomalies.append("usage_completion_tokens_missing")
    if total is None:
        anomalies.append("usage_total_tokens_missing")

    reasoning: int | None = None
    direct_reasoning, direct_bad = _usage_number(raw, "reasoning_tokens")
    if direct_bad:
        anomalies.append("usage_malformed")
    details = raw.get("completion_tokens_details")
    if details is not None:
        if not isinstance(details, dict):
            anomalies.append("usage_malformed")
        else:
            nested_reasoning, nested_bad = _usage_number(details, "reasoning_tokens")
            if nested_bad:
                anomalies.append("usage_malformed")
            if (
                direct_reasoning is not None
                and nested_reasoning is not None
                and direct_reasoning != nested_reasoning
            ):
                anomalies.append("usage_inconsistent")
            reasoning = nested_reasoning if nested_reasoning is not None else direct_reasoning
    else:
        reasoning = direct_reasoning

    if (
        prompt is not None
        and completion is not None
        and total is not None
        and total != prompt + completion
    ):
        anomalies.append("usage_inconsistent")
    if completion is not None and completion > REQUESTED_MAX_TOKENS:
        anomalies.append("completion_tokens_exceed_requested_max_tokens")
    if reasoning is not None and completion is not None and reasoning > completion:
        anomalies.append("reasoning_tokens_exceed_completion_tokens")
    if cost is not None and cost != 0:
        anomalies.append("reported_cost_nonzero")

    ordered = tuple(dict.fromkeys(anomalies))
    return UsageTelemetry(prompt, completion, total, reasoning, cost), ordered


def _parse_reply(value: object) -> tuple[str, str, str | None]:
    if not isinstance(value, dict):
        raise _Reject("malformed_reply")
    expected = {"reply", "action", "target"}
    if set(value) != expected:
        raise _Reject("malformed_reply")
    reply = value["reply"]
    action = value["action"]
    target = value["target"]
    _string_size(
        reply,
        MAX_REPLY_BYTES,
        "reply_too_large",
        type_category="malformed_reply",
    )
    _string_size(action, MAX_ACTION_BYTES, "malformed_reply")
    if target is not None:
        _string_size(target, MAX_TARGET_BYTES, "malformed_reply")
    return reply, action, target


def _parse_success(value: object) -> _ParsedReply:
    if not isinstance(value, dict):
        raise _Reject("malformed_response")
    if "error" in value:
        if not isinstance(value["error"], dict):
            raise _Reject("provider_error")
        raise _Reject("provider_error")

    allowed_outer = {
        "choices",
        "model",
        "provider",
        "usage",
        "id",
        "object",
        "created",
        "system_fingerprint",
        "citations",
    }
    if set(value) - allowed_outer:
        raise _Reject("malformed_response")
    for key in ("id", "object"):
        if key in value:
            _bounded_telemetry_string(value[key], "malformed_response")
    if "system_fingerprint" in value:
        _optional_bounded_string(
            value["system_fingerprint"],
            MAX_TELEMETRY_STRING_BYTES,
            "malformed_response",
        )
    if "citations" in value:
        _optional_metadata_list(value["citations"], "malformed_response")
    if "created" in value:
        created = value["created"]
        if (
            isinstance(created, bool)
            or not isinstance(created, int)
            or created < 0
            or created > MAX_TELEMETRY_COUNT
        ):
            raise _Reject("malformed_response")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise _Reject("malformed_choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _Reject("malformed_choices")
    allowed_choice = {"index", "message", "finish_reason", "native_finish_reason", "logprobs"}
    if set(choice) - allowed_choice:
        raise _Reject("malformed_choice")
    if "index" in choice:
        index = choice["index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 256:
            raise _Reject("malformed_choice")
    if "native_finish_reason" in choice:
        _optional_bounded_string(
            choice["native_finish_reason"],
            MAX_TELEMETRY_STRING_BYTES,
            "malformed_choice",
        )
    if "logprobs" in choice and choice["logprobs"] is not None and not isinstance(
        choice["logprobs"], dict
    ):
        raise _Reject("malformed_choice")
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise _Reject("truncated_response")
    if finish_reason != "stop":
        raise _Reject("unsupported_finish_reason")

    message = choice.get("message")
    if not isinstance(message, dict):
        raise _Reject("malformed_message")
    if "tool_calls" in message or "function_call" in message:
        raise _Reject("tool_calls_not_allowed")
    allowed_message = {
        "role",
        "content",
        "refusal",
        "reasoning",
        "reasoning_details",
        "annotations",
    }
    if set(message) - allowed_message or message.get("role") != "assistant":
        raise _Reject("malformed_message")
    refusal = message.get("refusal")
    refusal_present = refusal is not None
    if refusal_present:
        _string_size(refusal, MAX_REASONING_BYTES, "malformed_refusal")
        raise _Reject("refusal_not_allowed")
    if "reasoning" in message:
        _optional_bounded_string(
            message["reasoning"],
            MAX_REASONING_BYTES,
            "malformed_reasoning",
        )
    if "reasoning_details" in message:
        _optional_metadata_list(message["reasoning_details"], "malformed_reasoning")
    if "annotations" in message:
        _optional_metadata_list(message["annotations"], "malformed_annotations")
    if "content" not in message or message["content"] is None:
        raise _Reject("refusal_not_allowed" if refusal_present else "content_missing")
    content = message.get("content")
    _string_size(
        content,
        MAX_REPLY_BYTES,
        "reply_too_large",
        type_category="malformed_message",
    )

    inner = _parse_json(content.encode("utf-8"))
    reply, action, target = _parse_reply(inner)

    model: str | None = None
    if "model" in value:
        model = _bounded_telemetry_string(value["model"], "malformed_model")
    provider: str | None = None
    if "provider" in value:
        provider = _bounded_telemetry_string(value["provider"], "malformed_provider")

    usage, usage_anomalies = _parse_usage(value.get("usage"))
    anomalies = list(usage_anomalies)
    if model is None:
        anomalies.append("reported_model_missing")
    else:
        anomalies.append("reported_model_unverified")
        if model == REQUESTED_MODEL:
            anomalies.append("reported_model_is_request_alias")
    if provider is not None:
        anomalies.append("reported_provider_unverified")
    return _ParsedReply(
        reply=reply,
        action=action,
        target=target,
        model=model,
        provider=provider,
        usage=usage,
        anomalies=tuple(dict.fromkeys(anomalies)),
    )


def _failure(
    category: str,
    *,
    original_category: str | None = None,
    status: int | None,
    started: float | None,
    clock: MonotonicClock | None,
    sent_state: SentState,
    accounting_state: str,
) -> CompletionResult:
    latency = _latency_ms(clock, started)
    metadata = Failure(
        category=category,
        original_category=original_category,
        http_status=_status(status),
        latency_ms=latency,
        sent_state=sent_state.value,
        accounting_state=accounting_state,
    )
    return CompletionResult(
        ok=False,
        reply=None,
        action=None,
        target=None,
        reported_model=None,
        reported_provider=None,
        usage=None,
        telemetry_anomalies=(),
        failure=metadata,
        sent_state=sent_state.value,
        accounting_state=accounting_state,
        latency_ms=latency,
    )


def _settle(
    permit: AccountingPermit,
    reservation: PermitReservation,
    outcome: AttemptOutcome,
    sent_state: SentState,
) -> str:
    try:
        result = permit.settle(
            reservation,
            outcome=outcome,
            sent_state=sent_state,
        )
    except Exception:
        return "settlement_failed"
    if result is False:
        return "settlement_failed"
    return f"settled_{outcome.value}"


def _reserved_failure(
    category: str,
    *,
    status: int | None,
    started: float | None,
    clock: MonotonicClock,
    permit: AccountingPermit,
    reservation: PermitReservation,
    outcome: AttemptOutcome,
    sent_state: SentState,
) -> CompletionResult:
    accounting_state = _settle(permit, reservation, outcome, sent_state)
    original_category = category if accounting_state == "settlement_failed" else None
    if accounting_state == "settlement_failed":
        category = "accounting_failure"
    return _failure(
        category,
        original_category=original_category,
        status=status,
        started=started,
        clock=clock,
        sent_state=sent_state,
        accounting_state=accounting_state,
    )


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError):
        try:
            reason = error.reason
        except Exception:
            return False
        return isinstance(reason, (TimeoutError, socket.timeout))
    return False


class _NoRedirectHandler(HTTPRedirectHandler):
    """urllib handler that makes a redirect a terminal safe failure."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> Any:
        raise RedirectRefused()


class UrllibTransport:
    """Real stdlib HTTPS transport with redirect forwarding disabled."""

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        if request.method != "POST" or request.url != OPENROUTER_URL:
            raise ValueError()
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
            or timeout > MAX_SOCKET_WAIT_SECONDS
        ):
            raise ValueError()
        opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        wire_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        return opener.open(wire_request, timeout=timeout)


def _response_status(response: object) -> object:
    if isinstance(response, HTTPError):
        try:
            return response.code
        except Exception:
            return None
    try:
        return getattr(response, "status")
    except Exception:
        return None


def _read_and_close(response: object) -> tuple[object, bytes | None, str | None, bool]:
    status = _response_status(response)
    read_error: str | None = None
    body: bytes | None = None
    try:
        reader = getattr(response, "read", None)
        if not callable(reader):
            read_error = "response_read_failure"
        else:
            body = reader(MAX_RESPONSE_BYTES + 1)
            if not isinstance(body, bytes):
                read_error = "response_read_failure"
            elif len(body) > MAX_RESPONSE_BYTES:
                read_error = "response_too_large"
    except TransportCancelled:
        read_error = "cancelled"
    except Exception as error:
        read_error = "timeout" if _is_timeout(error) else "response_read_failure"

    close_failed = False
    try:
        closer = getattr(response, "close", None)
        if not callable(closer):
            close_failed = True
        else:
            closer()
    except Exception:
        close_failed = True
    return status, body, read_error, close_failed


def complete(
    context_envelope: str,
    visitor_text: str,
    *,
    credential_supplier: CredentialSupplier | Callable[[], str] | None = None,
    permit: AccountingPermit | None = None,
    clock: MonotonicClock = time.monotonic,
    transport: HttpTransport | None = None,
) -> CompletionResult:
    """Make one bounded free-routed request and return an untrusted proposal.

    All refusal paths return CompletionResult metadata instead of provider
    exception text.  A caller must supply the real accounting seam and an
    ephemeral credential; no default allow or environment lookup exists.
    """

    started: float | None = None
    try:
        started = _clock_value(clock)
    except _Reject as reject:
        return _failure(
            reject.category,
            status=None,
            started=None,
            clock=None,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )

    if permit is None:
        return _failure(
            "missing_permit",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )
    try:
        reserve_method = getattr(permit, "reserve", None)
        settle_method = getattr(permit, "settle", None)
    except Exception:
        return _failure(
            "invalid_permit",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )
    if not callable(reserve_method) or not callable(settle_method):
        return _failure(
            "invalid_permit",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )
    if transport is not None and not callable(getattr(transport, "send", None)):
        return _failure(
            "invalid_transport",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )

    try:
        _string_size(context_envelope, MAX_CONTEXT_BYTES, "invalid_context")
        _string_size(visitor_text, MAX_VISITOR_TEXT_BYTES, "invalid_visitor_text")
        payload = {
            "model": REQUESTED_MODEL,
            "messages": [
                {"role": "system", "content": context_envelope},
                {"role": "user", "content": visitor_text},
            ],
            "max_tokens": REQUESTED_MAX_TOKENS,
            "stream": False,
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise _Reject("request_too_large")
        credential = _credential(credential_supplier)
        remaining = _remaining(clock, started + MAX_ATTEMPT_SECONDS)
    except _Reject as reject:
        return _failure(
            reject.category,
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )
    except (TypeError, ValueError, UnicodeError):
        return _failure(
            "invalid_request",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="not_reserved",
        )

    request = HttpRequest(
        method="POST",
        url=OPENROUTER_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {credential}",
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        },
        body=body,
    )
    admission = AdmissionRequest(
        request_id=uuid.uuid4().hex,
        request_bytes=len(body),
        request_sha256=hashlib.sha256(body).hexdigest(),
        deadline=started + MAX_ATTEMPT_SECONDS,
    )
    try:
        reservation = permit.reserve(admission)
    except PermitRejected as rejection:
        category = {
            "denied": "admission_denied",
            "expired": "permit_expired",
            "exhausted": "permit_exhausted",
            "reused": "permit_reused",
        }.get(rejection.reason, "admission_denied")
        return _failure(
            category,
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="admission_denied",
        )
    except Exception:
        return _failure(
            "accounting_failure",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="admission_failed",
        )
    if not isinstance(reservation, PermitReservation):
        return _failure(
            "invalid_reservation",
            status=None,
            started=started,
            clock=clock,
            sent_state=SentState.NOT_SENT,
            accounting_state="admission_denied",
        )

    try:
        remaining = _remaining(clock, admission.deadline)
    except _Reject as reject:
        return _reserved_failure(
            reject.category,
            status=None,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.CANCELLED,
            sent_state=SentState.NOT_SENT,
        )

    selected_transport: HttpTransport = transport if transport is not None else UrllibTransport()
    response: object
    try:
        response = selected_transport.send(request, timeout=remaining)
    except RedirectRefused:
        return _reserved_failure(
            "redirect_refused",
            status=None,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.SENT,
        )
    except TransportCancelled:
        return _reserved_failure(
            "cancelled",
            status=None,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.CANCELLED,
            sent_state=SentState.UNKNOWN,
        )
    except HTTPError as error_response:
        response = error_response
    except Exception as error:
        return _reserved_failure(
            "timeout" if _is_timeout(error) else "transport_error",
            status=None,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.UNKNOWN_SENT,
            sent_state=SentState.UNKNOWN,
        )

    status_value, body, read_error, close_failed = _read_and_close(response)
    status = _status(status_value)
    if read_error is not None:
        return _reserved_failure(
            read_error,
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.UNKNOWN_SENT
            if read_error in {"timeout", "response_read_failure"}
            else AttemptOutcome.CANCELLED
            if read_error == "cancelled"
            else AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.UNKNOWN
            if read_error in {"timeout", "response_read_failure", "cancelled"}
            else SentState.SENT,
        )
    if close_failed:
        return _reserved_failure(
            "response_close_failure",
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.UNKNOWN_SENT,
            sent_state=SentState.UNKNOWN,
        )
    try:
        _remaining(clock, admission.deadline)
    except _Reject as reject:
        return _reserved_failure(
            reject.category,
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.UNKNOWN_SENT,
            sent_state=SentState.UNKNOWN,
        )

    if status is None:
        return _reserved_failure(
            "malformed_status",
            status=None,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.SENT,
        )
    if 300 <= status < 400:
        return _reserved_failure(
            "redirect_refused",
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.SENT,
        )
    if status != 200:
        return _reserved_failure(
            "provider_http_error",
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.SENT,
        )

    try:
        parsed = _parse_success(_parse_json(body if body is not None else b""))
        _remaining(clock, admission.deadline)
    except _Reject as reject:
        return _reserved_failure(
            reject.category,
            status=status,
            started=started,
            clock=clock,
            permit=permit,
            reservation=reservation,
            outcome=AttemptOutcome.PROVIDER_FAILED,
            sent_state=SentState.SENT,
        )

    accounting_state = _settle(
        permit,
        reservation,
        AttemptOutcome.COMPLETED,
        SentState.SENT,
    )
    if accounting_state == "settlement_failed":
        return _failure(
            "accounting_failure",
            original_category="completed",
            status=status,
            started=started,
            clock=clock,
            sent_state=SentState.SENT,
            accounting_state=accounting_state,
        )

    latency = _latency_ms(clock, started)
    return CompletionResult(
        ok=True,
        reply=parsed.reply,
        action=parsed.action,
        target=parsed.target,
        reported_model=parsed.model,
        reported_provider=parsed.provider,
        usage=parsed.usage,
        telemetry_anomalies=parsed.anomalies,
        failure=None,
        sent_state=SentState.SENT.value,
        accounting_state=accounting_state,
        latency_ms=latency,
    )
