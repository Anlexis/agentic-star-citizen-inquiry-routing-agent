"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the validated caller settings across the
# outer→inner graph boundary.
#
# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# the outer state's input_context, so an inner node reading
# state["input_context"] always saw {} through the full nested graph. The
# sanctioned subclass hooks bridge it:
#
#   GovInquiryWorkflowGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       → set_caller_settings({...})
#   GovInquiryDomainWorkflowGraph._extra_initial_state()  [runs INSIDE subgraph.invoke]
#       → get_caller_settings()
#
# Only settings that ValidateInputNode has already VALIDATED cross this bridge —
# the raw caller payload never reaches an inner node, so an inner node cannot
# consume a caller value that nothing bounded.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's settings.

from contextvars import ContextVar
from typing import Any

_CALLER_SETTINGS: ContextVar[dict[str, Any] | None] = ContextVar("gov_c2_003_caller_settings", default=None)


def set_caller_settings(settings: dict[str, Any] | None) -> None:
    """Stash the validated caller settings for the imminent inner-graph invoke."""
    _CALLER_SETTINGS.set(dict(settings) if settings else {})


def get_caller_settings() -> dict[str, Any]:
    """Read (without consuming) the stashed caller settings; {} when none was set."""
    return _CALLER_SETTINGS.get() or {}
