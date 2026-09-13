"""Offline contract tests for the bounded OpenRouter/free adapter.

Every test injects an unmistakably fake credential, permit, clock, and
transport.  The fake transport records the real serialized request and feeds
bounded response bytes through the adapter's actual wire and parser paths.
There are no sockets, providers, credentials, model calls, or world actions.
"""

from __future__ import annotations

import importlib
import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from inference import openrouter
from inference.openrouter import (
    AttemptOutcome,
    MAX_CONTEXT_BYTES,
    MAX_REPLY_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_VISITOR_TEXT_BYTES,
    PermitRejected,
    RedirectRefused,
    SentState,
    TransportCancelled,
    complete,
)


FAKE_CREDENTIAL = "fake-openrouter-credential-leo175"
CONTEXT = "fake-prepared-context"
VISITOR = "fake-visitor-text"


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


class FakeResponse:
    def __init__(
        self,
        status: object,
        body: bytes,
        *,
        ignore_limit: bool = False,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.ignore_limit = ignore_limit
        self.read_error = read_error
        self.read_limits: list[int] = []
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        if self.read_error is not None:
            raise self.read_error
        if self.ignore_limit:
            return self.body
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.events = events if events is not None else []
        self.calls = 0
        self.requests = []
        self.timeouts: list[float] = []

    def send(self, request, *, timeout: float):
        self.calls += 1
        self.events.append("send")
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


class FakePermit:
    def __init__(
        self,
        *,
        rejection: str | None = None,
        reservation: object = "fake-reservation",
        reserve_error: BaseException | None = None,
        settle_error: BaseException | None = None,
        settle_result: object = True,
        events: list[str] | None = None,
        on_reserve=None,
    ) -> None:
        self.rejection = rejection
        self.reservation = reservation
        self.reserve_error = reserve_error
        self.settle_error = settle_error
        self.settle_result = settle_result
        self.events = events if events is not None else []
        self.on_reserve = on_reserve
        self.reserve_calls = []
        self.settle_calls = []

    def reserve(self, request):
        self.events.append("reserve")
        self.reserve_calls.append(request)
        if self.on_reserve is not None:
            self.on_reserve()
        if self.reserve_error is not None:
            raise self.reserve_error
        if self.rejection is not None:
            raise PermitRejected(self.rejection)
        return self.reservation

    def settle(self, reservation, *, outcome, sent_state):
        self.events.append("settle")
        self.settle_calls.append((reservation, outcome, sent_state))
        if self.settle_error is not None:
            raise self.settle_error
        return self.settle_result


def response_body(
    *,
    reply: str = "fake reply",
    action: str = "propose",
    target: str | None = "fake-target",
    finish_reason: str = "stop",
    model: object = "fixture/free-model-a",
    provider: object = "fixture-provider",
    usage: object = None,
    inner_content: str | None = None,
) -> bytes:
    inner = (
        inner_content
        if inner_content is not None
        else json.dumps(
            {"reply": reply, "action": action, "target": target},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    outer = {
        "choices": [
            {
                "message": {"role": "assistant", "content": inner},
                "finish_reason": finish_reason,
            }
        ],
        "model": model,
        "provider": provider,
    }
    if usage is not None:
        outer["usage"] = usage
    return json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def invoke(
    *,
    permit: FakePermit | None = None,
    transport: FakeTransport | None = None,
    clock: FakeClock | None = None,
    context: str = CONTEXT,
    visitor: str = VISITOR,
    credential=lambda: FAKE_CREDENTIAL,
):
    return complete(
        context,
        visitor,
        credential_supplier=credential,
        permit=permit if permit is not None else FakePermit(),
        clock=clock if clock is not None else FakeClock(),
        transport=transport
        if transport is not None
        else FakeTransport(FakeResponse(200, response_body())),
    )


class OpenRouterAdapterTests(unittest.TestCase):
    def test_import_does_not_touch_socket(self) -> None:
        with patch.object(socket, "socket", side_effect=AssertionError("socket used")):
            importlib.import_module("inference.openrouter")

    def test_success_serializes_fixed_request_once_and_only_proposes_action(self) -> None:
        events: list[str] = []
        permit = FakePermit(events=events)
        response = FakeResponse(200, response_body())
        transport = FakeTransport(response, events=events)
        result = invoke(permit=permit, transport=transport)

        self.assertTrue(result.ok)
        self.assertEqual(result.reply, "fake reply")
        self.assertEqual(result.action, "propose")
        self.assertEqual(result.target, "fake-target")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(events[:2], ["reserve", "send"])
        self.assertEqual(events[-1], "settle")
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, openrouter.OPENROUTER_URL)
        self.assertEqual(request.headers["Authorization"], f"Bearer {FAKE_CREDENTIAL}")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(int(request.headers["Content-Length"]), len(request.body))
        payload = json.loads(request.body)
        self.assertEqual(payload["model"], "openrouter/free")
        self.assertEqual(payload["max_tokens"], 500)
        self.assertFalse(payload["stream"])
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": CONTEXT},
                {"role": "user", "content": VISITOR},
            ],
        )
        self.assertNotIn("tools", payload)
        self.assertEqual(response.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(response.closed)
        self.assertEqual(permit.settle_calls[0][1], AttemptOutcome.COMPLETED)
        self.assertEqual(permit.settle_calls[0][2], SentState.SENT)

    def test_result_and_request_repr_redact_secret_and_prompt(self) -> None:
        response = FakeResponse(401, b'{"error":{"message":"fake-provider-secret"}}')
        transport = FakeTransport(response)
        result = invoke(transport=transport, credential=lambda: "fake-secret-token")
        self.assertNotIn("fake-secret-token", repr(result))
        self.assertNotIn("fake-provider-secret", repr(result))
        request_repr = repr(transport.requests[0])
        self.assertNotIn("fake-secret-token", request_repr)
        self.assertNotIn(CONTEXT, request_repr)
        self.assertNotIn(VISITOR, request_repr)

    def test_success_diagnostics_redact_raw_proposal_text(self) -> None:
        result = invoke(
            context="fake-sensitive-context",
            visitor="fake-sensitive-visitor",
            transport=FakeTransport(
                FakeResponse(
                    200,
                    response_body(
                        reply="fake-provider-reply",
                        action="fake-action",
                        target="fake-target",
                    ),
                )
            ),
        )
        self.assertTrue(result.ok)
        diagnostic = repr(result)
        for value in (
            "fake-sensitive-context",
            "fake-sensitive-visitor",
            "fake-provider-reply",
            "fake-action",
            "fake-target",
        ):
            self.assertNotIn(value, diagnostic)

    def test_missing_permit_never_reads_credential_or_sends(self) -> None:
        calls: list[str] = []
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = complete(
            CONTEXT,
            VISITOR,
            credential_supplier=lambda: calls.append("credential") or FAKE_CREDENTIAL,
            permit=None,
            transport=transport,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "missing_permit")
        self.assertEqual(calls, [])
        self.assertEqual(transport.calls, 0)

    def test_missing_credential_never_reserves_or_sends(self) -> None:
        permit = FakePermit()
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(permit=permit, transport=transport, credential=None)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "missing_credential")
        self.assertEqual(permit.reserve_calls, [])
        self.assertEqual(transport.calls, 0)

    def test_rejected_permits_are_fail_closed_without_send(self) -> None:
        for reason, category in (
            ("denied", "admission_denied"),
            ("expired", "permit_expired"),
            ("exhausted", "permit_exhausted"),
            ("reused", "permit_reused"),
        ):
            with self.subTest(reason=reason):
                permit = FakePermit(rejection=reason)
                transport = FakeTransport(FakeResponse(200, response_body()))
                result = invoke(permit=permit, transport=transport)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, category)
                self.assertEqual(transport.calls, 0)
                self.assertEqual(permit.settle_calls, [])

    def test_boolean_permit_result_is_not_default_allow(self) -> None:
        permit = FakePermit(reservation=True)
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(permit=permit, transport=transport)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "admission_denied")
        self.assertEqual(transport.calls, 0)

    def test_accounting_failure_before_send_is_fail_closed(self) -> None:
        permit = FakePermit(reserve_error=RuntimeError("fake-accounting-secret"))
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(permit=permit, transport=transport)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "accounting_failure")
        self.assertEqual(result.failure.accounting_state, "admission_failed")
        self.assertEqual(result.failure.sent_state, "not_sent")
        self.assertEqual(transport.calls, 0)

    def test_accounting_failure_after_send_never_reports_success_or_refund(self) -> None:
        permit = FakePermit(settle_error=RuntimeError("fake-accounting-secret"))
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(permit=permit, transport=transport)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "accounting_failure")
        self.assertEqual(result.failure.sent_state, "sent")
        self.assertEqual(result.failure.accounting_state, "settlement_failed")
        self.assertEqual(transport.calls, 1)

    def test_provider_statuses_and_redirect_have_one_invocation_and_no_fallback(self) -> None:
        for status in (301, 401, 429, 500):
            with self.subTest(status=status):
                permit = FakePermit()
                response = FakeResponse(status, b'{"error":{"message":"fake-secret"}}')
                transport = FakeTransport(response)
                result = invoke(permit=permit, transport=transport)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, "redirect_refused" if status == 301 else "provider_http_error")
                self.assertEqual(result.failure.http_status, status)
                self.assertEqual(transport.calls, 1)
                self.assertEqual(permit.settle_calls[0][1], AttemptOutcome.PROVIDER_FAILED)

    def test_http_error_response_body_is_bounded(self) -> None:
        body = b"x" * (MAX_RESPONSE_BYTES + 1)
        response = FakeResponse(429, body, ignore_limit=True)
        error = HTTPError(
            openrouter.OPENROUTER_URL,
            429,
            "fake error secret",
            {},
            response,
        )
        permit = FakePermit()
        transport = FakeTransport(error=error)
        result = invoke(permit=permit, transport=transport)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "response_too_large")
        self.assertEqual(response.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(response.closed)
        self.assertEqual(transport.calls, 1)

    def test_timeout_and_cancel_are_terminal_and_never_retried(self) -> None:
        for error, category, outcome in (
            (TimeoutError(), "timeout", AttemptOutcome.UNKNOWN_SENT),
            (TransportCancelled(), "cancelled", AttemptOutcome.CANCELLED),
        ):
            with self.subTest(category=category):
                permit = FakePermit()
                transport = FakeTransport(error=error)
                result = invoke(permit=permit, transport=transport)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, category)
                self.assertEqual(transport.calls, 1)
                self.assertEqual(permit.settle_calls[0][1], outcome)
                self.assertEqual(permit.settle_calls[0][2], SentState.UNKNOWN)

    def test_response_timeout_closes_and_marks_send_unknown(self) -> None:
        for error, category, outcome in (
            (TimeoutError(), "timeout", AttemptOutcome.UNKNOWN_SENT),
            (TransportCancelled(), "cancelled", AttemptOutcome.CANCELLED),
        ):
            with self.subTest(category=category):
                response = FakeResponse(200, b"", read_error=error)
                permit = FakePermit()
                result = invoke(permit=permit, transport=FakeTransport(response))
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, category)
                self.assertEqual(result.failure.sent_state, "unknown")
                self.assertEqual(permit.settle_calls[0][1], outcome)
                self.assertTrue(response.closed)

    def test_oversized_inputs_and_request_are_rejected_before_admission(self) -> None:
        cases = (
            ("context", "x" * (MAX_CONTEXT_BYTES + 1), VISITOR, "invalid_context"),
            ("visitor", CONTEXT, "x" * (MAX_VISITOR_TEXT_BYTES + 1), "invalid_visitor_text"),
            (
                "request",
                "x" * MAX_CONTEXT_BYTES,
                "x" * MAX_VISITOR_TEXT_BYTES,
                "request_too_large",
            ),
        )
        for name, context, visitor, category in cases:
            with self.subTest(name=name):
                permit = FakePermit()
                transport = FakeTransport(FakeResponse(200, response_body()))
                result = invoke(
                    permit=permit,
                    transport=transport,
                    context=context,
                    visitor=visitor,
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, category)
                self.assertEqual(permit.reserve_calls, [])
                self.assertEqual(transport.calls, 0)

    def test_oversized_response_is_bounded_even_when_length_is_lying(self) -> None:
        response = FakeResponse(
            200,
            b"x" * (MAX_RESPONSE_BYTES + 1),
            ignore_limit=True,
        )
        permit = FakePermit()
        result = invoke(permit=permit, transport=FakeTransport(response))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "response_too_large")
        self.assertEqual(response.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(response.closed)

    def test_short_or_truncated_nonstreaming_body_is_rejected(self) -> None:
        body = response_body()
        response = FakeResponse(200, body[: len(body) // 2])
        result = invoke(transport=FakeTransport(response))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "malformed_json")

    def test_invalid_status_and_transport_data_are_safe_failures(self) -> None:
        for status in ("200", None):
            with self.subTest(status=status):
                result = invoke(
                    transport=FakeTransport(FakeResponse(status, b"{}")),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, "malformed_status")

    def test_strict_json_and_assistant_shape_rejections(self) -> None:
        malformed_reply_type = response_body(
            inner_content=json.dumps(
                {"reply": 1, "action": "x", "target": None},
                separators=(",", ":"),
            )
        )
        malformed_reply_extra = response_body(
            inner_content=json.dumps(
                {"reply": "x", "action": "x", "target": None, "extra": 1},
                separators=(",", ":"),
            )
        )
        unexpected_outer = response_body().replace(
            b'"provider":"fixture-provider"',
            b'"provider":"fixture-provider","unexpected":1',
        )
        nonfinite_usage = response_body().replace(
            b'"provider":"fixture-provider"',
            b'"provider":"fixture-provider","usage":{"prompt_tokens":NaN}',
        )
        duplicate_usage = response_body().replace(
            b'"provider":"fixture-provider"',
            b'"provider":"fixture-provider","usage":{"prompt_tokens":1,"prompt_tokens":2}',
        )
        fixtures = (
            (b"{", "malformed_json"),
            (b'{"choices":[]}', "malformed_choices"),
            (b'{"choices":[{"message":{"role":"assistant","content":"{}"},"finish_reason":"length"}]}', "truncated_response"),
            (
                b'{"choices":[{"message":{"role":"assistant","content":"{}","tool_calls":[]},"finish_reason":"stop"}]}',
                "tool_calls_not_allowed",
            ),
            (
                b'{"choices":[{"message":{"role":"assistant","content":"{}","refusal":"fake"},"finish_reason":"stop"}]}',
                "refusal_not_allowed",
            ),
            (malformed_reply_type, "malformed_reply"),
            (malformed_reply_extra, "malformed_reply"),
            (unexpected_outer, "malformed_response"),
            (nonfinite_usage, "nonfinite_json_number"),
            (duplicate_usage, "duplicate_json_key"),
        )
        for body, category in fixtures:
            with self.subTest(category=category):
                result = invoke(transport=FakeTransport(FakeResponse(200, body)))
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.category, category)

    def test_provider_error_is_structured_without_provider_text(self) -> None:
        body = b'{"error":{"code":401,"message":"fake-provider-secret-and-prompt"}}'
        result = invoke(transport=FakeTransport(FakeResponse(200, body)))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "provider_error")
        self.assertNotIn("fake-provider-secret", repr(result))

    def test_reply_boundary_and_json_depth_are_bounded(self) -> None:
        result = invoke(
            transport=FakeTransport(
                FakeResponse(
                    200,
                    response_body(reply="x" * (MAX_REPLY_BYTES + 1)),
                )
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "reply_too_large")

        nested: object = "x"
        for _ in range(openrouter.MAX_JSON_DEPTH + 2):
            nested = [nested]
        deep_body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"reply": "x", "action": "x", "target": None}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "nested": nested,
            }
        ).encode()
        result = invoke(transport=FakeTransport(FakeResponse(200, deep_body)))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "json_too_deep")

    def test_usage_anomalies_are_untrusted_and_do_not_reject_completion(self) -> None:
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 1751,
            "total_tokens": 1852,
            "cost": 0,
            "completion_tokens_details": {"reasoning_tokens": 1849},
        }
        result = invoke(
            transport=FakeTransport(
                FakeResponse(
                    200,
                    response_body(
                        usage=usage,
                        model="fixture/unknown-free-model",
                    ),
                )
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.usage.completion_tokens, 1751)
        self.assertEqual(result.usage.reasoning_tokens, 1849)
        self.assertIn("completion_tokens_exceed_requested_max_tokens", result.telemetry_anomalies)
        self.assertIn("reasoning_tokens_exceed_completion_tokens", result.telemetry_anomalies)
        self.assertIn("usage_inconsistent", result.telemetry_anomalies)
        self.assertIn("reported_model_unverified", result.telemetry_anomalies)
        self.assertNotIn("reported_cost_nonzero", result.telemetry_anomalies)

    def test_changing_unknown_reported_models_remain_untrusted_telemetry(self) -> None:
        for model in ("fixture/model-a", "fixture/model-b"):
            with self.subTest(model=model):
                result = invoke(
                    transport=FakeTransport(
                        FakeResponse(200, response_body(model=model, usage={}))
                    )
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.reported_model, model)
                self.assertIn("reported_model_unverified", result.telemetry_anomalies)

    def test_proposal_has_no_world_side_effect(self) -> None:
        world: list[str] = []
        result = invoke()
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "propose")
        self.assertEqual(world, [])

    def test_expired_after_reservation_is_cancelled_without_send(self) -> None:
        clock = FakeClock()
        permit = FakePermit(on_reserve=lambda: setattr(clock, "now", 131.0))
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(permit=permit, transport=transport, clock=clock)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "deadline_exceeded")
        self.assertEqual(result.failure.sent_state, "not_sent")
        self.assertEqual(transport.calls, 0)
        self.assertEqual(permit.settle_calls[0][1], AttemptOutcome.CANCELLED)
        self.assertEqual(permit.settle_calls[0][2], SentState.NOT_SENT)

    def test_response_past_deadline_is_unknown_sent(self) -> None:
        clock = FakeClock()
        response = FakeResponse(200, response_body())

        def send_and_expire(request, *, timeout):
            clock.now = 131.0
            return response

        transport = FakeTransport(response)
        transport.send = send_and_expire
        permit = FakePermit()
        result = invoke(permit=permit, transport=transport, clock=clock)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "deadline_exceeded")
        self.assertEqual(result.failure.sent_state, "unknown")
        self.assertEqual(permit.settle_calls[0][1], AttemptOutcome.UNKNOWN_SENT)

    def test_nonfinite_clock_fails_before_admission(self) -> None:
        permit = FakePermit()
        transport = FakeTransport(FakeResponse(200, response_body()))
        result = invoke(
            permit=permit,
            transport=transport,
            clock=lambda: float("inf"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "clock_failure")
        self.assertEqual(permit.reserve_calls, [])
        self.assertEqual(transport.calls, 0)

    def test_real_transport_redirect_handler_is_terminal(self) -> None:
        with self.assertRaises(RedirectRefused):
            openrouter._NoRedirectHandler().redirect_request()

    def test_http_error_exception_repr_is_not_returned(self) -> None:
        response = FakeResponse(503, b'{"message":"fake-provider-secret"}')
        error = HTTPError(
            openrouter.OPENROUTER_URL,
            503,
            "fake-secret-in-exception",
            {},
            response,
        )
        result = invoke(transport=FakeTransport(error=error))
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.category, "provider_http_error")
        self.assertNotIn("fake-secret-in-exception", repr(result))


if __name__ == "__main__":
    unittest.main()
