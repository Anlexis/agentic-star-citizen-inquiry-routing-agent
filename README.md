# Citizen Inquiry Classification & Routing Agent

AI agent for classifying and routing citizen inquiries, built with Agentic Star.

> **Category**: Cat 2 (domain-specific classification and routing pipeline)
> **Industry**: Government
> **Template ID**: GOV-C2-003

## Overview

A triage agent for citizen inquiries to a public-sector contact centre. It reads a single free-text inquiry, classifies it into one of seven administrative categories (permit, tax, social welfare, public safety, infrastructure, complaint, other), assigns an urgency level from the category and the wording of the inquiry, and routes it to the responsible department with a service-level deadline, an escalation path and a contact key. Classification is deterministic keyword matching — the same inquiry always produces the same routing decision, which is what an administrative record has to be able to show. Each agency supplies its own department table, either in `config/config.yaml` or per request, so no agency's org chart is baked into the code. Personal information found in the inquiry (individual numbers, postal codes, phone numbers) is stripped before classification and never reaches the routing decision, the response or the audit record.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails during
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design document and the test specification.

## Customising

1. Put your own department table in `config/config.yaml` (`routing.table`) — category key,
   department, SLA hours, escalation path, contact key.
2. Adapt the category keyword sets and urgency signals in `src/nodes/classify_intent_node.py`
   and `src/nodes/assign_urgency_node.py` to your own taxonomy and language.
3. Adjust the personal-information patterns in `src/nodes/validate_input_node.py` to the
   identifiers used in your jurisdiction.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

