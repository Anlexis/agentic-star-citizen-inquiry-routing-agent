"""AgentCore Platform v1.0 — GOV-C2-003: ClassifyIntentNode"""

# Inner domain graph node — classify_intent slot.
# Classifies citizen inquiry text into one of 7 administrative categories using
# rules-based keyword matching: deterministic, no model call, so the same
# inquiry always produces the same category — which is what an administrative
# record has to be able to show.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# 7 administrative categories with Japanese keyword signals.
# Each tuple: (category_key, [keyword_list])
# Order determines priority when several categories score equally.
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("public_safety", ["犯罪", "緊急", "警察", "事件", "事故", "危険", "不審"]),
    ("permit_inquiry", ["許可", "申請", "建築", "届出", "手続き", "認可", "免許"]),
    ("tax_question", ["税", "課税", "申告", "控除", "納税", "確定申告", "住民税", "固定資産税"]),
    ("social_welfare", ["福祉", "介護", "生活保護", "障害", "支援", "手当", "保育"]),
    ("infrastructure", ["道路", "水道", "下水", "公共施設", "公園", "街灯", "ごみ"]),
    ("complaint", ["苦情", "クレーム", "不満", "問題", "困っている", "おかしい", "改善"]),
]
_FALLBACK_CATEGORY = "other"

# Baseline confidence when no keywords match; per-hit increment; ceiling
_CONFIDENCE_BASELINE = 0.50
_CONFIDENCE_PER_HIT = 0.15
_CONFIDENCE_CEILING = 0.95

# Confidence bar applied when config/config.yaml declares none. 0.5 accepts
# every score the rules can produce, so an undeclared bar changes nothing.
_DEFAULT_MIN_CONFIDENCE = 0.50


def _classify(text: str) -> tuple[str, float]:
    """Keyword-based classification. Returns (category, confidence)."""
    best_category = _FALLBACK_CATEGORY
    best_score = 0.0

    for category, keywords in _CATEGORY_RULES:
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            score = min(_CONFIDENCE_BASELINE + hits * _CONFIDENCE_PER_HIT, _CONFIDENCE_CEILING)
            if score > best_score:
                best_score = score
                best_category = category

    if best_category == _FALLBACK_CATEGORY:
        best_score = _CONFIDENCE_BASELINE

    return best_category, best_score


class ClassifyIntentNode(FunctionNode):
    """Classify citizen inquiry text into one of 7 administrative categories.

    Inner domain graph node (classify_intent slot). Receives the sanitized
    inquiry text as user_input from GovInquiryWorkflowGraphNode.extract_input().

    Categories: permit_inquiry | tax_question | social_welfare | public_safety |
                infrastructure | complaint | other

    Confidence: 0.5 baseline + 0.15 per keyword hit, capped at 0.95.

    A best category scoring below the effective confidence bar
    (state["min_confidence"], seeded from config/config.yaml or the caller's
    validated override) is filed as "other" instead of being guessed into a
    department — the reported confidence stays the measured score, so the audit
    record shows why the inquiry went to the general desk.
    """

    # Inner graph node — explicitly ANONYMOUS.
    # Trust is enforced at the outer pre_process slot (ValidateInputNode,
    # VERIFIED_EXTERNAL). It must be ANONYMOUS rather than INTERNAL because
    # GraphNode.execute() propagates the outer caller's level to the inner
    # graph without escalating it, and VERIFIED_EXTERNAL ranks below INTERNAL —
    # so INTERNAL here would be denied at runtime. An explicit declaration is
    # required: the trust-level check rejects a node that inherits it.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # The inner graph receives the sanitized inquiry text as user_input
        text = state.get("user_input", "")

        if not text:
            emit_trace_event(
                "gov_c2_003.classify_intent.error",
                {"reason": "empty_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: user_input is empty"],
            }

        intent_category, intent_confidence = _classify(text)

        raw_bar = state.get("min_confidence")
        min_confidence = (
            float(raw_bar)
            if isinstance(raw_bar, (int, float)) and not isinstance(raw_bar, bool)
            else _DEFAULT_MIN_CONFIDENCE
        )
        below_bar = intent_confidence < min_confidence
        if below_bar:
            intent_category = _FALLBACK_CATEGORY

        emit_trace_event(
            "gov_c2_003.classify_intent.classified",
            {
                "intent_category": intent_category,
                "intent_confidence": intent_confidence,
                "min_confidence": min_confidence,
                "below_confidence_bar": below_bar,
            },
            state,
        )

        return {
            "intent_category": intent_category,
            "intent_confidence": intent_confidence,
            "status": AgentStatus.SUCCESS.value,
        }
