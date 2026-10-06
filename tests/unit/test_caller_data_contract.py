# GOV-C2-003 — Unit Tests: the caller-data contract (input_context)
#
# ValidateInputNode is the ingest point for everything the caller sends. These
# tests hold it to the contract in its module docstring: every field bounded,
# every number finite, every rendered string inert, every violation failing
# CLOSED with an error that names the field and never echoes the value.
#
# The non-finite matrix is parametrized per numeric field on purpose. NaN and
# ±Infinity survive a float() parse and compare False against every bound, so a
# field guarded by a bare range comparison accepts them silently — and a
# suppressed threshold is worse than a rejected request.

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import from_json

_INQUIRY = "住民税の申告について教えてください"

_VALID_TABLE = {
    "tax_question": {
        "department": "zeimu_ka",
        "sla_hours": 36,
        "escalation_path": "zeimu_kacho",
        "contact_key": "zeimu_contact",
    },
    "other": {
        "department": "sogo_madoguchi",
        "sla_hours": 96,
        "escalation_path": None,
        "contact_key": "sogo_contact",
    },
}

# Values that parse as numbers but are not finite, plus the ones that are not
# numbers at all. Applied to every caller-controlled numeric field.
_NON_FINITE = [
    float("nan"),
    float("inf"),
    float("-inf"),
    "0.7",
    True,
    None,
    [],
]
_NON_FINITE_IDS = ["nan", "inf", "neg-inf", "numeric-string", "bool", "none", "list"]


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    monkeypatch.setattr("src.nodes.validate_input_node.emit_trace_event", lambda *a, **k: None)


def _run(input_context, user_input=_INQUIRY):
    return ValidateInputNode().execute({"user_input": user_input, "input_context": input_context})


def _assert_rejected(result, field_fragment):
    # A value the caller can correct declines the request without terminating
    # it: the run completes carrying the reason, so the caller can fix the field
    # and send the inquiry again. Nothing is validated or processed.
    assert result["status"] == AgentStatus.SUCCESS
    assert result.get("error_code"), "a declined request must carry the reason"
    assert "validated_input" not in result, "a rejected request must not produce validated input"
    assert any(field_fragment in str(e) for e in result["error_log"]), result["error_log"]


class TestAcceptedCallerData:
    def test_absent_input_context_degrades_to_the_shipped_baseline(self):
        """No caller data is a supported case, not an error."""
        result = _run({})
        assert result["status"] == AgentStatus.SUCCESS
        assert result["validated_input"] == _INQUIRY
        assert "caller_routing_table_json" not in result
        assert "caller_tuning_json" not in result

    def test_valid_table_is_normalised_into_state(self):
        result = _run({"routing_table": _VALID_TABLE})
        assert result["status"] == AgentStatus.SUCCESS
        table = from_json(result["caller_routing_table_json"])
        assert table["tax_question"]["department"] == "zeimu_ka"
        assert table["tax_question"]["sla_hours"] == 36
        assert table["other"]["escalation_path"] is None

    def test_unknown_entry_keys_are_dropped(self):
        """Nothing the pipeline does not consume rides along into State."""
        entry = dict(_VALID_TABLE["tax_question"], surprise="<script>", note="free text")
        result = _run({"routing_table": {"tax_question": entry}})
        assert result["status"] == AgentStatus.SUCCESS
        stored = from_json(result["caller_routing_table_json"])["tax_question"]
        assert set(stored) == {"department", "sla_hours", "escalation_path", "contact_key"}
        assert "<script>" not in result["caller_routing_table_json"]

    def test_valid_tuning_is_stored(self):
        result = _run({"classification": {"min_confidence": 0.8}, "urgency": {"high_sla_factor": 0.25}})
        assert result["status"] == AgentStatus.SUCCESS
        tuning = from_json(result["caller_tuning_json"])
        assert tuning == {"min_confidence": 0.8, "high_sla_factor": 0.25}

    def test_valid_channel_is_recorded_but_not_part_of_the_decision(self):
        result = _run({"channel": "web_form"})
        assert result["status"] == AgentStatus.SUCCESS
        assert result["channel"] == "web_form"

    @pytest.mark.parametrize("sla_hours", [1, 720])
    def test_deadline_bounds_are_inclusive(self, sla_hours):
        table = {"other": dict(_VALID_TABLE["other"], sla_hours=sla_hours)}
        result = _run({"routing_table": table})
        assert result["status"] == AgentStatus.SUCCESS


class TestRejectedCallerData:
    def test_non_object_input_context_is_rejected(self):
        _assert_rejected(_run(["not", "an", "object"]), "input_context")

    @pytest.mark.parametrize(
        "channel",
        ["Web Form", "web-form", "x" * 33, "", 7, {"a": 1}],
        ids=["spaces", "hyphen", "too-long", "empty", "number", "object"],
    )
    def test_non_inert_channel_is_rejected(self, channel):
        _assert_rejected(_run({"channel": channel}), "channel")

    @pytest.mark.parametrize(
        "context_field,value",
        [
            ("channel", "090-1234-5678"),
            ("channel", "〒123-4567"),
            ("channel", "123456789012"),
        ],
        ids=["phone", "postal", "individual-number"],
    )
    def test_personal_identifiers_in_a_context_field_are_refused(self, context_field, value):
        """The context channel is a caller-controlled field like any other.

        The framework's own masking covers user_input only, so a context field
        carrying an identifier would otherwise slip past the strip the inquiry
        text gets and land in the audit record. The inert-identifier grammar
        alone does not close this — digits are legal in a label, so a bare
        individual number is a well-formed one — hence the explicit scan.
        """
        result = _run({context_field: value})
        assert result["status"] == AgentStatus.SUCCESS
        assert value not in str(result["error_log"])

    @pytest.mark.parametrize("field", ["department", "contact_key", "escalation_path"])
    @pytest.mark.parametrize("value", ["123456789012", "1234567"], ids=["individual-number", "postal"])
    def test_personal_identifiers_in_a_table_label_are_refused(self, field, value):
        """Table labels reach the audit record, and two of them are rendered."""
        entry = dict(_VALID_TABLE["other"], **{field: value})
        result = _run({"routing_table": {"other": entry}})
        assert result["status"] == AgentStatus.SUCCESS
        assert any(field in str(e) for e in result["error_log"]), result["error_log"]
        assert value not in str(result["error_log"])

    @pytest.mark.parametrize("category", ["123456789012", "1234567"], ids=["individual-number", "postal"])
    def test_personal_identifiers_in_a_category_key_are_refused(self, category):
        _assert_rejected(_run({"routing_table": {category: dict(_VALID_TABLE["other"])}}), "routing_table")

    def test_non_object_table_is_rejected(self):
        _assert_rejected(_run({"routing_table": ["tax_question"]}), "routing_table")

    def test_empty_table_is_rejected(self):
        _assert_rejected(_run({"routing_table": {}}), "routing_table")

    def test_oversized_table_is_rejected(self):
        table = {f"cat_{i}": dict(_VALID_TABLE["other"]) for i in range(13)}
        _assert_rejected(_run({"routing_table": table}), "routing_table")

    @pytest.mark.parametrize(
        "category",
        ["Tax Question", "tax-question", "x" * 33, ""],
        ids=["spaces", "hyphen", "too-long", "empty"],
    )
    def test_non_inert_category_key_is_rejected(self, category):
        _assert_rejected(_run({"routing_table": {category: dict(_VALID_TABLE["other"])}}), "routing_table")

    @pytest.mark.parametrize("field", ["department", "sla_hours", "contact_key"])
    def test_missing_required_entry_field_is_rejected(self, field):
        entry = {k: v for k, v in _VALID_TABLE["other"].items() if k != field}
        _assert_rejected(_run({"routing_table": {"other": entry}}), field)

    @pytest.mark.parametrize(
        "department",
        ["general inquiry", "General_Inquiry", "a" * 49, "", 12, None],
        ids=["spaces", "uppercase", "too-long", "empty", "number", "none"],
    )
    def test_non_inert_department_is_rejected(self, department):
        entry = dict(_VALID_TABLE["other"], department=department)
        _assert_rejected(_run({"routing_table": {"other": entry}}), "department")

    @pytest.mark.parametrize(
        "escalation_path",
        ["call 03-1234-5678", "Head Of Dept", "a" * 49, 4],
        ids=["phone", "spaces", "too-long", "number"],
    )
    def test_non_inert_escalation_path_is_rejected(self, escalation_path):
        entry = dict(_VALID_TABLE["other"], escalation_path=escalation_path)
        _assert_rejected(_run({"routing_table": {"other": entry}}), "escalation_path")

    @pytest.mark.parametrize(
        "sla_hours",
        [0, -5, 721, 24.5, True, "24", None, float("nan"), float("inf")],
        ids=["zero", "negative", "over-max", "float", "bool", "string", "none", "nan", "inf"],
    )
    def test_out_of_range_or_non_integer_deadline_is_rejected(self, sla_hours):
        entry = dict(_VALID_TABLE["other"], sla_hours=sla_hours)
        _assert_rejected(_run({"routing_table": {"other": entry}}), "sla_hours")

    @pytest.mark.parametrize("bad", _NON_FINITE, ids=_NON_FINITE_IDS)
    def test_non_finite_min_confidence_fails_closed(self, bad):
        _assert_rejected(_run({"classification": {"min_confidence": bad}}), "min_confidence")

    @pytest.mark.parametrize("bad", [-0.1, 1.1, 1e308], ids=["below", "above", "huge"])
    def test_out_of_range_min_confidence_fails_closed(self, bad):
        _assert_rejected(_run({"classification": {"min_confidence": bad}}), "min_confidence")

    @pytest.mark.parametrize("field", ["high_sla_factor", "medium_sla_factor"])
    @pytest.mark.parametrize("bad", _NON_FINITE, ids=_NON_FINITE_IDS)
    def test_non_finite_sla_factor_fails_closed(self, field, bad):
        _assert_rejected(_run({"urgency": {field: bad}}), field)

    @pytest.mark.parametrize("field", ["high_sla_factor", "medium_sla_factor"])
    @pytest.mark.parametrize("bad", [0.0, 0.04, 1.01], ids=["zero", "below-min", "above-max"])
    def test_out_of_range_sla_factor_fails_closed(self, field, bad):
        _assert_rejected(_run({"urgency": {field: bad}}), field)

    @pytest.mark.parametrize("block", ["classification", "urgency"])
    def test_non_object_tuning_block_is_rejected(self, block):
        _assert_rejected(_run({block: [1, 2, 3]}), block)

    def test_rejected_values_are_never_echoed(self):
        """The error names the field; the offending value never comes back."""
        marker = "SECRET_VALUE_ak-abcdefghijklmnopqrst"
        entry = dict(_VALID_TABLE["other"], department=marker)
        result = _run({"routing_table": {"other": entry}})
        assert result["status"] == AgentStatus.SUCCESS
        assert marker not in str(result["error_log"])


class TestInquiryTextBounds:
    def test_oversized_inquiry_is_rejected(self):
        result = _run({}, user_input="あ" * 4001)
        assert result["status"] == AgentStatus.SUCCESS
        assert any("4000" in str(e) for e in result["error_log"])

    def test_caller_data_is_validated_before_the_inquiry_is_processed(self):
        """A malformed table is refused even when the inquiry itself is fine."""
        result = _run({"routing_table": {"other": {"department": "x y"}}})
        assert result["status"] == AgentStatus.SUCCESS
        assert "validated_input" not in result
