"""AgentCore Platform v1.0 — GOV-C2-003 Inner Domain Workflow Graph"""

# Inner BaseGraph for GOV-C2-003.
# Instantiated by GovInquiryWorkflowGraphNode.get_subgraph() in graph.py.
#
# Pipeline (linear):
#   START → classify_intent → assign_urgency → route_to_department → END
#
# This graph receives user_input = the sanitized inquiry text (from
# extract_input()). The inner state is FRESH — outer-state fields such as
# pii_detected are NOT available here; SecurityGateOutputNode (the outer
# post_process slot) reads those from the outer state after merge_output().
#
# All 7 BaseGraph abstract methods are implemented below.
# register_nodes() does NOT call super() — BaseGraph.register_nodes() is abstract.
# Domain nodes are instantiated with NO constructor args: the node contract is
# execute(self, state) -> dict and BaseNode.__call__ passes exactly one
# argument, so runtime settings reach them through _extra_initial_state() and
# State, never through execute().

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_settings
from src.nodes.assign_urgency_node import AssignUrgencyNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.route_to_department_node import RouteToDepartmentNode
from src.schemas.state import State, to_json


class GovInquiryDomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for GOV-C2-003.

    Inherits BaseGraph (fully custom node topology — no pre/main/post backbone).

    Pipeline:
        START → classify_intent → assign_urgency → route_to_department → END

    Output: get_output() returns the domain fields consumed by
    GovInquiryWorkflowGraphNode.merge_output() in graph.py.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "GovInquiryDomainWorkflowGraph"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory config for the inner domain graph.

        Every forwarded setting is optional — each node falls back to its
        built-in default — so validation is permissive here rather than
        raising ConfigError.
        """
        pass

    # ── Settings forwarding into inner state ──────────────────────────────────

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the effective runtime settings.

        Two sources are merged, caller last:
          1. the deployment settings declared in config/config.yaml, forwarded
             by GovInquiryWorkflowGraphNode._parent_config() under
             config["configurable"];
          2. the VALIDATED caller overrides carried across the outer→inner
             boundary by src/graph/context_bridge.py (GraphNode.execute() does
             not forward input_context on subgraph.invoke(), and the raw caller
             payload is deliberately not bridged — only values ValidateInputNode
             has already bounded).

        Dict-valued settings are stored as JSON strings, never as bare dicts:
        checkpointed State fields are msgpack-serialised.

        A key absent from both sources is left out of the returned dict, so the
        consuming node keeps its built-in default.
        """
        configurable = (self.config or {}).get("configurable") or {}
        caller = get_caller_settings()
        caller_tuning = caller.get("tuning") or {}

        seeded: dict[str, Any] = {}

        routing_table = caller.get("routing_table") or configurable.get("routing_table")
        if isinstance(routing_table, dict) and routing_table:
            seeded["routing_table_json"] = to_json(routing_table)

        min_confidence = caller_tuning.get("min_confidence", configurable.get("min_confidence"))
        if isinstance(min_confidence, (int, float)) and not isinstance(min_confidence, bool):
            seeded["min_confidence"] = float(min_confidence)

        factors = {
            key: caller_tuning.get(key, configurable.get(key)) for key in ("high_sla_factor", "medium_sla_factor")
        }
        factors = {k: float(v) for k, v in factors.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if factors:
            seeded["urgency_factors_json"] = to_json(factors)

        return seeded

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register the domain nodes with NO constructor args.

        FunctionNode subclasses take no __init__ parameters; settings reach the
        nodes through _extra_initial_state() -> State. Do NOT call super()
        (BaseGraph.register_nodes() is abstract), and do NOT register
        initialize / finalize — those are outer backbone concerns.
        """
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["assign_urgency"] = AssignUrgencyNode()
        self._nodes["route_to_department"] = RouteToDepartmentNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear classification → urgency → routing pipeline."""
        self._sg.add_edge(START, "classify_intent")
        self._sg.add_edge("classify_intent", "assign_urgency")
        self._sg.add_edge("assign_urgency", "route_to_department")
        self._sg.add_edge("route_to_department", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by the BaseGraph contract.

        This graph uses a purely linear topology, so the method is never called
        in normal operation. Retained to satisfy the abstract method.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "route_to_department"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the sub_result handed to GovInquiryWorkflowGraphNode.merge_output().

        Designed together with merge_output() in graph.py — the field names
        must match. Returns only the domain fields the outer merge needs.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "intent_category": state.get("intent_category"),
            "intent_confidence": state.get("intent_confidence"),
            "urgency_level": state.get("urgency_level"),
            "department_assignment": state.get("department_assignment"),
            "routing_metadata": state.get("routing_metadata"),
            "status": state.get("status"),
        }
