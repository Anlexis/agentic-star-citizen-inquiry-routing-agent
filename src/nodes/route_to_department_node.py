"""AgentCore Platform v1.0 — GOV-C2-003: RouteToDepartmentNode"""

# Inner domain graph node — route_to_department slot.
# Maps (intent_category, urgency_level) to a department and its routing
# metadata, using the department directory in force for this invocation.
#
# The directory reaches this node as the State field routing_table_json,
# seeded by GovInquiryDomainWorkflowGraph._extra_initial_state() from
# config/config.yaml (routing.table) or from the caller's validated override.
# When neither declares one, the built-in directory in
# src/services/service.py applies.

import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.service import DepartmentDirectoryService

# Service-level multipliers applied when config declares none. Halving the
# deadline for a high-urgency inquiry is the shipped default; medium and low
# keep the department's standard deadline.
_DEFAULT_SLA_FACTORS: dict[str, float] = {"high_sla_factor": 0.5, "medium_sla_factor": 1.0}

# Deadlines never round below one hour, and a high-urgency inquiry always
# carries an escalation path even when the directory entry declares none.
_MIN_SLA_HOURS = 1
_DEFAULT_HIGH_ESCALATION = "department_head"


def _sla_factors(state: AgentState) -> dict[str, float]:
    """Read the effective service-level multipliers from state.

    Values are seeded already-validated; anything non-numeric that reaches here
    is ignored in favour of the shipped default rather than propagated.
    """
    factors = dict(_DEFAULT_SLA_FACTORS)
    seeded = from_json(state.get("urgency_factors_json"), default=None)
    if isinstance(seeded, dict):
        for key in _DEFAULT_SLA_FACTORS:
            value = seeded.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                factors[key] = float(value)
    return factors


def _apply_urgency(entry: dict[str, Any], urgency_level: str, factors: dict[str, float]) -> dict[str, Any]:
    """Apply the urgency adjustments to a directory entry. Returns a new dict."""
    result = dict(entry)
    factor = {"high": factors["high_sla_factor"], "medium": factors["medium_sla_factor"]}.get(urgency_level)
    if factor is not None:
        result["sla_hours"] = max(_MIN_SLA_HOURS, int(entry["sla_hours"] * factor))
    if urgency_level == "high" and not result.get("escalation_path"):
        result["escalation_path"] = _DEFAULT_HIGH_ESCALATION
    return result


class RouteToDepartmentNode(FunctionNode):
    """Route a classified inquiry to the responsible department.

    Inner domain graph node (route_to_department slot). Reads intent_category
    from ClassifyIntentNode and urgency_level from AssignUrgencyNode, looks the
    category up in the department directory, and applies the urgency
    adjustments: a high-urgency inquiry gets half the deadline (never below one
    hour) and always carries an escalation path.

    Returns:
        department_assignment: the target department key
        routing_metadata: {department, sla_hours, escalation_path, contact_key,
                           urgency_level, intent_category}
    """

    # Inner graph node — explicitly ANONYMOUS. See classify_intent_node.py for
    # the rationale; an explicit declaration in the class body is required.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        intent_category = state.get("intent_category", "other")
        urgency_level = state.get("urgency_level", "low")

        directory = DepartmentDirectoryService(from_json(state.get("routing_table_json"), default=None))
        entry = _apply_urgency(directory.lookup(intent_category), urgency_level, _sla_factors(state))

        department_assignment = entry["department"]
        routing_metadata = {
            "department": entry["department"],
            "sla_hours": entry["sla_hours"],
            "escalation_path": entry["escalation_path"],
            "contact_key": entry["contact_key"],
            "urgency_level": urgency_level,
            "intent_category": intent_category,
        }

        emit_trace_event(
            "gov_c2_003.route_to_department.routed",
            {
                "department_assignment": department_assignment,
                "sla_hours": entry["sla_hours"],
                "urgency_level": urgency_level,
                "intent_category": intent_category,
                "default_directory": directory.is_default_directory,
            },
            state,
        )

        return {
            "department_assignment": department_assignment,
            "routing_metadata": routing_metadata,
            "status": AgentStatus.SUCCESS.value,
        }
