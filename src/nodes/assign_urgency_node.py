"""AgentCore Platform v1.0 — GOV-C2-003: AssignUrgencyNode"""

# Inner domain graph node — assign_urgency slot.
# Rules-based urgency assignment from intent_category plus keyword signals in
# the inquiry text.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Categories that imply elevated urgency when present
_HIGH_URGENCY_CATEGORIES = frozenset(["public_safety"])
_MEDIUM_URGENCY_CATEGORIES = frozenset(["complaint", "infrastructure", "social_welfare"])

# Keyword signals that override category-based urgency (checked first)
_HIGH_URGENCY_KEYWORDS = ["緊急", "至急", "今すぐ", "危険", "事件", "事故", "助けて"]
_MEDIUM_URGENCY_KEYWORDS = ["早急", "できるだけ早く", "なるべく早く", "急ぎ"]

_URGENCY_HIGH = "high"
_URGENCY_MEDIUM = "medium"
_URGENCY_LOW = "low"


def _assign_urgency(intent_category: str, text: str) -> str:
    """Determine urgency level from category + keyword signals."""
    # Keyword signals take priority over category rules
    if any(kw in text for kw in _HIGH_URGENCY_KEYWORDS):
        return _URGENCY_HIGH
    if any(kw in text for kw in _MEDIUM_URGENCY_KEYWORDS):
        return _URGENCY_MEDIUM

    # Category-based rules
    if intent_category in _HIGH_URGENCY_CATEGORIES:
        return _URGENCY_HIGH
    if intent_category in _MEDIUM_URGENCY_CATEGORIES:
        return _URGENCY_MEDIUM

    return _URGENCY_LOW


class AssignUrgencyNode(FunctionNode):
    """Assign an urgency level (high/medium/low) to a classified inquiry.

    Inner domain graph node (assign_urgency slot). Reads intent_category set by
    ClassifyIntentNode and the sanitized inquiry text (user_input) for keyword
    signals.

    Rules:
    - high: public_safety category OR an urgent keyword (緊急, 至急, 今すぐ, 危険, …)
    - medium: complaint / infrastructure / social_welfare OR a medium keyword
    - low: everything else
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
        text = state.get("user_input", "")

        urgency_level = _assign_urgency(intent_category, text)

        emit_trace_event(
            "gov_c2_003.assign_urgency.assigned",
            {
                "intent_category": intent_category,
                "urgency_level": urgency_level,
            },
            state,
        )

        return {
            "urgency_level": urgency_level,
            "status": AgentStatus.SUCCESS.value,
        }
