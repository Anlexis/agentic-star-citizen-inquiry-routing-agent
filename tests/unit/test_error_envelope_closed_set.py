# GOV-C2-003 — the caller-visible ERROR envelope carries closed-set labels only.
#
# On any non-success invoke the caller must receive values this template chose
# from a closed set — a constant reason code — and nothing read from error_log
# or from any other node- or framework-authored string. In this repository the
# live channel is Graph.get_output(): the backbone routes every non-success
# status straight to finalize, and the override used to copy state["error_log"]
# into the invoke body. An entry there can be the output boundary's refusal
# line, a caller-data rejection, a trust denial, or — through the framework's
# exception wrapping in BaseNode.__call__ — an exception's message and its
# traceback with absolute source paths.
#
# Two surfaces are held here:
#   - Graph.get_output(), parameterised over every non-success shape the state
#     can take at finalize, including the ones a compiled run cannot be coaxed
#     into (a foreign value in the reason slot, a surviving pre-gate `result`);
#   - SecurityGateOutputNode's refusal delta, parameterised over every layer
#     that can refuse and over both entry points (execute() and the framework
#     pipeline): it records the closed-set reason and re-emits nothing.
# The full path through the real ASGI /invoke is held in
# tests/proof_of_boundary/test_invoke_e2e.py.
#
# The sentinel is deliberately NOT credential-shaped and carries no trace
# fragment: a redaction-based envelope passes it straight through, which is the
# defect these tests must fail on — not one a redaction happened to catch.

import json
from unittest.mock import MagicMock

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.nodes.security_gate_output_node import (
    ERROR_REASONS,
    SecurityGateOutputNode,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    error_envelope,
)

# Assembled at runtime (never a committed literal): a name, an email and a
# token-shaped fragment — what an echoed upstream body or a wrapped exception
# message can carry.
_FRAGMENTS = ("A. Tanaka", "a.tanaka@example.com", "sk-" + "live-xxx")
_SENTINEL = (
    "boom: upstream said {'customer':'"
    + _FRAGMENTS[0]
    + "','email':'"
    + _FRAGMENTS[1]
    + "','token':'"
    + _FRAGMENTS[2]
    + "'}"
)
_MARKERS = (_SENTINEL, "upstream said", *_FRAGMENTS)

# Framework-authored entries of the shapes the real wheel writes — internal
# too, never projected: the trust gate's denial, and the exception wrapper's
# `[Node] message` plus traceback.
_TRUST_DENIAL = "[ValidateInputNode] trust gate denied: required=verified_external, caller=anonymous"
_WRAPPED_EXCEPTION = (
    "[GovInquiryWorkflowGraphNode] " + _SENTINEL + '\nTraceback (most recent call last):\n  File "/abs/x.py", line 1'
)
_INTERNAL_ENTRIES = [_SENTINEL, _TRUST_DENIAL, _WRAPPED_EXCEPTION]
_INTERNAL_MARKERS = (*_MARKERS, "trust gate denied", "Traceback", 'File "')

_STRUCTURED_KEYS = (
    "intent_category",
    "intent_confidence",
    "urgency_level",
    "department_assignment",
    "routing_metadata",
)
_BASE_ENVELOPE_KEYS = {"output", "status", "trace_id", "correlation_id", "node_history"}
_DECISION = "Category: tax_question | Urgency: low | Department: taxation_dept | SLA: 24h"
_INQUIRY = "住民税の申告について教えてください"


def _strings(value):
    """Every string reachable in value: dict keys and values, list/tuple items,
    and the repr of anything else that is not a plain scalar."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _strings(child)
    elif value is not None and not isinstance(value, (bool, int, float)):
        yield repr(value)


def _found(mapping, *markers) -> list[str]:
    """The markers reachable anywhere inside mapping, walking nested values."""
    texts = list(_strings(mapping))
    return [marker for marker in markers if any(marker in text for text in texts)]


def _assert_internal_text_absent(mapping: dict) -> None:
    assert _found(mapping, *_INTERNAL_MARKERS) == [], mapping
    rendered = json.dumps(mapping, default=str)
    for marker in _INTERNAL_MARKERS:
        assert marker not in rendered


# ── Graph.get_output(): the invoke envelope on every non-success state ────────


def _finalize_state(**overrides) -> dict:
    """The outer state as finalize sees it after a non-success run: the
    internal channel carries every shape of entry the pipeline can put there."""
    state = {
        "status": AgentStatus.ERROR.value,
        "user_input": _INQUIRY,
        "validated_input": _INQUIRY,
        "pii_detected": False,
        "error_log": list(_INTERNAL_ENTRIES),
        "trace_id": "tr",
        "correlation_id": "co",
        "node_history": ["InitializeNode", "ValidateInputNode", "FinalizeNode"],
    }
    state.update(overrides)
    return state


_NON_SUCCESS_STATES = [
    pytest.param({}, _REASON_WORKFLOW_FAILED, id="caller-data-rejected-at-pre-process"),
    pytest.param({"status": AgentStatus.TIMEOUT.value}, _REASON_WORKFLOW_FAILED, id="timeout-status"),
    pytest.param({"error_reason": _REASON_OUTPUT_WITHHELD}, _REASON_OUTPUT_WITHHELD, id="output-boundary-refused"),
    pytest.param({"error_reason": _SENTINEL}, _REASON_WORKFLOW_FAILED, id="reason-outside-the-closed-set"),
    pytest.param({"error_reason": [_REASON_OUTPUT_WITHHELD]}, _REASON_WORKFLOW_FAILED, id="reason-not-a-string"),
    pytest.param({"result": _SENTINEL}, _REASON_WORKFLOW_FAILED, id="pre-gate-result-survives-in-state"),
    pytest.param({"formatted_output": _SENTINEL}, _REASON_WORKFLOW_FAILED, id="formatted-output-survives-in-state"),
    pytest.param(
        {
            "intent_category": _SENTINEL,
            "intent_confidence": 0.65,
            "urgency_level": _SENTINEL,
            "department_assignment": _SENTINEL,
            "routing_metadata": {"contact_key": _SENTINEL, "sla_hours": 24},
        },
        _REASON_WORKFLOW_FAILED,
        id="structured-fields-survive-in-state",
    ),
]


class TestInvokeErrorEnvelope:
    @pytest.mark.parametrize("overrides, reason", _NON_SUCCESS_STATES)
    def test_error_values_are_drawn_from_the_declared_constants(self, overrides, reason):
        out = Graph().get_output(_finalize_state(**overrides))

        assert out["status"] != AgentStatus.SUCCESS.value
        assert set(out["error"]) == {"reason"}
        assert out["error"]["reason"] in ERROR_REASONS
        assert out["error"] == error_envelope(reason)
        assert out["error"], "the error envelope must stay truthy"
        assert out["output"] is None
        assert "error_log" not in out
        for key in _STRUCTURED_KEYS:
            assert out[key] is None, key

    @pytest.mark.parametrize("overrides, reason", _NON_SUCCESS_STATES)
    def test_internal_text_appears_nowhere_in_the_invoke_envelope(self, overrides, reason):
        out = Graph().get_output(_finalize_state(**overrides))
        _assert_internal_text_absent(out)

    @pytest.mark.parametrize("overrides, reason", _NON_SUCCESS_STATES)
    def test_the_envelope_keys_are_a_fixed_set(self, overrides, reason):
        out = Graph().get_output(_finalize_state(**overrides))
        assert set(out) == _BASE_ENVELOPE_KEYS | set(_STRUCTURED_KEYS) | {"pii_detected", "error"}

    def test_success_envelope_is_unchanged(self):
        state = _finalize_state(
            status=AgentStatus.SUCCESS.value,
            formatted_output=_DECISION,
            result=_DECISION,
            intent_category="tax_question",
            intent_confidence=0.65,
            urgency_level="low",
            department_assignment="taxation_dept",
            routing_metadata={"department": "taxation_dept", "sla_hours": 24, "contact_key": "taxation_contact"},
            error_log=[],
        )
        out = Graph().get_output(state)

        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["output"] == _DECISION
        assert out["department_assignment"] == "taxation_dept"
        assert out["routing_metadata"]["sla_hours"] == 24
        assert "error" not in out
        assert "error_log" not in out
        assert set(out) == _BASE_ENVELOPE_KEYS | set(_STRUCTURED_KEYS) | {"pii_detected"}

    def test_a_stale_reason_cannot_turn_a_success_into_an_error(self):
        state = _finalize_state(
            status=AgentStatus.SUCCESS.value,
            formatted_output=_DECISION,
            result=_DECISION,
            error_reason=_REASON_OUTPUT_WITHHELD,
            error_log=[],
        )
        out = Graph().get_output(state)
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["output"] == _DECISION
        assert "error" not in out

    def test_the_probe_finds_the_sentinel_where_it_lives(self):
        # Verify the verifier: the same walk DOES find every marker in a mapping
        # that carries it, so the "nowhere" assertions above are not vacuous.
        carrier = {"error_log": list(_INTERNAL_ENTRIES), "nested": {"deep": [{"k": _SENTINEL}]}}
        assert set(_found(carrier, *_INTERNAL_MARKERS)) == set(_INTERNAL_MARKERS)


# ── SecurityGateOutputNode: the refusal delta on every layer that can refuse ──


@pytest.fixture
def audit_spy(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr("src.nodes.security_gate_output_node.emit_trace_event", spy)
    return spy


def _metadata(**overrides) -> dict:
    metadata = {
        "department": "taxation_dept",
        "sla_hours": 24,
        "escalation_path": None,
        "contact_key": "taxation_contact",
        "urgency_level": "low",
        "intent_category": "tax_question",
    }
    metadata.update(overrides)
    return metadata


def _gate_state(**overrides) -> dict:
    state = {
        "department_assignment": "taxation_dept",
        "intent_category": "tax_question",
        "urgency_level": "low",
        "routing_metadata": _metadata(),
        "pii_detected": False,
        "session_id": "closed-set-session",
        "trace_id": "closed-set-trace",
        "correlation_id": "closed-set-test",
        "user_input": _INQUIRY,
        "validated_input": _INQUIRY,
        # The internal channel already carries the sentinel when the boundary runs.
        "error_log": [_SENTINEL],
        # For the framework pipeline entry point (node(state)).
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


_REFUSAL_PATHS = [
    pytest.param({"department_assignment": "taxation dept (see inquiry)"}, id="layer1-free-text-department"),
    pytest.param({"intent_category": None}, id="layer1-missing-category"),
    pytest.param({"routing_metadata": _metadata(sla_hours=721)}, id="layer1-deadline-out-of-range"),
    pytest.param({"routing_metadata": _metadata(escalation_path="Supervisor Name")}, id="layer1-free-text-escalation"),
    pytest.param({"routing_metadata": {}}, id="layer1-missing-metadata"),
    pytest.param({"department_assignment": "123456789012"}, id="layer3-individual-number-shaped-label"),
    pytest.param({"department_assignment": "1234567"}, id="layer3-postal-code-shaped-label"),
]
_ENTRY_POINTS = ["execute", "call"]


def _drive(state: dict, entry: str) -> dict:
    node = SecurityGateOutputNode()
    return node.execute(state) if entry == "execute" else node(state)


class TestRefusalRecordsTheClosedSetReason:
    @pytest.mark.parametrize("overrides", _REFUSAL_PATHS)
    @pytest.mark.parametrize("entry", _ENTRY_POINTS)
    def test_refusal_delta_carries_the_declared_reason_and_publishes_nothing(self, audit_spy, overrides, entry):
        result = _drive(_gate_state(**overrides), entry)

        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_reason"] == _REASON_OUTPUT_WITHHELD
        assert result["error_reason"] in ERROR_REASONS
        assert "formatted_output" not in result, "a blocked output must never be published"
        assert "result" not in result
        assert "audit_entry" not in result

    @pytest.mark.parametrize("overrides", _REFUSAL_PATHS)
    @pytest.mark.parametrize("entry", _ENTRY_POINTS)
    def test_refusal_writes_its_own_line_and_re_emits_nothing(self, audit_spy, overrides, entry):
        result = _drive(_gate_state(**overrides), entry)

        # One line, this node's own, naming a field or a pattern — never a value.
        assert len(result["error_log"]) == 1, result["error_log"]
        assert result["error_log"][0].startswith("SecurityGateOutputNode: output blocked")
        # The entry already in error_log is not re-emitted (the state reducer
        # appends, so it would be duplicated) and appears nowhere in the delta.
        assert _found(result, *_MARKERS) == [], result

    @pytest.mark.parametrize("overrides", _REFUSAL_PATHS)
    def test_the_audit_event_carries_the_reason_and_no_value(self, audit_spy, overrides):
        SecurityGateOutputNode().execute(_gate_state(**overrides))

        payloads = [call.args[1] for call in audit_spy.call_args_list if len(call.args) > 1]
        assert payloads, "a refusal must be audited"
        assert payloads[-1]["reason"] == _REASON_OUTPUT_WITHHELD
        for payload in payloads:
            assert _found(payload, *_MARKERS) == [], payload

    def test_a_clean_decision_records_no_reason(self, audit_spy):
        result = SecurityGateOutputNode().execute(_gate_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "error_reason" not in result
        assert result["formatted_output"] == _DECISION


# ── The envelope builder itself ───────────────────────────────────────────────


class TestErrorEnvelopeBuilder:
    def test_every_declared_reason_builds_a_truthy_single_key_envelope(self):
        for reason in ERROR_REASONS:
            envelope = error_envelope(reason)
            assert envelope
            assert envelope == {"reason": reason}

    def test_a_reason_outside_the_closed_set_is_refused_and_not_echoed(self):
        with pytest.raises(ValueError) as info:
            error_envelope(_SENTINEL)
        assert _SENTINEL not in str(info.value)
