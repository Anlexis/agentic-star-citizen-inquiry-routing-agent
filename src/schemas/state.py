"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# Serialisation contract: every dict/list-valued field is stored as a JSON
# string (Optional[str]) and passed through to_json() / from_json() at the
# producing and consuming node.  Never type a dict/list field as a bare
# dict/list — msgpack cannot round-trip it.

import json
import math
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled number: a FINITE value within [lo, hi], else None.

    Rejects bools, strings, other non-numerics and — critically — non-finite
    values. ``float("nan")`` parses fine and Python's json module accepts bare
    ``NaN`` / ``Infinity`` in a request body, while every IEEE comparison
    against NaN is False: a NaN threshold would silently disable the check it
    guards instead of failing. Every caller-supplied number goes through here
    and fails CLOSED on violation.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def finite_int_in_range(value: Any, lo: int, hi: int) -> Optional[int]:
    """Parse a caller-controlled integer within [lo, hi], else None.

    Same fail-closed contract as finite_in_range, restricted to whole numbers:
    bools, floats, strings and out-of-range magnitudes are all rejected.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not lo <= value <= hi:
        return None
    return int(value)


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an unset field stays distinguishable from
    an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input yields ``default`` so a missing or corrupt
    field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """GOV-C2-003 agent state — Citizen Inquiry Classification & Routing.

    All shared fields (user_input, input_context, status, session_id,
    node_history, error_log, result, response_metadata, trace_id,
    correlation_id, schema_version, hitl_*) are inherited from AgentState.

    Field ownership per node:
      ValidateInputNode (outer pre_process):
        validated_input, pii_detected, channel,
        caller_routing_table_json, caller_tuning_json
      ClassifyIntentNode (inner: classify_intent):
        intent_category, intent_confidence
      AssignUrgencyNode (inner: assign_urgency):
        urgency_level
      RouteToDepartmentNode (inner: route_to_department):
        department_assignment, routing_metadata
      SecurityGateOutputNode (outer post_process):
        formatted_output, result, audit_entry, error_reason
    """

    # --- ValidateInputNode outputs ---
    # Sanitized inquiry text (personal identifiers stripped); set by pre_process
    validated_input: Optional[str]

    # True when personal identifiers (individual number, postal code, phone)
    # were detected and stripped. The values themselves are NEVER stored —
    # only this boolean flag.
    pii_detected: Optional[bool]

    # Caller channel label, locked to an inert identifier. Audit metadata only —
    # it is never rendered into the caller-facing output.
    channel: Optional[str]

    # JSON string of the VALIDATED caller-supplied department table, present
    # only when the caller supplied one and every entry cleared its bounds.
    caller_routing_table_json: Optional[str]

    # JSON string of the VALIDATED caller tuning overrides
    # ({min_confidence, high_sla_factor, medium_sla_factor} — any subset).
    caller_tuning_json: Optional[str]

    # --- ClassifyIntentNode outputs (inner graph) ---
    # One of: permit_inquiry | tax_question | social_welfare |
    #         public_safety | infrastructure | complaint | other
    intent_category: Optional[str]

    # Classification confidence, 0.0–1.0
    intent_confidence: Optional[float]

    # --- AssignUrgencyNode output (inner graph) ---
    # One of: high | medium | low
    urgency_level: Optional[str]

    # --- RouteToDepartmentNode outputs (inner graph) ---
    # Target department key, e.g. "urban_planning_dept"
    department_assignment: Optional[str]

    # Routing metadata: {department, sla_hours, escalation_path, contact_key,
    # urgency_level, intent_category}. JSON-serializable scalars only.
    routing_metadata: Optional[dict[str, Any]]

    # --- Runtime settings seeded into the inner graph ---
    # The three fields below are written by
    # GovInquiryDomainWorkflowGraph._extra_initial_state(), which merges the
    # values declared in config/config.yaml with the validated caller overrides
    # carried across the outer→inner boundary (src/graph/context_bridge.py).
    # They travel through State rather than an execute() argument because
    # BaseNode.__call__ invokes execute(state) with a single argument.
    #
    # JSON string of the effective department table; absent means "no table
    # declared anywhere", and RouteToDepartmentNode uses its built-in default.
    routing_table_json: Optional[str]

    # Effective minimum classification confidence. A best category scoring
    # below this bar is filed as "other" instead of guessed into a department.
    min_confidence: Optional[float]

    # JSON string of the effective service-level multipliers
    # ({high_sla_factor, medium_sla_factor}) applied to the routed department's
    # sla_hours.
    urgency_factors_json: Optional[str]

    # --- SecurityGateOutputNode outputs (outer post_process) ---
    # Final caller-facing output — the routing decision only; no inquiry text
    formatted_output: Optional[str]

    # Audit record written to the persistent store; inquiry text NOT included
    audit_entry: Optional[dict[str, Any]]

    # Closed-set reason recorded when the output boundary refuses a response
    # ("output_withheld"). Graph.get_output() projects it — and only it — to
    # the caller as error.reason; error_log is never projected. Never free text.
    error_reason: Optional[str]
    error_code: Optional[str]
