# PB: End-to-end business behaviour through POST /invoke — src/api/server.py
#
# Proves the supported caller-data contract produces REAL routing decisions
# through the full nested graph (outer backbone → inner domain pipeline), not
# only the shipped baseline:
#   - a real category / urgency / department decision for a plain inquiry
#   - a CALLER-supplied department table changing the routed department, which
#     also proves the caller settings cross the outer→inner graph boundary (the
#     framework does not forward them — see src/graph/context_bridge.py)
#   - every urgency path (high / medium / low) reachable end to end
#   - validation rejections for malformed caller data, including non-finite
#     numerics arriving as raw JSON NaN/Infinity
#   - personal identifiers stripped before anything is decided or returned
#   - the adapter-level size cap and the Bearer-auth boundary
#   - an output scan: the response carries the routing decision only
#
# The app is driven through its real ASGI interface: every request crosses the
# entry-point auth, the outer trust and ingest gates, the settings bridge into
# the inner graph, all three domain nodes, and the output boundary.

import asyncio
import json

import pytest

from src.api import server as server_module  # noqa: F401  (import = boot check)
from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"

# One keyword hit (道路) → infrastructure at confidence 0.65, medium urgency.
_INFRASTRUCTURE_INQUIRY = "道路に穴が開いている"
# public_safety category → high urgency, halved deadline, escalation set.
_URGENT_INQUIRY = "緊急事態が発生した、警察を呼んでほしい"
# No category keyword at all → the general desk, low urgency.
_UNCLASSIFIED_INQUIRY = "hello, I have a general question"
# Carries a phone number the ingest stage must strip.
_INQUIRY_WITH_PII = "道路の陥没について連絡先は090-1234-5678です"

_CALLER_TABLE = {
    "infrastructure": {
        "department": "doboku_ka",
        "sla_hours": 6,
        "escalation_path": "doboku_kacho",
        "contact_key": "doboku_contact",
    },
    "other": {
        "department": "sogo_madoguchi",
        "sla_hours": 96,
        "escalation_path": None,
        "contact_key": "sogo_contact",
    },
}


def _post_invoke(payload: dict, with_auth: bool = True) -> tuple[int, dict, str]:
    """POST /invoke through the real ASGI app; returns (status, body, raw_text)."""
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if with_auth:
        headers.append((b"authorization", f"Bearer {_TOKEN}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    messages = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = sent["body"].decode()
    return start["status"], json.loads(raw or "{}"), raw


@pytest.fixture(autouse=True)
def token_configured(monkeypatch):
    """Deployed server environment: INVOKE_AUTH_TOKEN set, caller uses Bearer."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)


def _invoke(inquiry: str, input_context: dict | None = None) -> dict:
    status_code, body, _ = _post_invoke(
        {"input": inquiry, "session_id": "pb-invoke-e2e", "input_context": input_context or {}}
    )
    assert status_code == 200, f"expected 200, got {status_code}: {body}"
    return body


class TestInvokeEndToEnd:
    def test_plain_inquiry_produces_a_real_routing_decision(self):
        """The shipped baseline decides a real category, urgency and department."""
        body = _invoke(_INFRASTRUCTURE_INQUIRY)

        assert body["status"] == "success"
        assert body["intent_category"] == "infrastructure"
        assert body["urgency_level"] == "medium"
        assert body["department_assignment"] == "public_works_dept"
        assert body["routing_metadata"]["sla_hours"] == 24
        assert body["routing_metadata"]["contact_key"] == "public_works_contact"
        assert "Department: public_works_dept" in body["output"]

    def test_caller_table_changes_the_routed_department(self):
        """A caller-supplied table must reach the inner routing node and win.

        This is also the proof that the settings bridge works end to end: the
        framework does not forward caller context into a nested graph, so a
        routed department of doboku_ka can only come from the bridge.
        """
        body = _invoke(_INFRASTRUCTURE_INQUIRY, {"routing_table": _CALLER_TABLE})

        assert body["status"] == "success"
        assert body["department_assignment"] == "doboku_ka"
        assert body["routing_metadata"]["sla_hours"] == 6
        assert body["routing_metadata"]["contact_key"] == "doboku_contact"
        assert "doboku_ka" in body["output"]
        assert "public_works_dept" not in body["output"]

    def test_urgent_inquiry_takes_the_high_severity_path(self):
        """High urgency halves the deadline and always carries an escalation."""
        body = _invoke(_URGENT_INQUIRY)

        assert body["status"] == "success"
        assert body["intent_category"] == "public_safety"
        assert body["urgency_level"] == "high"
        assert body["department_assignment"] == "public_safety_dept"
        assert body["routing_metadata"]["sla_hours"] == 1  # 2h standard, halved
        assert body["routing_metadata"]["escalation_path"] == "emergency_line"
        assert "Escalation: emergency_line" in body["output"]

    def test_unclassified_inquiry_goes_to_the_general_desk(self):
        """No category signal → the catch-all, at the low-urgency deadline."""
        body = _invoke(_UNCLASSIFIED_INQUIRY)

        assert body["status"] == "success"
        assert body["intent_category"] == "other"
        assert body["urgency_level"] == "low"
        assert body["department_assignment"] == "general_inquiry_dept"
        assert body["routing_metadata"]["sla_hours"] == 72

    def test_caller_confidence_bar_reroutes_to_the_general_desk(self):
        """A caller may tighten the classification bar through /invoke."""
        routed = _invoke(_INFRASTRUCTURE_INQUIRY)
        assert routed["department_assignment"] == "public_works_dept"

        strict = _invoke(_INFRASTRUCTURE_INQUIRY, {"classification": {"min_confidence": 0.9}})
        assert strict["status"] == "success"
        assert strict["intent_category"] == "other"
        assert strict["department_assignment"] == "general_inquiry_dept"

    def test_caller_sla_factor_changes_the_deadline(self):
        body = _invoke(_URGENT_INQUIRY, {"urgency": {"high_sla_factor": 1.0}})
        assert body["status"] == "success"
        assert body["routing_metadata"]["sla_hours"] == 2  # standard deadline, uncompressed

    def test_personal_identifiers_are_stripped_and_never_returned(self):
        """The inquiry is processed, but the identifier never reaches the caller."""
        status_code, body, raw = _post_invoke(
            {"input": _INQUIRY_WITH_PII, "session_id": "pb-invoke-e2e", "input_context": {}}
        )
        assert status_code == 200
        assert body["status"] == "success"
        assert body["pii_detected"] is True
        assert "090-1234-5678" not in raw
        assert "personal information" in body["output"]

    def test_invalid_caller_table_is_rejected_without_echo(self):
        """Malformed caller data fails closed with the closed-set reason. The
        field-level wording stays on the internal channel (held at unit level
        in test_caller_data_contract.py), and the rejected value must never
        round-trip into the response."""
        bad_department = "INVALID DEPT WITH SPACES"
        status_code, body, raw = _post_invoke(
            {
                "input": _INFRASTRUCTURE_INQUIRY,
                "session_id": "pb-invoke-e2e",
                "input_context": {
                    "routing_table": {
                        "infrastructure": dict(_CALLER_TABLE["infrastructure"], department=bad_department)
                    }
                },
            }
        )
        assert status_code == 200
        assert body["status"] == "success", body
        # The run completes carrying the reason so the caller can correct the
        # table and send the inquiry again; no routing decision is published,
        # and the field-level wording stays on the internal channel.
        assert body.get("output"), body
        assert "error_log" not in body
        assert body.get("department_assignment") is None
        assert bad_department not in raw
        assert "routing_table" not in raw

    @pytest.mark.parametrize(
        "bad_value",
        ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf")],
        ids=["str-nan", "str-inf", "str-neginf", "raw-nan", "raw-inf", "raw-neginf"],
    )
    def test_non_finite_confidence_bar_fails_closed_not_open(self, bad_value):
        """A NaN/Infinity bar must ERROR with no decision — never a 'success with
        the bar silently disabled' fail-open. The raw floats cover Python json's
        bare-NaN extension reaching the request body."""
        body = _invoke(_INFRASTRUCTURE_INQUIRY, {"classification": {"min_confidence": bad_value}})
        assert body["status"] == "success", body
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )
        assert body.get("department_assignment") is None

    @pytest.mark.parametrize(
        "bad_value",
        ["NaN", "Infinity", float("nan"), float("inf")],
        ids=["str-nan", "str-inf", "raw-nan", "raw-inf"],
    )
    def test_non_finite_sla_factor_fails_closed(self, bad_value):
        body = _invoke(_URGENT_INQUIRY, {"urgency": {"high_sla_factor": bad_value}})
        assert body["status"] == "success", body
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )

    def test_oversized_input_context_is_capped_at_the_adapter(self):
        """The adapter rejects an oversized payload before it reaches the graph."""
        status_code, _, _ = _post_invoke(
            {
                "input": _INFRASTRUCTURE_INQUIRY,
                "session_id": "pb-invoke-e2e",
                "input_context": {"routing_table": {"other": {"department": "a" * 300_000}}},
            }
        )
        assert status_code == 413

    def test_missing_bearer_token_is_unauthorized(self):
        status_code, _, _ = _post_invoke(
            {"input": _INFRASTRUCTURE_INQUIRY, "session_id": "pb-invoke-e2e"},
            with_auth=False,
        )
        assert status_code == 401

    def test_output_carries_the_routing_decision_and_nothing_else(self):
        """The caller-facing string renders decision labels only — no inquiry
        text, no internal state, no credential or identifier material."""
        body = _invoke(_INFRASTRUCTURE_INQUIRY, {"routing_table": _CALLER_TABLE, "channel": "web_form"})
        output = body["output"]

        # The inquiry itself is never echoed.
        assert _INFRASTRUCTURE_INQUIRY not in output
        # No raw internal state in the external surface.
        assert '"routing_metadata"' not in output
        assert '"contact_key"' not in output
        assert "web_form" not in output
        # Only the decision labels, in the documented shape.
        assert output.startswith("Category: infrastructure | Urgency: medium | Department: doboku_ka | SLA: 6h")
        # The same scan the output boundary enforces.
        from src.nodes.security_gate_output_node import _scan_for_disallowed

        assert _scan_for_disallowed(output) is None


# ─────────────────────────────────────────────────────────────────────────────
# The non-success envelope is a closed set — at the boundary the caller sees.
#
# error_log is the internal channel: the state reducer appends to it and the
# audit trail reads it. Whatever a node — or the framework on a node's behalf —
# writes there must reach neither the body nor any nested value in it. The
# caller receives `error: {"reason": <constant>}`, `output: null` and no
# `error_log` key. Each case below makes a node author a recognisable sentinel
# into error_log during a REAL /invoke (a helper the node calls is replaced;
# the node, the graph and the adapter are not), then walks the whole body.
# ─────────────────────────────────────────────────────────────────────────────
def _sentinel() -> str:
    # Assembled at runtime so no credential-shaped literal is committed — and
    # deliberately not credential-shaped, so a redaction would pass it.
    token = "sk-" + "live-xxx"
    return "boom: upstream said {'customer':'A. Tanaka','email':'a.tanaka@example.com','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "a.tanaka@example.com", "boom: upstream", "sk-" + "live-")
_FRAMEWORK_MARKERS = ("Traceback", 'File "', "RuntimeError", "trust gate denied", "error_log")
_STRUCTURED_KEYS = (
    "intent_category",
    "intent_confidence",
    "urgency_level",
    "department_assignment",
    "routing_metadata",
)


def _strings(value):
    """Every string reachable in value: dict keys and values, list items, and
    the repr of anything else that is not a plain scalar."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)
    elif value is not None and not isinstance(value, (bool, int, float)):
        yield repr(value)


def _found(blob, *markers) -> list:
    texts = list(_strings(blob))
    return [marker for marker in markers if any(marker in text for text in texts)]


def _assert_closed_set_declined(body: dict, raw: str) -> None:
    """The published shape of a run declined over a value the caller can correct.

    The caller reads one sentence saying what to fix. Everything else the run
    touched - the field-level wording, the framework's own markers - stays on
    the internal channel, exactly as it does for a terminated run.
    """
    assert body["status"] == "success", body
    assert body.get("output"), body
    assert "error_log" not in body
    # A declined run produced none of these, so none is released: the keys are
    # absent from the envelope rather than present and empty.
    for key in _STRUCTURED_KEYS:
        assert body.get(key) is None, key
    assert _found(body, *_SENTINEL_FRAGMENTS, *_FRAMEWORK_MARKERS) == [], body
    for marker in (*_SENTINEL_FRAGMENTS, *_FRAMEWORK_MARKERS):
        assert marker not in raw


def _assert_closed_set_error(body: dict, raw: str, reason: str) -> None:
    assert body["status"] == "error", body
    assert body["output"] is None
    assert body["error"] == {"reason": reason}
    assert "error_log" not in body
    for key in _STRUCTURED_KEYS:
        assert body[key] is None, key
    assert _found(body, *_SENTINEL_FRAGMENTS, *_FRAMEWORK_MARKERS) == [], body
    for marker in (*_SENTINEL_FRAGMENTS, *_FRAMEWORK_MARKERS):
        assert marker not in raw


def _seeded_request(input_context: dict | None = None) -> dict:
    return {"input": _INFRASTRUCTURE_INQUIRY, "session_id": "pb-invoke-e2e", "input_context": input_context or {}}


class TestNonSuccessEnvelopeIsClosedSet:
    def test_a_caller_data_rejection_publishes_the_reason_only(self, monkeypatch):
        """The ingest node's table validator authors the sentinel into its
        rejection line; the request is declined at pre_process and the run
        completes carrying only the caller-facing reason."""
        monkeypatch.setattr("src.nodes.validate_input_node._validate_routing_table", lambda raw: (None, _sentinel()))
        status_code, body, raw = _post_invoke(_seeded_request({"routing_table": _CALLER_TABLE}))
        assert status_code == 200
        _assert_closed_set_declined(body, raw)

    def test_a_wrapped_exception_publishes_neither_its_message_nor_its_traceback(self, monkeypatch):
        """An inner node raises. The framework wraps `[Node] <message>` plus the
        traceback into error_log, the GraphNode re-raises it as SubgraphError
        (error_strategy="propagate") and the outer wrapper does it again — so
        by finalize the internal channel holds the message and two tracebacks
        with absolute source paths. None of it is the caller's."""

        def _raise(text):
            raise RuntimeError(_sentinel())

        monkeypatch.setattr("src.nodes.classify_intent_node._classify", _raise)
        status_code, body, raw = _post_invoke(_seeded_request())
        assert status_code == 200
        _assert_closed_set_error(body, raw, "workflow_failed")

    def test_an_output_boundary_refusal_publishes_the_reason_only(self, monkeypatch):
        """The boundary's grammar check names the field it refused; here the
        name it reports is the sentinel, so the refusal line carries it."""
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node._check_rendered_tokens", lambda *args: (_sentinel(), 0)
        )
        status_code, body, raw = _post_invoke(_seeded_request())
        assert status_code == 200
        _assert_closed_set_error(body, raw, "output_withheld")

    def test_a_trust_denial_publishes_the_reason_only(self, monkeypatch):
        """No token configured and no middleware: the request runs ANONYMOUS
        and the entry node's trust gate refuses it with a framework-authored
        line naming the node and the levels."""
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        status_code, body, raw = _post_invoke(_seeded_request(), with_auth=False)
        assert status_code == 200
        _assert_closed_set_error(body, raw, "workflow_failed")

    def test_the_sentinel_really_enters_the_internal_channel(self, monkeypatch):
        """Verify the verifier: the same replaced validator DOES put the
        sentinel into the ingest node's error_log, and the same walk DOES find
        it there — so the "nowhere in the body" assertions are not vacuous."""
        from src.nodes.validate_input_node import ValidateInputNode

        monkeypatch.setattr("src.nodes.validate_input_node._validate_routing_table", lambda raw: (None, _sentinel()))
        delta = ValidateInputNode().execute(
            {"user_input": _INFRASTRUCTURE_INQUIRY, "input_context": {"routing_table": _CALLER_TABLE}}
        )
        assert delta["status"] == "success"
        assert set(_found(delta, *_SENTINEL_FRAGMENTS)) == set(_SENTINEL_FRAGMENTS)
