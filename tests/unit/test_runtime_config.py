# GOV-C2-003 — Unit Tests: runtime-config plumbing (config/config.yaml)
#
# The manifest split leaves the runtime parameters in config/config.yaml. These
# tests prove the declared values are LIVE — read by _runtime_config(),
# forwarded by GovInquiryWorkflowGraphNode._parent_config(), seeded into the
# inner state, and actually deciding outcomes through a full compiled invoke —
# rather than dead configuration text.
#
# The last property is the one worth guarding: a config reader that silently
# returns {} leaves every node on its hard-coded default, all happy-path tests
# stay green, and the declared settings quietly do nothing. The probes below
# flip an observable outcome via the config file so that regression cannot hide.

import pytest

from framework.schemas.agent_status import AgentStatus

_NODE_MODULES = (
    "validate_input_node",
    "classify_intent_node",
    "assign_urgency_node",
    "route_to_department_node",
    "security_gate_output_node",
)

# One keyword hit (道路) → confidence 0.65: above the shipped 0.6 bar, below a
# probe bar of 0.9, so the configured value decides the routed department.
_ONE_HIT_INQUIRY = "道路に穴が開いている"


def _patch_emit(monkeypatch):
    for mod in _NODE_MODULES:
        monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)


def _invoke(inquiry, input_context=None):
    from framework.schemas.invocation_context import InvocationContext, TrustLevel
    from src.graph.graph import Graph

    agent = Graph()
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(inquiry, ctx=ctx, input_context=input_context or {})


class TestRuntimeConfigFile:
    def test_shipped_config_parses_and_declares_expected_keys(self):
        from src.graph.graph import _runtime_config

        cfg = _runtime_config()
        assert cfg.get("max_retry") == 3
        assert cfg.get("timeout_s") == 30
        assert cfg["classification"]["min_confidence"] == 0.6
        assert cfg["urgency"]["high_sla_factor"] == 0.5
        assert cfg["urgency"]["medium_sla_factor"] == 1.0

    def test_parent_config_forwards_declared_settings(self):
        from src.graph.graph import GovInquiryWorkflowGraphNode

        configurable = GovInquiryWorkflowGraphNode()._parent_config()["configurable"]
        assert configurable["min_confidence"] == 0.6
        assert configurable["high_sla_factor"] == 0.5
        assert configurable["medium_sla_factor"] == 1.0

    def test_missing_or_malformed_file_degrades_to_empty(self, tmp_path, monkeypatch):
        import src.graph.graph as graph_mod

        monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
        assert graph_mod._runtime_config() == {}

        bad = tmp_path / "bad.yaml"
        bad.write_text("not: [valid yaml", encoding="utf-8")
        monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", bad)
        assert graph_mod._runtime_config() == {}


class TestConfigReachesTheInnerGraphEndToEnd:
    """The declared settings must decide real outcomes through the full graph."""

    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        _patch_emit(monkeypatch)

    def _point_at(self, monkeypatch, tmp_path, body):
        import src.graph.graph as graph_mod

        probe = tmp_path / "config.yaml"
        probe.write_text(body, encoding="utf-8")
        monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", probe)

    def test_shipped_bar_keeps_a_single_hit_classification(self):
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent_category"] == "infrastructure"
        assert result["department_assignment"] == "public_works_dept"

    def test_tightened_confidence_bar_flips_the_routed_department(self, tmp_path, monkeypatch):
        # Same inquiry, same pipeline — only config/config.yaml differs. The
        # tightened bar must file it as "other", proving the value travels
        # config file → _parent_config → inner state → ClassifyIntentNode
        # through the full nested invoke.
        self._point_at(monkeypatch, tmp_path, "classification:\n  min_confidence: 0.9\n")
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent_category"] == "other"
        assert result["department_assignment"] == "general_inquiry_dept"

    def test_configured_sla_factor_changes_the_deadline(self, tmp_path, monkeypatch):
        # public_safety carries a 2-hour standard deadline; the shipped high
        # factor halves it to 1. A medium factor of 0.5 must halve an
        # infrastructure deadline (24h → 12h) the same way.
        self._point_at(monkeypatch, tmp_path, "urgency:\n  medium_sla_factor: 0.5\n")
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["routing_metadata"]["urgency_level"] == "medium"
        assert result["routing_metadata"]["sla_hours"] == 12
        assert "SLA: 12h" in result["output"]

    def test_configured_department_table_replaces_the_built_in_directory(self, tmp_path, monkeypatch):
        self._point_at(
            monkeypatch,
            tmp_path,
            "routing:\n"
            "  table:\n"
            "    infrastructure:\n"
            "      department: doboku_ka\n"
            "      sla_hours: 6\n"
            "      escalation_path: null\n"
            "      contact_key: doboku_contact\n",
        )
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["department_assignment"] == "doboku_ka"
        assert result["routing_metadata"]["sla_hours"] == 6
        assert "doboku_ka" in result["output"]

    def test_partial_config_entry_degrades_to_the_catch_all_values(self, tmp_path, monkeypatch):
        """An operator-written table need not be complete — missing fields fall
        back to the catch-all entry rather than failing the invocation."""
        self._point_at(
            monkeypatch,
            tmp_path,
            "routing:\n  table:\n    infrastructure:\n      department: doboku_ka\n",
        )
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["department_assignment"] == "doboku_ka"
        assert result["routing_metadata"]["sla_hours"] == 72
        assert result["routing_metadata"]["contact_key"] == "general_inquiry_contact"

    def test_absent_config_still_serves_with_node_defaults(self, tmp_path, monkeypatch):
        import src.graph.graph as graph_mod

        monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
        result = _invoke(_ONE_HIT_INQUIRY)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["department_assignment"] == "public_works_dept"
        assert result["routing_metadata"]["sla_hours"] == 24


class TestCallerOverridesBeatConfig:
    """A validated caller override takes precedence over the declared value."""

    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        _patch_emit(monkeypatch)

    def test_caller_confidence_bar_overrides_the_configured_one(self):
        result = _invoke(_ONE_HIT_INQUIRY, {"classification": {"min_confidence": 0.9}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent_category"] == "other"
        assert result["department_assignment"] == "general_inquiry_dept"

    def test_caller_table_overrides_the_built_in_directory(self):
        result = _invoke(
            _ONE_HIT_INQUIRY,
            {
                "routing_table": {
                    "infrastructure": {
                        "department": "doboku_ka",
                        "sla_hours": 6,
                        "escalation_path": None,
                        "contact_key": "doboku_contact",
                    }
                }
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["department_assignment"] == "doboku_ka"
        assert result["routing_metadata"]["sla_hours"] == 6
