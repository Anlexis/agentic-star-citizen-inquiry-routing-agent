# GOV-C2-003 — Unit Tests: ClassifyIntentNode
#
# Covers: GOV-TC-010 through GOV-TC-018 (docs/03_test_spec.md)
# Inner domain node: 7-category keyword classification

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    """Patch emit_trace_event at the node module to avoid shared.* backend calls."""
    monkeypatch.setattr(
        "src.nodes.classify_intent_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestClassifyIntentNode:
    """Unit tests for ClassifyIntentNode (inner domain node, INTERNAL)."""

    def setup_method(self):
        self.node = ClassifyIntentNode()

    # ── Category classification ─────────────────────────────────────────────

    def test_tax_question_category(self):
        """GOV-TC-010: 住民税の申告 → tax_question, confidence ≥ 0.5."""
        state = {"user_input": "住民税の申告について教えてください"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "tax_question"
        assert result["intent_confidence"] >= 0.5

    def test_permit_inquiry_category(self):
        """GOV-TC-011: 建築許可の申請 → permit_inquiry."""
        state = {"user_input": "建築許可の申請方法を教えてください"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "permit_inquiry"

    def test_social_welfare_category(self):
        """GOV-TC-012: 障害者福祉の相談 → social_welfare.

        Uses an unambiguous welfare-specific input (障害 + 福祉 both map to social_welfare;
        no permit_inquiry keywords). The earlier test input '介護保険の手続きについて' was
        ambiguous — 手続き maps to permit_inquiry AND 介護 maps to social_welfare with
        equal score, making the result order-dependent.
        """
        state = {"user_input": "障害者福祉の相談がしたい"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "social_welfare"

    def test_public_safety_category(self):
        """GOV-TC-013: 緊急事態 → public_safety."""
        state = {"user_input": "緊急事態が発生した"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "public_safety"

    def test_infrastructure_category(self):
        """GOV-TC-014: 道路に穴 → infrastructure."""
        state = {"user_input": "道路に穴が開いている"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "infrastructure"

    def test_complaint_category(self):
        """GOV-TC-015: 苦情 → complaint."""
        state = {"user_input": "サービスへの苦情があります"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "complaint"

    def test_other_fallback(self):
        """GOV-TC-016: no Japanese keywords → other, confidence = 0.5 baseline."""
        state = {"user_input": "hello world how are you"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "other"
        assert result["intent_confidence"] == pytest.approx(0.5)

    def test_empty_input_returns_error(self):
        """GOV-TC-017: empty user_input → ERROR."""
        state = {"user_input": ""}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR

    def test_confidence_increases_with_keyword_hits(self):
        """GOV-TC-010b: multiple keyword hits raise confidence above baseline."""
        state = {"user_input": "税の申告と課税の控除について知りたい"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "tax_question"
        assert result["intent_confidence"] > 0.5  # multiple hits > baseline

    def test_public_safety_takes_priority_on_conflict(self):
        """GOV-TC-018: public_safety rule has highest priority in _CATEGORY_RULES."""
        # 警察 (public_safety) + 税 (tax_question): public_safety is first in rules
        state = {"user_input": "税の件で警察に通報したい"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["intent_category"] == "public_safety"

    def test_confidence_ceiling_at_0_95(self):
        """Confidence is capped at 0.95 regardless of keyword count."""
        # Many tax keywords — still should not exceed ceiling
        state = {"user_input": "税 課税 申告 控除 納税 確定申告 住民税 固定資産税 追加の税"}
        result = self.node.execute(state)
        assert result["intent_confidence"] <= 0.95

    def test_returns_only_changed_keys(self):
        """Node contract: execute() returns only intent_category, intent_confidence, status."""
        result = self.node.execute({"user_input": "道路の問題"})
        assert set(result.keys()) == {"intent_category", "intent_confidence", "status"}
