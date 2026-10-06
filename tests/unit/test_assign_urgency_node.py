# GOV-C2-003 — Unit Tests: AssignUrgencyNode
#
# Covers: GOV-TC-020 through GOV-TC-028 (docs/03_test_spec.md)
# Inner domain node: rules-based urgency (high/medium/low)

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.assign_urgency_node import AssignUrgencyNode


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    """Patch emit_trace_event at the node module to avoid shared.* backend calls."""
    monkeypatch.setattr(
        "src.nodes.assign_urgency_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestAssignUrgencyNode:
    """Unit tests for AssignUrgencyNode (inner domain node, INTERNAL)."""

    def setup_method(self):
        self.node = AssignUrgencyNode()

    # ── Category-based rules ────────────────────────────────────────────────

    def test_public_safety_is_always_high(self):
        """GOV-TC-020: public_safety category → urgency=high."""
        state = {"intent_category": "public_safety", "user_input": "事件発生"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "high"

    def test_complaint_is_medium(self):
        """GOV-TC-022: complaint category, no urgency keywords → medium."""
        state = {"intent_category": "complaint", "user_input": "クレームがあります"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "medium"

    def test_infrastructure_is_medium(self):
        """GOV-TC-023: infrastructure category, no urgency keywords → medium."""
        state = {"intent_category": "infrastructure", "user_input": "道路の問題"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "medium"

    def test_social_welfare_is_medium(self):
        """GOV-TC-024: social_welfare category, no urgency keywords → medium."""
        state = {"intent_category": "social_welfare", "user_input": "介護について"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "medium"

    def test_permit_inquiry_is_low(self):
        """GOV-TC-025: permit_inquiry, no urgency keywords → low."""
        state = {"intent_category": "permit_inquiry", "user_input": "建築申請について"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "low"

    def test_tax_question_is_low(self):
        """GOV-TC-026: tax_question, no urgency keywords → low."""
        state = {"intent_category": "tax_question", "user_input": "確定申告について"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "low"

    def test_other_is_low(self):
        """GOV-TC-027: other category → low."""
        state = {"intent_category": "other", "user_input": "その他のお問い合わせ"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "low"

    # ── Keyword override rules ───────────────────────────────────────────────

    def test_high_urgency_keyword_overrides_low_category(self):
        """GOV-TC-021: 至急 keyword in permit_inquiry → overridden to high."""
        state = {"intent_category": "permit_inquiry", "user_input": "至急手続きしたい"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "high"

    def test_medium_urgency_keyword_overrides_low_category(self):
        """GOV-TC-028: 早急 keyword in tax_question → overridden to medium."""
        state = {"intent_category": "tax_question", "user_input": "早急に対応してください"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["urgency_level"] == "medium"

    def test_high_keywords_緊急(self):
        """緊急 keyword → high regardless of category."""
        state = {"intent_category": "other", "user_input": "緊急です"}
        result = self.node.execute(state)
        assert result["urgency_level"] == "high"

    def test_high_keywords_危険(self):
        """危険 keyword → high."""
        state = {"intent_category": "other", "user_input": "危険な状況です"}
        result = self.node.execute(state)
        assert result["urgency_level"] == "high"

    def test_returns_only_changed_keys(self):
        """Node contract: execute() returns only urgency_level, status."""
        result = self.node.execute({"intent_category": "other", "user_input": "test"})
        assert set(result.keys()) == {"urgency_level", "status"}
