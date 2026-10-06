"""AgentCore Platform v1.0"""

# Generic post_process node — the reference implementation the graph samples in
# src/examples/ wire into the backbone's post_process slot. The agent shipped
# in this repository does NOT use it: its post_process slot holds
# SecurityGateOutputNode (src/nodes/security_gate_output_node.py), which
# enforces this domain's output boundary and writes the audit record.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PostProcessNode(FunctionNode):
    """Format and finalize the output."""

    # Declared explicitly by design, never inherited implicitly.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        result = state.get("result", "")

        # Audit: record that output was finalized (no caller text in the payload).
        emit_trace_event(
            "post_process_complete",
            {"output_chars": len(result) if isinstance(result, str) else 0},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
