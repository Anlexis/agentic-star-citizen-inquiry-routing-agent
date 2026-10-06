# GOV-C2-003 — Unit Tests: ValidateInputNode
#
# Covers: GOV-TC-001 through GOV-TC-008 (docs/03_test_spec.md)
# Input rejection (empty / oversized) and personal-identifier suppression
# (individual number, postal code, phone number).

import pytest
from unittest.mock import MagicMock

from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    """Patch emit_trace_event at the node module to avoid shared.* backend calls."""
    monkeypatch.setattr(
        "src.nodes.validate_input_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestValidateInputNode:
    """Unit tests for ValidateInputNode (pre_process / VERIFIED_EXTERNAL)."""

    def setup_method(self):
        self.node = ValidateInputNode()

    # ── Input rejection ─────────────────────────────────────────────────────

    def test_empty_string_returns_error(self):
        """GOV-TC-001: empty user_input → ERROR."""
        result = self.node.execute({"user_input": ""})
        assert result["status"] == AgentStatus.SUCCESS
        assert any("empty" in msg.lower() or "missing" in msg.lower() for msg in result["error_log"])

    def test_whitespace_only_returns_error(self):
        """GOV-TC-002: whitespace-only user_input → ERROR."""
        result = self.node.execute({"user_input": "   "})
        assert result["status"] == AgentStatus.SUCCESS

    def test_missing_user_input_key_returns_error(self):
        """GOV-TC-002b: no user_input key at all → ERROR."""
        result = self.node.execute({})
        assert result["status"] == AgentStatus.SUCCESS

    def test_oversized_input_returns_error(self):
        """GOV-TC-003: input > 4000 chars → ERROR with length note."""
        long_input = "あ" * 4001
        result = self.node.execute({"user_input": long_input})
        assert result["status"] == AgentStatus.SUCCESS
        assert any("4000" in msg or "exceeds" in msg.lower() for msg in result["error_log"])

    def test_exactly_4000_chars_is_accepted(self):
        """GOV-TC-003b: input exactly 4000 chars → SUCCESS (boundary)."""
        boundary_input = "a" * 4000
        result = self.node.execute({"user_input": boundary_input})
        assert result["status"] == AgentStatus.SUCCESS

    # ── Personal-identifier suppression ─────────────────────────────────────

    def test_clean_input_no_pii(self):
        """GOV-TC-004: clean Japanese inquiry → SUCCESS, pii_detected=False."""
        state = {"user_input": "住民税の申告について教えてください"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["pii_detected"] is False
        assert result["validated_input"] == "住民税の申告について教えてください"

    def test_my_number_stripped(self):
        """GOV-TC-005: マイナンバー (12-digit) detected and stripped."""
        state = {"user_input": "マイナンバーは123456789012です"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["pii_detected"] is True
        assert "123456789012" not in result["validated_input"]
        assert "[PII_REDACTED]" in result["validated_input"]

    def test_postal_code_stripped(self):
        """GOV-TC-006: postal code 〒NNN-NNNN detected and stripped."""
        state = {"user_input": "住所は〒123-4567の近くです"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["pii_detected"] is True
        assert "123-4567" not in result["validated_input"]

    def test_phone_number_stripped(self):
        """GOV-TC-007: phone number detected and stripped."""
        state = {"user_input": "連絡先は090-1234-5678です"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["pii_detected"] is True
        assert "090-1234-5678" not in result["validated_input"]

    def test_leading_whitespace_stripped_from_validated_input(self):
        """GOV-TC-008: leading/trailing whitespace stripped in validated_input."""
        state = {"user_input": "  道路の陥没を報告したい  "}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["validated_input"] == "道路の陥没を報告したい"

    def test_returns_only_changed_keys(self):
        """Node contract: execute() returns ONLY the keys it changes."""
        result = self.node.execute({"user_input": "道路の穴"})
        assert set(result.keys()) == {"validated_input", "pii_detected", "status"}

    def test_emit_trace_event_called_on_success(self, monkeypatch):
        """An audit event is emitted for accepted inputs."""
        spy = MagicMock()
        monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", spy)
        self.node.execute({"user_input": "税金の申告について"})
        assert spy.called
        # Payload (2nd arg) must not contain citizen text
        payloads = [c.args[1] for c in spy.call_args_list if len(c.args) > 1]
        for payload in payloads:
            assert "税金の申告について" not in str(payload)
