"""AgentCore Platform v1.0"""

# Generic pre_process node — the reference implementation the graph samples in
# src/examples/ wire into the backbone's pre_process slot. The agent shipped in
# this repository does NOT use it: its pre_process slot holds ValidateInputNode
# (src/nodes/validate_input_node.py), which adds the caller-data contract and
# the personal-identifier strip this domain needs.
#
# Keep it as the minimal shape of a pre_process node:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Never import from mediator/, api/, or other agents

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PreProcessNode(FunctionNode):
    """Validate and enrich incoming input before main processing."""

    # Declared explicitly by design, never inherited implicitly. Raise it to
    # VERIFIED_EXTERNAL / INTERNAL when the node performs a privileged
    # operation — as the agent's own entry node does.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {}) or {}

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        validated_input = user_input.strip()

        # Audit: record that input passed validation (no caller text in the payload).
        emit_trace_event(
            "pre_process_complete",
            {"input_chars": len(validated_input)},
            state,
        )

        return {
            "validated_input": validated_input,
            "enriched_context": {
                "source": "CitizenInquiryClassificationRoutingAgent",
                "channel": input_context.get("channel", "unknown"),
            },
            "status": AgentStatus.SUCCESS.value,
        }
