"""AgentCore Platform v1.0 — GOV-C2-003 Citizen Inquiry Classification & Routing Agent"""

# Cat 2 — nested outer AgentBaseGraph pattern.
#
# Outer backbone (this file):
#   START → initialize → pre_process → main → {route} → post_process → finalize → END
#   pre_process  = ValidateInputNode          (input trust gate + caller-data contract)
#   main         = GovInquiryWorkflowGraphNode (GraphNode wrapping the inner BaseGraph)
#   post_process = SecurityGateOutputNode      (output gate + audit record)
#
# Inner domain workflow (src/graph/domain_workflow_graph.py):
#   START → classify_intent → assign_urgency → route_to_department → END
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph
#   src/graph/context_bridge.py        ← caller-settings hand-off outer → inner
#
# See src/examples/graph_cat2_sample.py for the canonical GraphNode pattern.
# Do NOT override add_edges() on the outer graph — backbone wiring is framework-owned.

from pathlib import Path
from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_settings
from src.nodes.security_gate_output_node import (
    ERROR_REASONS,
    SecurityGateOutputNode,
    _REASON_WORKFLOW_FAILED,
    error_envelope,
)
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import State, from_json

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repository root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    config/config.yaml (separate from the static manifest config/agent.yaml)
    declares max_retry / timeout_s at the root plus the classification, urgency
    and routing blocks the inner pipeline consumes. Returns an empty dict —
    never raises — when the file is absent, unreadable, not valid YAML or not a
    mapping; the inner nodes then fall back to their declared defaults. PyYAML
    is imported lazily: it is a framework runtime dependency, so importing it
    on demand avoids a hard module-load coupling.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


class GovInquiryWorkflowGraphNode(GraphNode):
    """GraphNode wrapping the inner citizen-inquiry domain workflow.

    Assigned to the `main` slot of the outer AgentBaseGraph. Delegates to
    GovInquiryDomainWorkflowGraph via get_subgraph().

    Boundary contract:
      _parent_config() — forwards the declared runtime settings to the inner graph
      extract_input()  — passes validated_input to the inner graph's user_input,
                         and bridges the validated caller settings across the
                         boundary (the framework does not forward input_context)
      merge_output()   — maps inner domain results back to the outer state

    The inner graph starts with a FRESH state: only user_input and whatever
    _extra_initial_state() seeds. Fields written by ValidateInputNode
    (pii_detected, channel) stay in the OUTER state, where
    SecurityGateOutputNode reads them after merge_output() completes.
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail-fast).
    error_strategy: ClassVar[str] = "propagate"

    # False: interrupts for human review are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> dict[str, Any]:
        """Forward the declared runtime settings to the inner graph.

        Reads config/config.yaml (see _runtime_config) and exposes the declared
        settings under the ``configurable`` key, which is where BaseGraph
        subclasses and their nodes look for tuning values. Without this the
        inner graph would be constructed with no config at all and every
        setting declared in the file would be dead text — including the
        department table this agent is documented to take per deployment.

        Only keys the file actually declares are forwarded; absent keys fall
        back to each node's built-in default.
        """
        cfg = _runtime_config()
        classification = cfg.get("classification")
        urgency = cfg.get("urgency")
        routing = cfg.get("routing")
        declared: dict[str, Any] = {
            "min_confidence": (classification or {}).get("min_confidence")
            if isinstance(classification, dict)
            else None,
            "high_sla_factor": (urgency or {}).get("high_sla_factor") if isinstance(urgency, dict) else None,
            "medium_sla_factor": (urgency or {}).get("medium_sla_factor") if isinstance(urgency, dict) else None,
            "routing_table": (routing or {}).get("table") if isinstance(routing, dict) else None,
        }
        return {"configurable": {k: v for k, v in declared.items() if v is not None}}

    def get_subgraph(self) -> Any:
        """Instantiate the inner domain workflow graph.

        The import is lazy to avoid circular-import issues; the inner graph is
        instantiated fresh per execute() call (no shared state). The
        manifest-derived config from _parent_config() is passed into the
        BaseGraph constructor; the inner graph republishes the parts its nodes
        need into inner state via _extra_initial_state().
        """
        from src.graph.domain_workflow_graph import GovInquiryDomainWorkflowGraph

        return GovInquiryDomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Pass the sanitized inquiry text into the inner graph.

        Prefers validated_input (written by ValidateInputNode) over the raw
        user_input, so the inner pipeline never sees unstripped text.

        Also bridges the VALIDATED caller settings to the inner graph:
        GraphNode.execute() does not forward input_context on
        subgraph.invoke(), and extract_input is the last template hook that
        sees the outer state before the inner invoke — see
        src/graph/context_bridge.py.
        """
        set_caller_settings(
            {
                "routing_table": from_json(state.get("caller_routing_table_json"), default=None),
                "tuning": from_json(state.get("caller_tuning_json"), default=None) or {},
            }
        )
        result = state.get("validated_input", state.get("user_input", ""))
        return str(result) if result is not None else ""

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph output fields back to the outer state.

        sub_result = GovInquiryDomainWorkflowGraph.get_output(). Returns ONLY
        the keys that changed — never the full state. Designed together with
        that method; the field names must match.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "intent_category": sub_result.get("intent_category"),
            "intent_confidence": sub_result.get("intent_confidence"),
            "urgency_level": sub_result.get("urgency_level"),
            "department_assignment": sub_result.get("department_assignment"),
            "routing_metadata": sub_result.get("routing_metadata"),
            "status": sub_result.get("status"),
        }


class Graph(AgentBaseGraph):
    """GOV-C2-003 — Citizen Inquiry Classification & Routing Agent.

    Cat 2 nested pattern:
      pre_process  = ValidateInputNode           (VERIFIED_EXTERNAL)
      main         = GovInquiryWorkflowGraphNode (wraps the inner domain workflow)
      post_process = SecurityGateOutputNode      (ANONYMOUS)

    Domain logic (classify_intent → assign_urgency → route_to_department) lives
    entirely inside the inner BaseGraph. add_edges() is NOT overridden here —
    backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        return "CitizenInquiryClassificationRoutingAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects InitializeNode + FinalizeNode (backbone defaults).
        super().register_nodes()

        self._nodes["pre_process"] = ValidateInputNode()
        self._nodes["main"] = GovInquiryWorkflowGraphNode()
        self._nodes["post_process"] = SecurityGateOutputNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the structured routing decision on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope,
        which would drop the routing decision the inner graph produced — a
        caller would get the one-line summary string and nothing machine-
        readable. This override adds the structured fields.

        The output-gate invariant is preserved (fail closed):
          * ``output`` stays the POST-gate ``formatted_output`` produced by
            SecurityGateOutputNode — never a pre-gate value, so the gate
            cannot be bypassed through this envelope;
          * the structured fields are surfaced ONLY when the gate passed
            (status == SUCCESS). On any other outcome they are withheld (None).

        On any non-success status the caller receives closed-set labels only:
        ``output`` is None (the base envelope's ``formatted_output or result``
        fallback is not consulted) and ``error`` is ``error_envelope()`` — one
        constant reason code. ``output_withheld`` is the gate's own reason,
        recorded in state as ``error_reason`` when it refused; any other value
        in that slot is not trusted, and every other non-success outcome — a
        caller-data rejection, a trust denial, an inner-workflow error, all of
        which the backbone routes straight to finalize — is ``workflow_failed``.
        ``error_log`` is never projected: it is the internal channel (state
        reducer, audit trail) and carries node- and framework-authored text,
        including a wrapped exception's message and traceback.
        """
        output = dict(super().get_output(state))
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return output
        succeeded = state.get("status") == AgentStatus.SUCCESS.value

        output["intent_category"] = state.get("intent_category") if succeeded else None
        output["intent_confidence"] = state.get("intent_confidence") if succeeded else None
        output["urgency_level"] = state.get("urgency_level") if succeeded else None
        output["department_assignment"] = state.get("department_assignment") if succeeded else None
        output["routing_metadata"] = state.get("routing_metadata") if succeeded else None
        output["pii_detected"] = bool(state.get("pii_detected", False))
        if not succeeded:
            recorded = state.get("error_reason")
            reason = recorded if isinstance(recorded, str) and recorded in ERROR_REASONS else _REASON_WORKFLOW_FAILED
            output["output"] = None
            output["error"] = error_envelope(reason)
        return output


# Registry alias: config/agent.yaml declares
# class: src.graph.graph.CitizenInquiryClassificationRoutingAgent, which must
# resolve to the AgentBaseGraph subclass defined in this file.
CitizenInquiryClassificationRoutingAgent = Graph
