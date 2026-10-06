"""AgentCore Platform v1.0"""

# Registry entry point.
#
# config/agent.yaml declares the agent as a single dotted path:
#     class: "src.graph.graph.CitizenInquiryClassificationRoutingAgent"
# which the registry resolves against the module that defines it.
#
# Re-exporting here keeps the shorter package-level path
# (src.graph.CitizenInquiryClassificationRoutingAgent) working as well, for
# callers that import the agent directly rather than through the manifest.
#
# Graph is exported alongside it because src/graph/graph.py defines the agent
# as `Graph` and aliases the manifest name to it; both are part of the public
# surface.

from .graph import CitizenInquiryClassificationRoutingAgent, Graph

__all__ = ["CitizenInquiryClassificationRoutingAgent", "Graph"]
