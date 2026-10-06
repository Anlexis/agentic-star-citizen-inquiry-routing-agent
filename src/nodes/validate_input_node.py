"""AgentCore Platform v1.0 — GOV-C2-003: ValidateInputNode"""

# Outer backbone pre_process slot — the trust boundary and the ingest point for
# everything the caller sends.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never the full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Never import from mediator/, api/, or other agents
#  - Security checks inline in execute() — NOT as _extra_security_gate_*
#    instance methods (the framework wraps methods of that name, and a wrapped
#    hook returning None is passed on as the next node's state)
#
# Caller-data contract (input_context — every field hostile until proven
# bounded; a violation fails CLOSED with an error naming the FIELD, never
# echoing the value):
#
#   input_context = {
#     "channel": str ^[a-z0-9_]{1,32}$          (optional; audit metadata only,
#                                                never rendered to the caller)
#     "routing_table": {                        (optional, 1..12 entries)
#       "<category>": {                         key ^[a-z0-9_]{1,32}$
#         "department":      str ^[a-z0-9_]{1,48}$    (required; rendered)
#         "sla_hours":       int 1..720               (required; rendered)
#         "escalation_path": str ^[a-z0-9_]{1,48}$    (optional, or null)
#         "contact_key":     str ^[a-z0-9_]{1,48}$    (required)
#       }, ...
#     }
#     "classification": {                       (optional)
#       "min_confidence": number 0.0..1.0       (finite; below it an inquiry is
#                                                filed as "other")
#     }
#     "urgency": {                              (optional)
#       "high_sla_factor":   number 0.05..1.0   (finite)
#       "medium_sla_factor": number 0.05..1.0   (finite)
#     }
#   }
#
# Every caller string that can reach the caller-facing output is locked to an
# inert identifier, so there is no free text on that path at all. Every caller
# number goes through the finite+bounded parsers in src/schemas/state.py, which
# reject NaN / ±Infinity: those parse happily through float() and compare False
# against every bound, which would turn a threshold into a silent no-op.
#
# A validated table/tuning set is written to State here and carried into the
# inner graph by src/graph/context_bridge.py — the raw payload never crosses
# that boundary.

import re
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.schemas.state import finite_in_range, finite_int_in_range, to_json

# Maximum accepted inquiry length in characters (oversize guard)
_MAX_INPUT_CHARS = 4000

# Personal-identifier patterns for Japanese public-sector inquiries.
# Individual number (マイナンバー): a 12-digit sequence, optionally grouped
_PATTERN_INDIVIDUAL_NUMBER = re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}\b")
# Postal code (〒NNN-NNNN)
_PATTERN_POSTAL = re.compile(r"〒?\d{3}[-\s]?\d{4}")
# Domestic phone number (0N-NNNN-NNNN and the other national groupings)
_PATTERN_PHONE = re.compile(r"0\d{1,4}[-\s]\d{2,4}[-\s]\d{4}")

_PII_PATTERNS = [_PATTERN_INDIVIDUAL_NUMBER, _PATTERN_POSTAL, _PATTERN_PHONE]

# Replacement token for stripped personal identifiers
_PII_MASK = "[PII_REDACTED]"

# ── Caller-data validation bounds ────────────────────────────────────────────
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_CATEGORY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_LABEL_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_MAX_ROUTING_ENTRIES = 12
_SLA_HOURS_MIN, _SLA_HOURS_MAX = 1, 720
_MIN_CONFIDENCE_LO, _MIN_CONFIDENCE_HI = 0.0, 1.0
_SLA_FACTOR_LO, _SLA_FACTOR_HI = 0.05, 1.0

_REQUIRED_ENTRY_FIELDS = ("department", "sla_hours", "contact_key")


def _strip_pii(text: str) -> tuple[str, bool]:
    """Strip personal identifiers from text. Returns (sanitized_text, was_detected)."""
    detected = False
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            detected = True
            text = pattern.sub(_PII_MASK, text)
    return text, detected


def _carries_personal_identifier(value: str) -> bool:
    """True when a caller LABEL carries one of the personal-identifier patterns.

    The inert-identifier grammar alone does not close this: digits are legal in
    a label, so a bare individual number or postal code is a well-formed label.
    Labels land in the audit record and some are rendered in the decision, so
    they get the same scan the inquiry text gets — and are refused outright
    rather than masked, because a label is meant to be an agency's own key, not
    caller data that needs sanitising.
    """
    return any(pattern.search(value) for pattern in _PII_PATTERNS)


def _validate_routing_table(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate a caller-supplied department table.

    Returns (normalised_table, error_message). Error messages name the category
    and field only — never the offending value. Unknown entry keys are dropped
    from the normalised result, so nothing the pipeline does not consume can
    ride along into State.
    """
    if not isinstance(raw, dict):
        return None, "routing_table must be an object"
    if not 1 <= len(raw) <= _MAX_ROUTING_ENTRIES:
        return None, f"routing_table must contain between 1 and {_MAX_ROUTING_ENTRIES} entries"

    normalised: dict[str, Any] = {}
    for category, entry in raw.items():
        if not isinstance(category, str) or not _CATEGORY_RE.match(category):
            return None, "routing_table keys must be strings matching [a-z0-9_]{1,32}"
        if _carries_personal_identifier(category):
            return None, "routing_table keys must not contain a personal identifier"
        if not isinstance(entry, dict):
            return None, f"routing_table[{category}] must be an object"

        for field in _REQUIRED_ENTRY_FIELDS:
            if field not in entry:
                return None, f"routing_table[{category}].{field} is required"

        department = entry.get("department")
        if not isinstance(department, str) or not _LABEL_RE.match(department):
            return None, f"routing_table[{category}].department must be a string matching [a-z0-9_]{{1,48}}"
        if _carries_personal_identifier(department):
            return None, f"routing_table[{category}].department must not contain a personal identifier"

        contact_key = entry.get("contact_key")
        if not isinstance(contact_key, str) or not _LABEL_RE.match(contact_key):
            return None, f"routing_table[{category}].contact_key must be a string matching [a-z0-9_]{{1,48}}"
        if _carries_personal_identifier(contact_key):
            return None, f"routing_table[{category}].contact_key must not contain a personal identifier"

        sla_hours = finite_int_in_range(entry.get("sla_hours"), _SLA_HOURS_MIN, _SLA_HOURS_MAX)
        if sla_hours is None:
            return None, (
                f"routing_table[{category}].sla_hours must be a whole number of hours "
                f"between {_SLA_HOURS_MIN} and {_SLA_HOURS_MAX}"
            )

        escalation_path = entry.get("escalation_path", None)
        if escalation_path is not None:
            if not isinstance(escalation_path, str) or not _LABEL_RE.match(escalation_path):
                return None, (
                    f"routing_table[{category}].escalation_path must be null or a string " f"matching [a-z0-9_]{{1,48}}"
                )
            if _carries_personal_identifier(escalation_path):
                return None, f"routing_table[{category}].escalation_path must not contain a personal identifier"

        normalised[category] = {
            "department": department,
            "sla_hours": sla_hours,
            "escalation_path": escalation_path,
            "contact_key": contact_key,
        }
    return normalised, None


def _validate_tuning(input_context: dict[str, Any]) -> tuple[Optional[dict[str, float]], Optional[str]]:
    """Validate the caller's classification / urgency overrides.

    Every value goes through the finite+bounded parser and fails CLOSED:
    bools, strings, NaN, ±Infinity and out-of-range magnitudes are all rejected
    with an error naming the field.
    """
    tuning: dict[str, float] = {}

    classification = input_context.get("classification")
    if classification is not None:
        if not isinstance(classification, dict):
            return None, "classification must be an object"
        if "min_confidence" in classification:
            parsed = finite_in_range(classification["min_confidence"], _MIN_CONFIDENCE_LO, _MIN_CONFIDENCE_HI)
            if parsed is None:
                return None, (
                    f"classification.min_confidence must be a finite number between "
                    f"{_MIN_CONFIDENCE_LO} and {_MIN_CONFIDENCE_HI}"
                )
            tuning["min_confidence"] = parsed

    urgency = input_context.get("urgency")
    if urgency is not None:
        if not isinstance(urgency, dict):
            return None, "urgency must be an object"
        for field in ("high_sla_factor", "medium_sla_factor"):
            if field in urgency:
                parsed = finite_in_range(urgency[field], _SLA_FACTOR_LO, _SLA_FACTOR_HI)
                if parsed is None:
                    return None, (
                        f"urgency.{field} must be a finite number between {_SLA_FACTOR_LO} and {_SLA_FACTOR_HI}"
                    )
                tuning[field] = parsed

    return tuning, None


# Rejection reason -> the code the output node turns into a caller-facing
# sentence. An unlisted reason falls back to the generic one.
_REASON_CODES = {
    "empty_input": "EMPTY_INPUT",
    "oversized_input": "QUESTION_TOO_LONG",
}


class ValidateInputNode(FunctionNode):
    """Input validation node (outer pre_process slot).

    Responsibilities:
    - Enforce VERIFIED_EXTERNAL trust (the framework denies lower-trust callers
      before execute() runs)
    - Reject empty / whitespace-only and oversized inquiries
    - Detect and strip personal identifiers (individual number, postal code,
      phone), recording only the boolean flag — never the values
    - Validate the caller-data contract in input_context against explicit
      bounds and fail closed on any violation
    - Emit an audit event for every validation decision

    Output state keys (partial dict):
        validated_input:           str
        pii_detected:              bool
        channel:                   str   (only when the caller supplied one)
        caller_routing_table_json: str   (only when the caller supplied a table)
        caller_tuning_json:        str   (only when the caller supplied tuning)
        status:                    str
        error_log:                 list[str]  (only on ERROR)
    """

    # External-facing entry slot: verify the caller before anything is processed
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: AgentState, reason: str, message: str, **extra: Any) -> dict[str, Any]:
        """Decline the request without carrying it out, and say why.

        Every rejection reachable here is a value the caller can correct, so the
        run COMPLETES carrying the reason rather than terminating: the caller
        reads what to fix and can send a corrected inquiry on the same
        conversation. `message` names a field only and is kept to the internal
        audit channel; the caller receives the matching sentence, never the
        rejected value.
        """
        emit_trace_event(
            "gov_c2_003.validate_input.rejected",
            {"reason": reason, **extra},
            state,
        )
        emit_progress(INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": _REASON_CODES.get(reason, "INVALID_REQUEST"),
            "error_log": [f"ValidateInputNode: {message}"],
        }

    def execute(self, state: AgentState) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context") or {}

        # ── Caller-data contract shape ────────────────────────────────────────
        if not isinstance(input_context, dict):
            return self._reject(state, "input_context_not_object", "input_context must be an object")

        channel = input_context.get("channel")
        if channel is not None:
            if not isinstance(channel, str) or not _CHANNEL_RE.match(channel):
                return self._reject(
                    state,
                    "invalid_channel_label",
                    "input_context.channel must be a string matching [a-z0-9_]{1,32}",
                )
            if _carries_personal_identifier(channel):
                # The framework's own masking covers user_input only, and this
                # label lands in the audit record — so it gets the same scan the
                # inquiry text gets.
                return self._reject(
                    state,
                    "channel_carries_personal_identifier",
                    "input_context.channel must not contain a personal identifier",
                )

        # ── Inquiry text ──────────────────────────────────────────────────────
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            return self._reject(state, "empty_input", "user_input is empty or missing", pii_detected=False)

        if len(user_input) > _MAX_INPUT_CHARS:
            return self._reject(
                state,
                "oversized_input",
                f"input exceeds {_MAX_INPUT_CHARS} characters (got {len(user_input)})",
                length=len(user_input),
                pii_detected=False,
            )

        # ── Caller department table ───────────────────────────────────────────
        result: dict[str, Any] = {}
        if "routing_table" in input_context:
            table, err = _validate_routing_table(input_context["routing_table"])
            if err:
                return self._reject(state, "routing_table_rejected", err, field_error=err)
            result["caller_routing_table_json"] = to_json(table)

        # ── Caller tuning overrides ───────────────────────────────────────────
        tuning, err = _validate_tuning(input_context)
        if err or tuning is None:
            return self._reject(state, "tuning_rejected", err or "invalid tuning overrides", field_error=err)
        if tuning:
            result["caller_tuning_json"] = to_json(tuning)

        # ── Personal-identifier detection + suppression ───────────────────────
        validated_input, pii_detected = _strip_pii(user_input.strip())

        emit_trace_event(
            "gov_c2_003.validate_input.accepted",
            {
                "pii_detected": pii_detected,
                "input_length": len(validated_input),
                "caller_routing_entries": len(input_context.get("routing_table") or {})
                if isinstance(input_context.get("routing_table"), dict)
                else 0,
                "caller_tuning_fields": sorted(tuning),
            },
            state,
        )

        result["validated_input"] = validated_input
        result["pii_detected"] = pii_detected
        if channel is not None:
            result["channel"] = channel
        result["status"] = AgentStatus.SUCCESS.value
        return result
