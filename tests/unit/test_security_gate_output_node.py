# GOV-C2-003 — Unit Tests: SecurityGateOutputNode
#
# Covers GOV-TC-050 through GOV-TC-062 (docs/03_test_spec.md): the output
# boundary and the audit record.
#
# The gate's stated invariant is that the caller-facing output carries the
# routing decision and nothing else. These tests probe it from both sides:
# every representation that could carry something else must be refused, and
# every legitimate routing decision must render byte-for-byte.

from unittest.mock import MagicMock

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.security_gate_output_node import SecurityGateOutputNode


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    """Patch emit_trace_event at the node module. Returns the spy for tests that need it."""
    spy = MagicMock()
    monkeypatch.setattr("src.nodes.security_gate_output_node.emit_trace_event", spy)
    return spy


def _make_state(**overrides):
    """Build a minimal valid post_process state."""
    defaults = {
        "department_assignment": "taxation_dept",
        "intent_category": "tax_question",
        "urgency_level": "low",
        "routing_metadata": {
            "department": "taxation_dept",
            "sla_hours": 24,
            "escalation_path": None,
            "contact_key": "taxation_contact",
            "urgency_level": "low",
            "intent_category": "tax_question",
        },
        "pii_detected": False,
        "session_id": "test-session-001",
        "trace_id": "test-trace-001",
        "user_input": "住民税の申告について教えてください",  # must NOT leak into output
    }
    defaults.update(overrides)
    return defaults


def _with_metadata(**meta_overrides):
    """State whose routing_metadata carries the given overrides."""
    state = _make_state()
    metadata = dict(state["routing_metadata"])
    metadata.update(meta_overrides)
    state["routing_metadata"] = metadata
    return state


class TestRoutingDecisionRendering:
    """The legitimate decision renders exactly, on every path."""

    def setup_method(self):
        self.node = SecurityGateOutputNode()

    def test_success_path_contains_routing_fields(self, mock_emit):
        """GOV-TC-050: clean routing → the decision fields appear in the output."""
        result = self.node.execute(_make_state())
        assert result["status"] == AgentStatus.SUCCESS
        assert "taxation_dept" in result["formatted_output"]
        assert "low" in result["formatted_output"]
        assert "SLA: 24h" in result["formatted_output"]

    def test_formatted_output_and_result_are_the_same_gated_value(self, mock_emit):
        """Both caller-facing keys carry the post-gate value — never a pre-gate one."""
        result = self.node.execute(_make_state())
        assert result["result"] == result["formatted_output"]

    def test_pii_detected_true_adds_notice(self, mock_emit):
        """GOV-TC-051: a stripped-identifier inquiry says so, without naming anything."""
        result = self.node.execute(_make_state(pii_detected=True))
        assert result["status"] == AgentStatus.SUCCESS
        assert "personal information" in result["formatted_output"]

    def test_escalation_path_in_output_when_high_urgency(self, mock_emit):
        """GOV-TC-052: high urgency with an escalation path renders it."""
        state = _make_state(
            urgency_level="high",
            department_assignment="public_safety_dept",
            intent_category="public_safety",
            routing_metadata={
                "department": "public_safety_dept",
                "sla_hours": 1,
                "escalation_path": "emergency_line",
                "contact_key": "public_safety_contact",
                "urgency_level": "high",
                "intent_category": "public_safety",
            },
        )
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert "emergency_line" in result["formatted_output"]

    def test_no_escalation_section_when_none(self, mock_emit):
        """No escalation path → no escalation section at all (never an empty one)."""
        result = self.node.execute(_make_state())
        assert "Escalation" not in result["formatted_output"]

    @pytest.mark.parametrize("sla_hours", [1, 24, 720])
    def test_boundary_sla_values_render_unchanged(self, mock_emit, sla_hours):
        """The accepted deadline range renders byte-identically at both ends."""
        result = self.node.execute(_with_metadata(sla_hours=sla_hours))
        assert result["status"] == AgentStatus.SUCCESS
        assert f"SLA: {sla_hours}h" in result["formatted_output"]


class TestOutputBoundaryFailsClosed:
    """Anything that is not a routing value is refused, and nothing is published."""

    def setup_method(self):
        self.node = SecurityGateOutputNode()

    def test_inquiry_text_never_reaches_the_output(self, mock_emit):
        """GOV-TC-054: the inquiry text must not appear in the rendered decision."""
        state = _make_state(user_input="特別な市民の問い合わせテキスト")
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert "特別な市民の問い合わせテキスト" not in result["formatted_output"]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("department_assignment", "住民税の申告について教えてください"),
            ("department_assignment", "dept with spaces"),
            ("department_assignment", "Dept-Upper"),
            ("department_assignment", ""),
            ("department_assignment", None),
            ("department_assignment", 42),
            ("department_assignment", "a" * 49),
            ("intent_category", "tax question"),
            ("intent_category", None),
            ("urgency_level", "very high"),
            ("urgency_level", 3),
        ],
        ids=[
            "dept-inquiry-text",
            "dept-spaces",
            "dept-uppercase",
            "dept-empty",
            "dept-none",
            "dept-number",
            "dept-too-long",
            "category-spaces",
            "category-none",
            "urgency-spaces",
            "urgency-number",
        ],
    )
    def test_non_inert_rendered_field_is_refused(self, mock_emit, field, value):
        """GOV-TC-056: a rendered field that is not an inert identifier blocks the output."""
        result = self.node.execute(_make_state(**{field: value}))
        assert result["status"] == AgentStatus.ERROR
        assert "formatted_output" not in result, "a blocked output must never be published"
        assert "result" not in result
        assert "audit_entry" not in result
        assert any(field in str(e) for e in result["error_log"]), result["error_log"]

    @pytest.mark.parametrize(
        "sla_hours",
        [0, -1, 721, 24.5, True, "24", None, float("nan"), float("inf")],
        ids=["zero", "negative", "over-max", "float", "bool", "string", "none", "nan", "inf"],
    )
    def test_out_of_range_or_non_integer_deadline_is_refused(self, mock_emit, sla_hours):
        """GOV-TC-057: the deadline must be a whole number of hours in range.

        Non-finite values are covered explicitly: they survive a float() parse
        and compare False against every bound, so a range check written as a
        comparison alone would let them through.
        """
        result = self.node.execute(_with_metadata(sla_hours=sla_hours))
        assert result["status"] == AgentStatus.ERROR
        assert "formatted_output" not in result
        assert any("sla_hours" in str(e) for e in result["error_log"])

    @pytest.mark.parametrize(
        "escalation_path",
        ["escalate to 03-1234-5678", "Supervisor Name", "a" * 49, 7],
        ids=["phone-fragment", "free-text", "too-long", "number"],
    )
    def test_non_inert_escalation_path_is_refused(self, mock_emit, escalation_path):
        result = self.node.execute(_with_metadata(escalation_path=escalation_path))
        assert result["status"] == AgentStatus.ERROR
        assert any("escalation_path" in str(e) for e in result["error_log"])

    def test_missing_routing_metadata_is_refused(self, mock_emit):
        """No routing metadata means no deadline — the output cannot be assembled."""
        result = self.node.execute(_make_state(routing_metadata={}))
        assert result["status"] == AgentStatus.ERROR
        assert "formatted_output" not in result

    def test_rejected_value_is_never_echoed_back(self, mock_emit):
        """A refusal names the field, never the offending value."""
        secret_looking = "ak-abcdefghijklmnopqrstuvwx"
        result = self.node.execute(_make_state(department_assignment=f"{secret_looking} dept"))
        assert result["status"] == AgentStatus.ERROR
        assert secret_looking not in str(result["error_log"])
        payloads = [c.args[1] for c in mock_emit.call_args_list if len(c.args) > 1]
        for payload in payloads:
            assert secret_looking not in str(payload)


class TestAuditRecord:
    """The audit record documents the decision without carrying the inquiry."""

    def setup_method(self):
        self.node = SecurityGateOutputNode()

    def test_audit_entry_does_not_contain_the_inquiry(self, mock_emit):
        """GOV-TC-053: the audit record must not include the inquiry text."""
        inquiry = "とても機密性の高い市民問い合わせ"
        result = self.node.execute(_make_state(user_input=inquiry))
        assert result["status"] == AgentStatus.SUCCESS
        audit = result["audit_entry"]
        assert "user_input" not in audit
        assert inquiry not in str(audit)

    def test_audit_entry_has_required_fields(self, mock_emit):
        """GOV-TC-053b: the record identifies the session and the decision."""
        result = self.node.execute(_make_state())
        audit = result["audit_entry"]
        assert audit["session_id"] == "test-session-001"
        assert audit["intent_category"] == "tax_question"
        assert audit["urgency_level"] == "low"
        assert audit["department_assignment"] == "taxation_dept"
        assert audit["sla_hours"] == 24
        assert audit["contact_key"] == "taxation_contact"
        assert "pii_detected" in audit

    def test_audit_payload_does_not_contain_the_inquiry(self, mock_emit):
        """GOV-TC-055: the emitted payload (call.args[1]) excludes the inquiry text.

        Asserts against the payload argument, NOT repr(call_args_list) — the
        latter includes the state argument, which legitimately still carries
        user_input.
        """
        inquiry = "非常に個人的な市民の問い合わせ内容"
        result = self.node.execute(_make_state(user_input=inquiry))
        assert result["status"] == AgentStatus.SUCCESS

        payloads = [c.args[1] for c in mock_emit.call_args_list if len(c.args) > 1]
        for payload in payloads:
            assert inquiry not in str(payload), f"inquiry text leaked into an audit payload: {payload}"

    def test_audit_event_emitted(self, mock_emit):
        self.node.execute(_make_state())
        assert mock_emit.called

    def test_returns_only_changed_keys(self, mock_emit):
        """Node contract: execute() returns only the keys it writes."""
        result = self.node.execute(_make_state())
        assert set(result.keys()) == {"formatted_output", "result", "audit_entry", "status"}
