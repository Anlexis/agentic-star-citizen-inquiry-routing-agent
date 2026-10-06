"""AgentCore Platform v1.0 — GOV-C2-003: SecurityGateOutputNode"""

# Outer backbone post_process slot — the output boundary and the audit record.
#
# The stated invariant of this agent's external surface is narrow and absolute:
#
#   the caller-facing output carries the ROUTING DECISION and nothing else —
#   no inquiry text, no internal state, no free text of any origin.
#
# Three independent layers enforce it, each with its own audit event. They are
# deliberately not one combined check: a value that slips past the grammar is
# still caught verbatim, and a value that is neither is still credential-scanned.
#
#   1. Rendered-token grammar (allow-list, fail CLOSED). Every field rendered
#      into the output must be an inert identifier ([a-z0-9_]{1,48}) and the
#      deadline a whole number of hours in 1..720. Anything else — a fragment
#      of inquiry text, a punctuated string, a float, a non-finite number — is
#      refused and NOTHING is published. An allow-list is used rather than a
#      list of forbidden tokens because the forbidden-token form only catches
#      the representations someone thought of; this one covers every
#      representation by construction.
#   2. Verbatim-embedding scan. The assembled string is checked against the
#      inquiry text held in State (raw and sanitized). Any verbatim embedding
#      is replaced with [REDACTED] and audited — the layer that would catch a
#      leak arriving through a field the grammar happens to accept.
#   3. Credential scan. The assembled string is scanned for credential-like
#      patterns (API keys, JWTs, bearer tokens, secret assignments) and the
#      personal-identifier patterns the input stage strips. A hit fails CLOSED:
#      the status becomes ERROR and nothing is published.
#
# The gate is not switchable — there is no configuration flag that turns it
# off, because a routing agent whose output boundary can be disabled is not the
# same product.
#
# The checks are inline in execute() and in module-level functions — NOT as
# _extra_security_gate_output() instance methods: the framework wraps a method
# of that name, and a wrapped hook returning None is passed on as the next
# node's state.
#
# Refusal contract. When a layer refuses, NOTHING is published: no
# formatted_output, no result, no audit_entry. The refusal is recorded as the
# closed-set reason error_reason="output_withheld" (see _contain) plus one
# error_log line that names the field or the pattern — never the value.
# Graph.get_output() projects the reason to the caller; error_log stays
# internal.

import re
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

# Layer 1 — the grammar every rendered field must satisfy.
_INERT_LABEL_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_SLA_HOURS_MIN, _SLA_HOURS_MAX = 1, 720

# Layer 2 — State fields whose values must never appear verbatim in the output.
# The inquiry text is the whole point: this agent publishes a decision about an
# inquiry, never the inquiry itself.
_BLOCKED_STATE_FIELDS = ("user_input", "validated_input")
# Shorter fragments cannot meaningfully identify inquiry text and would
# over-redact ordinary words.
_MIN_VERBATIM_CHARS = 8

# Layer 3 — patterns that must never reach a caller.
_CREDENTIAL_PATTERNS: list[tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
    # The identifiers the input stage strips must not reappear on the way out.
    (r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", "individual_number_pattern"),
    (r"〒?\d{3}[-\s]?\d{4}", "postal_code_pattern"),
    (r"0\d{1,4}[-\s]\d{2,4}[-\s]\d{4}", "phone_number_pattern"),
]

# ── Caller-visible ERROR reason — a closed set ────────────────────────────────
#
# On every non-success invoke the caller receives ONE constant chosen here and
# nothing else: not an error_log line, not the refused field name, not an
# exception's text. Those stay on the internal channel (error_log — the state
# reducer appends to it, the audit trail reads it). A node- or framework-
# authored line can embed an exception's message and traceback, and truncating
# or redacting such text is not a closed set; not projecting it is.
_REASON_WORKFLOW_FAILED = "workflow_failed"  # a caller-data rejection, trust denial or inner-workflow error
_REASON_OUTPUT_WITHHELD = "output_withheld"  # this boundary refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def error_envelope(reason: str) -> dict[str, str]:
    """The caller-visible ERROR envelope: a constant reason code and nothing else.

    Always carries its one key, so it is always truthy — a falsy value in a
    caller-facing slot would re-open AgentBaseGraph.get_output()'s
    ``formatted_output or result`` fallback.
    """
    if reason not in ERROR_REASONS:
        raise ValueError("error envelope reason must be one of ERROR_REASONS")
    return {"reason": reason}


def _contain(reason: str, new_errors: list[str]) -> dict[str, Any]:
    """Fail-closed refusal delta: nothing published, the closed-set reason recorded.

    ``new_errors`` are this node's own lines (they name a field or a pattern,
    never a value) and go to error_log — the internal channel. Entries already
    in error_log are not re-emitted: the state reducer appends, so they would
    be duplicated. No formatted_output, result or audit_entry key is written.
    """
    return {
        "status": AgentStatus.ERROR.value,
        "error_reason": reason,
        "error_log": list(new_errors),
    }


def _check_rendered_tokens(
    intent_category: Any,
    urgency_level: Any,
    department_assignment: Any,
    sla_hours: Any,
    escalation_path: Any,
) -> tuple[Optional[str], int]:
    """Layer 1: verify every value about to be rendered is inert.

    Returns (offending_field, checked_sla_hours). offending_field is the name of
    the first field that fails, or None when all pass; the deadline comes back
    as a plain int only on the passing path (0 otherwise, never rendered).
    """
    for field, value in (
        ("intent_category", intent_category),
        ("urgency_level", urgency_level),
        ("department_assignment", department_assignment),
    ):
        if not isinstance(value, str) or not _INERT_LABEL_RE.match(value):
            return field, 0

    if isinstance(sla_hours, bool) or not isinstance(sla_hours, int):
        return "sla_hours", 0
    if not _SLA_HOURS_MIN <= sla_hours <= _SLA_HOURS_MAX:
        return "sla_hours", 0

    if escalation_path is not None:
        if not isinstance(escalation_path, str) or not _INERT_LABEL_RE.match(escalation_path):
            return "escalation_path", 0

    return None, int(sla_hours)


def _redact_verbatim_input(content: str, state: AgentState) -> tuple[str, list[str]]:
    """Layer 2: replace verbatim embeddings of the inquiry text.

    Returns (sanitised_content, redacted_field_names).
    """
    redacted: list[str] = []
    for field in _BLOCKED_STATE_FIELDS:
        value = state.get(field)
        if isinstance(value, str) and len(value) >= _MIN_VERBATIM_CHARS and value in content:
            content = content.replace(value, "[REDACTED]")
            redacted.append(field)
    return content, redacted


def _scan_for_disallowed(content: str) -> Optional[str]:
    """Layer 3: scan the output for credential-like or personal identifiers.

    Returns the first violation name, or None when the output is clean. A
    module-level function, not a node instance method — that form keeps the
    scan outside the framework's node-method wrapping.
    """
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    return None


def _build_formatted_output(
    intent_category: str,
    urgency_level: str,
    department_assignment: str,
    sla_hours: int,
    escalation_path: Optional[str],
    pii_detected: bool,
) -> str:
    """Assemble the caller-facing routing decision.

    Structured decision fields only — every part of this string has already
    cleared the rendered-token grammar.
    """
    parts = [
        f"Category: {intent_category}",
        f"Urgency: {urgency_level}",
        f"Department: {department_assignment}",
        f"SLA: {sla_hours}h",
    ]
    if escalation_path:
        parts.append(f"Escalation: {escalation_path}")
    if pii_detected:
        parts.append("Note: the inquiry contained personal information, which was removed before processing")
    return " | ".join(parts)


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class SecurityGateOutputNode(FunctionNode):
    """Output gate and audit record for GOV-C2-003 (outer post_process slot).

    Output gate (inline in execute, three layers — see the module docstring):
      - every rendered field must be an inert identifier / a bounded whole
        number of hours, else nothing is published
      - the inquiry text must not appear verbatim in the output
      - the output must contain no credential-like or personal identifiers

    Audit record (inline in execute):
      - emit_trace_event with the routing outcome and the session identifiers
      - the inquiry text is NOT part of the audit payload
      - audit_entry is stored in State for downstream persistence

    Declared ANONYMOUS: trust was already enforced at the outer pre_process
    slot (ValidateInputNode, VERIFIED_EXTERNAL).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        intent_category = state.get("intent_category", "other")
        urgency_level = state.get("urgency_level", "low")
        department_assignment = state.get("department_assignment", "general_inquiry_dept")
        routing_metadata = state.get("routing_metadata") or {}
        pii_detected = bool(state.get("pii_detected", False))
        sla_hours = routing_metadata.get("sla_hours")
        escalation_path = routing_metadata.get("escalation_path")

        # ── Layer 1: rendered-token grammar ───────────────────────────────────
        offending_field, checked_sla_hours = _check_rendered_tokens(
            intent_category, urgency_level, department_assignment, sla_hours, escalation_path
        )
        if offending_field is not None:
            # Name the field, never the value: a rejected value is exactly the
            # kind of content that must not be echoed back.
            emit_trace_event(
                "gov_c2_003.security_gate_output.rendered_token_rejected",
                {"field": offending_field, "reason": _REASON_OUTPUT_WITHHELD},
                state,
            )
            return _contain(
                _REASON_OUTPUT_WITHHELD,
                [f"SecurityGateOutputNode: output blocked — {offending_field} is not a renderable routing value"],
            )

        formatted_output = _build_formatted_output(
            intent_category=intent_category,
            urgency_level=urgency_level,
            department_assignment=department_assignment,
            sla_hours=checked_sla_hours,
            escalation_path=escalation_path,
            pii_detected=pii_detected,
        )

        # ── Layer 2: verbatim inquiry-text embedding ──────────────────────────
        formatted_output, redacted_fields = _redact_verbatim_input(formatted_output, state)
        if redacted_fields:
            emit_trace_event(
                "gov_c2_003.security_gate_output.verbatim_redaction",
                {"fields": redacted_fields},
                state,
            )

        # ── Layer 3: credential / personal-identifier scan ────────────────────
        violation = _scan_for_disallowed(formatted_output)
        if violation:
            emit_trace_event(
                "gov_c2_003.security_gate_output.disallowed_pattern",
                {"violation": violation, "reason": _REASON_OUTPUT_WITHHELD},
                state,
            )
            return _contain(
                _REASON_OUTPUT_WITHHELD,
                [f"SecurityGateOutputNode: output blocked — disallowed pattern ({violation})"],
            )

        # ── Audit record — no inquiry text ────────────────────────────────────
        audit_entry = {
            "session_id": state.get("session_id", ""),
            "trace_id": state.get("trace_id", ""),
            "channel": state.get("channel", ""),
            "intent_category": intent_category,
            "intent_confidence": state.get("intent_confidence"),
            "urgency_level": urgency_level,
            "department_assignment": department_assignment,
            "sla_hours": checked_sla_hours,
            "escalation_path": escalation_path,
            "contact_key": routing_metadata.get("contact_key"),
            "pii_detected": pii_detected,
            # The inquiry text is intentionally absent from the audit payload.
        }

        emit_trace_event(
            "gov_c2_003.security_gate_output.routed",
            audit_entry,
            state,
        )

        return {
            "formatted_output": formatted_output,
            "result": formatted_output,
            "audit_entry": audit_entry,
            "status": AgentStatus.SUCCESS.value,
        }
