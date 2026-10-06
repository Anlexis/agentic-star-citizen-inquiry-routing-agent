# GOV-C2-003 — Unit Tests: the caller trust gate
#
# These tests invoke nodes via node(state) — through BaseNode.__call__, which
# runs the trust gate, then the framework input gate, then execute(), then the
# framework output gate. The other suites in tests/unit/ deliberately call
# node.execute(state) to exercise the domain logic in isolation; that path
# bypasses __call__ entirely, so none of them touches the trust gate. This file
# covers only the gate and does not restate their assertions.
#
# A denial RETURNS an error dict (it never raises): status ERROR, an error_log
# entry naming the node and the required level, and — because execute() never
# ran — none of the node's own output keys.
#
# Every assertion below is pinned to an artefact of the node under test (its own
# output keys, its own error prefix, the required level named in the denial), so
# that lowering required_trust_level on the gate node makes these tests fail
# rather than pass for an unrelated reason.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.assign_urgency_node import AssignUrgencyNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.route_to_department_node import RouteToDepartmentNode
from src.nodes.security_gate_output_node import SecurityGateOutputNode
from src.nodes.validate_input_node import ValidateInputNode

# A clean, valid citizen inquiry: within the length cap, and free of the
# personal-identifier patterns the node strips — so the ONLY thing that can
# turn this input into an error is the trust gate.
_VALID_INPUT = "住民税の申告方法について教えてください"


def _state(trust_value: str, **extra) -> dict:
    state = {
        "user_input": _VALID_INPUT,
        "input_context": {},
        "caller_trust_level": trust_value,
        "node_history": [],
        "error_log": [],
        "session_id": "test-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestTrustGate:
    """Every invocation here goes through node(state) / __call__."""

    def test_anonymous_caller_denied_on_validate_input(self):
        """ANONYMOUS caller against the VERIFIED_EXTERNAL pre_process node.

        __call__ must RETURN (never raise) an error dict, and execute() must not
        have run — so validated_input / pii_detected, the only two keys this node
        always produces, are ABSENT.
        """
        node = ValidateInputNode()  # required_trust_level = VERIFIED_EXTERNAL
        result = node(_state(TrustLevel.ANONYMOUS.value))

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            "trust gate denied" in str(e) for e in error_log
        ), f"expected a trust-gate denial in error_log, got: {error_log}"
        # Pin the denial to THIS node and THIS required level: if
        # ValidateInputNode.required_trust_level is lowered, no denial is
        # produced at all and both assertions below fail.
        assert any("ValidateInputNode" in str(e) for e in error_log)
        assert any(
            "required=VERIFIED_EXTERNAL" in str(e) for e in error_log
        ), f"denial must name the required level; got: {error_log}"
        assert "validated_input" not in result, "execute() must not run on a denial — validated_input leaked"
        assert "pii_detected" not in result, "execute() must not run on a denial — pii_detected leaked"

    def test_verified_external_caller_passes_validate_input(self):
        """A VERIFIED_EXTERNAL caller clears the gate and the node runs.

        This is the other half of the assertion: the same input that is refused
        above must succeed here, which proves the refusal came from the trust
        gate and not from the node's own input validation.
        """
        node = ValidateInputNode()
        result = node(_state(TrustLevel.VERIFIED_EXTERNAL.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == _VALID_INPUT
        assert result.get("pii_detected") is False
        assert not any("trust gate denied" in str(e) for e in result.get("error_log", []))

    def test_internal_caller_passes_validate_input(self):
        """INTERNAL outranks VERIFIED_EXTERNAL, so it clears the same gate."""
        node = ValidateInputNode()
        result = node(_state(TrustLevel.INTERNAL.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == _VALID_INPUT

    def test_missing_trust_level_defaults_to_anonymous_and_is_denied(self):
        """Secure-by-default: no caller_trust_level in state is read as ANONYMOUS."""
        state = _state(TrustLevel.ANONYMOUS.value)
        del state["caller_trust_level"]
        result = ValidateInputNode()(state)

        assert result.get("status") == AgentStatus.ERROR.value
        assert any("trust gate denied" in str(e) for e in result.get("error_log", []))
        assert "validated_input" not in result

    def test_anonymous_caller_allowed_on_inner_node(self):
        """Inner domain nodes are ANONYMOUS by design and must admit that caller.

        Asserts on an artefact only this node writes (intent_category), so a pass
        here means the node really ran rather than merely not being denied.
        """
        result = ClassifyIntentNode()(_state(TrustLevel.ANONYMOUS.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("intent_category") == "tax_question"
        assert not any("trust gate denied" in str(e) for e in result.get("error_log", []))

    def test_anonymous_caller_allowed_on_output_gate(self):
        """The post_process output gate is ANONYMOUS and must admit that caller."""
        result = SecurityGateOutputNode()(
            _state(
                TrustLevel.ANONYMOUS.value,
                intent_category="tax_question",
                urgency_level="low",
                department_assignment="taxation_dept",
                routing_metadata={
                    "department": "taxation_dept",
                    "sla_hours": 24,
                    "escalation_path": None,
                    "contact_key": "taxation_contact",
                },
                pii_detected=False,
            )
        )

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "taxation_dept" in result.get("formatted_output", "")


class TestOutputBoundaryIsThisNodesGate:
    """The output refusal must come from SecurityGateOutputNode's own boundary.

    FunctionNode._security_gate_output() applies a framework-wide credential
    scan to every node's result, so asserting only `status == ERROR` would pass
    even if this node had no boundary of its own. This case trips the node's own
    rendered-token grammar — which the framework scan does not look at — and
    asserts this node's own refusal wording.
    """

    def test_non_inert_department_blocked_by_this_nodes_boundary(self):
        result = SecurityGateOutputNode()(
            _state(
                TrustLevel.ANONYMOUS.value,
                intent_category="tax_question",
                urgency_level="low",
                # A department label carrying free text is exactly what the
                # node's own grammar exists to catch before anything is released.
                department_assignment="taxation dept (see inquiry)",
                routing_metadata={
                    "department": "taxation_dept",
                    "sla_hours": 24,
                    "escalation_path": None,
                    "contact_key": "taxation_contact",
                },
                pii_detected=False,
            )
        )

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            str(e).startswith("SecurityGateOutputNode: output blocked") for e in error_log
        ), f"expected this node's own refusal, got: {error_log}"
        assert "formatted_output" not in result, "blocked output must not be released"
        assert "result" not in result
        assert "audit_entry" not in result


class TestTrustLevelMatrix:
    """The template's declared trust matrix (docs/02_design.md).

    The outer entry slot requires VERIFIED_EXTERNAL; the output slot and the
    three inner domain nodes run behind that boundary and are ANONYMOUS per the
    nested-graph convention (a higher level on an inner node would be denied at
    runtime, since GraphNode propagates the caller's level unescalated).
    """

    def test_entry_node_requires_verified_external(self):
        assert ValidateInputNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_inner_and_output_nodes_admit_anonymous(self):
        for node_cls in (
            ClassifyIntentNode,
            AssignUrgencyNode,
            RouteToDepartmentNode,
            SecurityGateOutputNode,
        ):
            assert node_cls.required_trust_level is TrustLevel.ANONYMOUS, (
                f"{node_cls.__name__} must declare TrustLevel.ANONYMOUS " "(it runs behind the outer trust boundary)"
            )
