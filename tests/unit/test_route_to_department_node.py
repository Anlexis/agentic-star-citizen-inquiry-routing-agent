# GOV-C2-003 — Unit Tests: RouteToDepartmentNode
#
# Covers: GOV-TC-030 through GOV-TC-040 (docs/03_test_spec.md)
# Inner domain node: category × urgency → department + routing_metadata

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.route_to_department_node import RouteToDepartmentNode
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    """Patch emit_trace_event at the node module to avoid shared.* backend calls."""
    monkeypatch.setattr(
        "src.nodes.route_to_department_node.emit_trace_event",
        lambda *a, **k: None,
    )


class TestRouteToDepartmentNode:
    """Unit tests for RouteToDepartmentNode (inner domain node, INTERNAL)."""

    def setup_method(self):
        self.node = RouteToDepartmentNode()

    # ── Default routing table ────────────────────────────────────────────────

    def test_permit_inquiry_low(self):
        """GOV-TC-030: permit_inquiry + low → urban_planning_dept, sla=48h."""
        state = {"intent_category": "permit_inquiry", "urgency_level": "low"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "urban_planning_dept"
        assert result["routing_metadata"]["sla_hours"] == 48

    def test_tax_question_medium(self):
        """GOV-TC-031: tax_question + medium → taxation_dept, sla=24h."""
        state = {"intent_category": "tax_question", "urgency_level": "medium"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "taxation_dept"

    def test_social_welfare_medium(self):
        """GOV-TC-032: social_welfare + medium → welfare_dept, sla=12h."""
        state = {"intent_category": "social_welfare", "urgency_level": "medium"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "welfare_dept"
        assert result["routing_metadata"]["sla_hours"] == 12

    def test_public_safety_high_halves_sla(self):
        """GOV-TC-033: public_safety + high → sla halved (2h → 1h)."""
        state = {"intent_category": "public_safety", "urgency_level": "high"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "public_safety_dept"
        assert result["routing_metadata"]["sla_hours"] == 1  # 2 // 2

    def test_infrastructure_low(self):
        """GOV-TC-034: infrastructure + low → public_works_dept."""
        state = {"intent_category": "infrastructure", "urgency_level": "low"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "public_works_dept"

    def test_complaint_low(self):
        """GOV-TC-035: complaint + low → citizen_relations_dept, sla=8h."""
        state = {"intent_category": "complaint", "urgency_level": "low"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "citizen_relations_dept"
        assert result["routing_metadata"]["sla_hours"] == 8

    def test_other_low(self):
        """GOV-TC-036: other + low → general_inquiry_dept, sla=72h."""
        state = {"intent_category": "other", "urgency_level": "low"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "general_inquiry_dept"
        assert result["routing_metadata"]["sla_hours"] == 72

    def test_high_urgency_sets_escalation(self):
        """GOV-TC-037: permit_inquiry + high → sla halved (48→24), escalation set."""
        state = {"intent_category": "permit_inquiry", "urgency_level": "high"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_metadata"]["sla_hours"] == 24  # 48 // 2
        assert result["routing_metadata"]["escalation_path"] is not None

    def test_unknown_category_falls_back_to_general(self):
        """GOV-TC-038: unknown category → general_inquiry_dept fallback."""
        state = {"intent_category": "unknown_xyz", "urgency_level": "low"}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "general_inquiry_dept"

    def test_custom_routing_table_override(self):
        """GOV-TC-039: a routing_table override replaces the default table.

        The override is delivered through State (routing_table_json), which is
        the only channel a node has: BaseNode.__call__ calls execute(state) with
        a single argument, so the previous per-call config kwarg could never be
        populated at runtime.
        """
        custom_table = {
            "tax_question": {
                "department": "zeimu_ka",
                "sla_hours": 48,
                "escalation_path": None,
                "contact_key": "zeimu_contact",
            },
            "other": {
                "department": "general_inquiry",
                "sla_hours": 72,
                "escalation_path": None,
                "contact_key": "general_contact",
            },
        }
        state = {
            "intent_category": "tax_question",
            "urgency_level": "low",
            "routing_table_json": to_json(custom_table),
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "zeimu_ka"

    def test_malformed_routing_table_falls_back_to_default(self):
        """A corrupt routing_table_json must not break routing — default table applies."""
        state = {
            "intent_category": "tax_question",
            "urgency_level": "low",
            "routing_table_json": "{not-valid-json",
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["department_assignment"] == "taxation_dept"

    def test_routing_metadata_contains_required_fields(self):
        """GOV-TC-040: routing_metadata always contains intent_category and urgency_level."""
        state = {"intent_category": "complaint", "urgency_level": "medium"}
        result = self.node.execute(state)
        meta = result["routing_metadata"]
        assert "urgency_level" in meta
        assert "intent_category" in meta
        assert "sla_hours" in meta
        assert "department" in meta

    def test_routing_metadata_is_json_serializable(self):
        """routing_metadata must be a plain JSON-serializable dict (State msgpack safety)."""
        import json

        state = {"intent_category": "infrastructure", "urgency_level": "high"}
        result = self.node.execute(state)
        # Must not raise — plain dict with primitives
        json.dumps(result["routing_metadata"])

    def test_returns_only_changed_keys(self):
        """Node contract: execute() returns only department_assignment, routing_metadata, status."""
        result = self.node.execute({"intent_category": "other", "urgency_level": "low"})
        assert set(result.keys()) == {"department_assignment", "routing_metadata", "status"}
