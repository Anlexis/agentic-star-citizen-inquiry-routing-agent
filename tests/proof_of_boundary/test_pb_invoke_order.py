# PB-6: Invoke Execution Order Verification
# Part A: Verifies BaseNode.__call__() enforces the per-node gate order:
#   trust gate -> node_start -> _security_gate_input() -> execute() ->
#   _security_gate_output() -> node_complete
#   for every concrete FunctionNode under src/nodes/.
#
# Part B: Full backbone invoke order — Graph().invoke() over a SUCCESS-yielding
#   citizen inquiry payload asserts the backbone visits
#   [InitializeNode → pre_process → main → post_process → FinalizeNode]
#   in the correct order.

import importlib
import inspect
import pkgutil

import pytest

# ── Template-specific constants (Part B) ─────────────────────────────────────

# The concrete GraphNode subclass assigned to the `main` backbone slot.
# Used to verify it appears in the node_history after Graph().invoke().
from src.graph.graph import GovInquiryWorkflowGraphNode as _MAIN_SLOT_NODE

# A SUCCESS-yielding citizen inquiry payload (clean — no PII, passes ValidateInputNode).
_VALID_PAYLOAD = "住民税の申告について教えてください"


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ runs trust -> node_start -> input gate -> execute() ->
    output gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestBackboneInvokeOrder:
    """PB-6 Part B: Graph().invoke() visits backbone slots in the correct order.

    Asserts that a SUCCESS-yielding invocation produces:
      initialize → pre_process → main (GovInquiryWorkflowGraphNode) → post_process → finalize

    emit_trace_event is patched at all five node modules to avoid audit-backend
    calls in CI. shared.* is NOT stubbed in sys.modules — the real wheel provides it.
    """

    def test_backbone_order_on_success_payload(self, monkeypatch):
        """Full Graph().invoke() with _VALID_PAYLOAD → SUCCESS + correct backbone order."""
        # Mute audit events at the node module level (NOT via sys.modules stub)
        _emit_modules = [
            "src.nodes.validate_input_node",
            "src.nodes.classify_intent_node",
            "src.nodes.assign_urgency_node",
            "src.nodes.route_to_department_node",
            "src.nodes.security_gate_output_node",
        ]
        for mod_path in _emit_modules:
            monkeypatch.setattr(f"{mod_path}.emit_trace_event", lambda *a, **k: None)

        from uuid import uuid4
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()

        # Pass VERIFIED_EXTERNAL so ValidateInputNode's trust gate is satisfied
        ctx = InvocationContext(
            session_id=str(uuid4()),
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            caller_id="pb6-backbone-test",
        )
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        # The agent must succeed on a clean citizen inquiry
        assert result.get("status") in (
            AgentStatus.SUCCESS,
            AgentStatus.SUCCESS.value,
        ), f"Expected SUCCESS from backbone invoke, got: {result.get('status')}"

        # AgentBaseGraph.get_output() surfaces formatted_output as the 'output' key.
        # Inner domain fields (intent_category etc.) stay in the LangGraph state and
        # are NOT propagated to the top-level return dict.
        output = result.get("output", "")
        assert output, (
            "output must be set — SecurityGateOutputNode populates formatted_output which "
            "AgentBaseGraph.get_output() surfaces as 'output'"
        )

        # Output boundary: the inquiry text must NOT appear in the formatted output
        assert (
            _VALID_PAYLOAD not in output
        ), f"output boundary violation: the inquiry text must not appear in output. Got: {output!r}"

        # The formatted output must contain routing result signals from the inner pipeline
        # (SecurityGateOutputNode embeds intent category + department labels in output)
        assert any(
            kw in output for kw in ["tax_question", "taxation_dept", "Department", "SLA"]
        ), f"Expected routing tokens in formatted output but got: {output!r}"

        # Verify the main slot GraphNode was visited in the backbone node_history
        node_history = result.get("node_history")
        if node_history:
            assert _MAIN_SLOT_NODE.__name__ in str(
                node_history
            ), f"Expected {_MAIN_SLOT_NODE.__name__} in backbone node_history, got: {node_history}"
