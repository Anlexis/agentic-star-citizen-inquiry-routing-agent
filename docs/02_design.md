# Template Design Specification — GOV-C2-003

## Position in the AgentCore architecture

| Field | Value |
|---|---|
| Template ID | GOV-C2-003 |
| Agent class | `CitizenInquiryClassificationRoutingAgent` (`src/graph/graph.py`) |
| Category | Cat 2 — multi-step domain workflow, public-sector industry |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Composition | outer `AgentBaseGraph` + a `GraphNode` in the `main` slot (nested Cat-2 pattern) |
| Generation mode | deterministic — no model call anywhere in the pipeline |

Three-layer separation:

- **State** — a flat TypedDict (`State(AgentState)`), no Pydantic, no credentials.
- **Node** — `FunctionNode` subclasses implementing `execute(self, state) -> dict` and
  returning only the keys they change.
- **Graph** — composition via `register_nodes()`; edge wiring belongs to the framework.

---

## Architecture overview

### Nested Cat-2 pattern

The five domain nodes sit in two layers:

- **Outer backbone** (`src/graph/graph.py`, `AgentBaseGraph`):
  - `pre_process` → `ValidateInputNode` — trust boundary, inquiry bounds, caller-data contract
  - `main` → `GovInquiryWorkflowGraphNode(GraphNode)` — wraps the inner domain graph
  - `post_process` → `SecurityGateOutputNode` — output boundary + audit record

- **Inner domain graph** (`src/graph/domain_workflow_graph.py`, `BaseGraph`):
  - `classify_intent` → `ClassifyIntentNode`
  - `assign_urgency` → `AssignUrgencyNode`
  - `route_to_department` → `RouteToDepartmentNode`
  - Linear: `classify_intent → assign_urgency → route_to_department → END`

**Why nested:** the three domain steps have inter-step data dependencies — classify first,
then derive urgency from the category and the wording, then route from both. Collapsing them
into a single `main` node would mix three responsibilities and make each one untestable on
its own.

### Node configuration

| Node | Slot | Class | Responsibility | Trust level |
|------|------|-------|---------------|-------------|
| initialize | backbone | `InitializeNode` (framework default) | Session + schema init | — |
| pre_process | backbone, outer | `ValidateInputNode` | Inquiry bounds, personal-identifier strip, caller-data contract | `VERIFIED_EXTERNAL` |
| main | backbone, outer | `GovInquiryWorkflowGraphNode` | GraphNode wrapper — delegates to the inner graph | `ANONYMOUS` |
| classify_intent | inner graph | `ClassifyIntentNode` | Rules-based 7-category classification | `ANONYMOUS` |
| assign_urgency | inner graph | `AssignUrgencyNode` | Rules-based urgency from category + keyword signals | `ANONYMOUS` |
| route_to_department | inner graph | `RouteToDepartmentNode` | Category × urgency → department + routing metadata | `ANONYMOUS` |
| post_process | backbone, outer | `SecurityGateOutputNode` | Output boundary, audit record, final formatting | `ANONYMOUS` |
| finalize | backbone | `FinalizeNode` (framework default) | Response metadata + timing | — |

Every inner node is declared `ANONYMOUS` deliberately. `GraphNode.execute()` propagates the
outer caller's trust level into the inner graph without escalating it, and
`VERIFIED_EXTERNAL` ranks below `INTERNAL`, so an inner node declaring `INTERNAL` would be
denied at runtime for every real caller. Trust is enforced once, at the entry slot.

### Pipeline

```
Outer (AgentBaseGraph backbone):
  START → initialize → pre_process → main → {route} → post_process → finalize → END
                                              ↓ (RETRY, max 3)
                                           pre_process

Inside `main` (GovInquiryWorkflowGraphNode.execute()):
  Inner graph (BaseGraph):
    START → classify_intent → assign_urgency → route_to_department → END
```

---

## Caller-data contract (`input_context`)

`POST /invoke` accepts an optional `input_context` object alongside the inquiry. Every field
is validated at ingest against explicit bounds; a violation fails CLOSED — nothing is
classified, routed or published, and the internal audit line names the field and never echoes
the value. Every such violation is a value the caller can correct, so the run COMPLETES rather
than terminating (see *Declining a request the caller can correct* below). Absent caller data
is a supported case: the agent falls back to the settings in `config/config.yaml`, then to its
built-in defaults.

```
input_context = {
  "channel": str ^[a-z0-9_]{1,32}$          optional; audit metadata only, never rendered
  "routing_table": {                        optional, 1..12 entries
    "<category>": {                         key ^[a-z0-9_]{1,32}$
      "department":      str ^[a-z0-9_]{1,48}$    required; rendered in the decision
      "sla_hours":       int 1..720               required; rendered in the decision
      "escalation_path": str ^[a-z0-9_]{1,48}$    optional, or null
      "contact_key":     str ^[a-z0-9_]{1,48}$    required
    }, ...
  }
  "classification": { "min_confidence": number 0.0..1.0 }        optional
  "urgency": { "high_sla_factor":   number 0.05..1.0,            optional
               "medium_sla_factor": number 0.05..1.0 }
}
```

Three rules make this contract safe rather than merely documented:

1. **Every caller string that can reach the output is an inert identifier.** There is no free
   text on the rendered path at all, so caller content cannot shape the response.
2. **Every caller number goes through a finite + bounded parser**
   (`finite_in_range` / `finite_int_in_range` in `src/schemas/state.py`). `NaN` and
   `±Infinity` survive a `float()` parse, arrive through ordinary JSON, and compare False
   against every bound — a field guarded by a bare range comparison would accept them and
   silently disable the check it guards.
3. **Every caller label is scanned for personal identifiers and refused if it carries one.**
   The inert grammar alone does not close this: digits are legal in a label, so a bare
   individual number or postal code is a well-formed label. Labels land in the audit record
   and two of them are rendered, and the framework's own masking covers `user_input` only —
   so labels get the same scan the inquiry text gets, and are refused rather than masked.

The adapter additionally caps the serialized `input_context` at 256 KB and answers `413`
above it, so an oversized payload never reaches the graph.

---

## Runtime settings (`config/config.yaml`)

`config/agent.yaml` is the static manifest (identity, entry point, trust level, compile-time
requirements). The runtime parameters live beside it in `config/config.yaml`:

| Key | Meaning |
|---|---|
| `max_retry`, `timeout_s` | Backbone runtime limits |
| `classification.min_confidence` | Score below which an inquiry is filed as `other` |
| `urgency.high_sla_factor` / `.medium_sla_factor` | Multipliers on the department deadline |
| `routing.table` | The deployment's department table — replaces the built-in directory |

`GovInquiryWorkflowGraphNode._parent_config()` reads that file and forwards the declared
settings to the inner graph under `configurable`;
`GovInquiryDomainWorkflowGraph._extra_initial_state()` merges them with the validated caller
overrides (caller wins) and seeds the result into the inner state. A setting declared in
neither place leaves the consuming node on its built-in default.

### Crossing the outer → inner boundary

`GraphNode.execute()` invokes the inner graph as
`subgraph.invoke(user_input, session_id=..., ctx=...)` — it does **not** forward the caller's
`input_context`, so an inner node reading `state["input_context"]` always sees `{}`.
`src/graph/context_bridge.py` bridges the gap through the two sanctioned subclass hooks:
`extract_input()` (which runs immediately before the inner invoke) stashes the settings in a
`ContextVar`, and `_extra_initial_state()` (which runs inside it) reads them back. Only
values `ValidateInputNode` has already validated cross that boundary — the raw caller payload
never does.

---

## State definition (`src/schemas/state.py`)

| Field | Type | Set by | Purpose |
|-------|------|--------|---------|
| `validated_input` | `Optional[str]` | `ValidateInputNode` | Sanitized inquiry text |
| `pii_detected` | `Optional[bool]` | `ValidateInputNode` | Whether personal identifiers were found and stripped |
| `channel` | `Optional[str]` | `ValidateInputNode` | Caller channel label (audit only) |
| `caller_routing_table_json` | `Optional[str]` | `ValidateInputNode` | Validated caller department table, JSON |
| `caller_tuning_json` | `Optional[str]` | `ValidateInputNode` | Validated caller tuning overrides, JSON |
| `intent_category` | `Optional[str]` | `ClassifyIntentNode` | permit_inquiry / tax_question / social_welfare / public_safety / infrastructure / complaint / other |
| `intent_confidence` | `Optional[float]` | `ClassifyIntentNode` | Measured score, 0.0–1.0 |
| `urgency_level` | `Optional[str]` | `AssignUrgencyNode` | high / medium / low |
| `department_assignment` | `Optional[str]` | `RouteToDepartmentNode` | Target department key |
| `routing_metadata` | `Optional[dict]` | `RouteToDepartmentNode` | department, sla_hours, escalation_path, contact_key, urgency_level, intent_category |
| `routing_table_json` | `Optional[str]` | inner `_extra_initial_state()` | Effective department table, JSON |
| `min_confidence` | `Optional[float]` | inner `_extra_initial_state()` | Effective confidence bar |
| `urgency_factors_json` | `Optional[str]` | inner `_extra_initial_state()` | Effective deadline multipliers, JSON |
| `formatted_output` | `Optional[str]` | `SecurityGateOutputNode` | Final caller-facing decision string |
| `audit_entry` | `Optional[dict]` | `SecurityGateOutputNode` | Audit record — no inquiry text |
| `error_reason` | `Optional[str]` | `SecurityGateOutputNode` | Closed-set refusal reason (`output_withheld`); the only value `Graph.get_output()` projects into `error` |
| `error_code` | `Optional[str]` | `ValidateInputNode` | Reason code for a request declined over a correctable value; makes every later node pass through untouched. Internal only — never projected into the envelope |

**Inherited from `AgentState`:** `user_input`, `input_context`, `status`, `session_id`,
`node_history`, `error_log`, `result`, `response_metadata`, `trace_id`, `correlation_id`,
`schema_version`, `hitl_*`.

State constraints:

- Flat TypedDict only — primitives and JSON-serializable values.
- Dict/list values are stored as JSON strings (`to_json` / `from_json`); LangGraph
  checkpoints are msgpack-serialised and cannot round-trip arbitrary objects.
- No credentials, no `InvocationContext`, no Pydantic models or dataclasses.
- `pii_detected` is a plain bool — the identifiers themselves are never stored.

---

## Node contracts

### ValidateInputNode (`src/nodes/validate_input_node.py`)

**Slot:** `pre_process` (outer backbone) — **Trust:** `VERIFIED_EXTERNAL`

```
Input:  state["user_input"], state["input_context"]
Output: {"validated_input": str, "pii_detected": bool, "status": AgentStatus,
         "channel"?: str, "caller_routing_table_json"?: str, "caller_tuning_json"?: str}
```

1. Reject a non-object `input_context`, a channel label that is not an inert identifier, or
   one that carries a personal identifier.
2. Reject empty / whitespace-only input, and input longer than 4000 characters.
3. Validate the caller department table and the tuning overrides against the bounds above,
   including the personal-identifier scan on every label.
4. Detect and strip personal identifiers — individual number (a 12-digit sequence), postal
   code (`〒NNN-NNNN`), domestic phone number — replacing each with `[PII_REDACTED]` and
   setting `pii_detected`.

The identifiers are never stored anywhere in State; only the boolean flag is.

Every rejection this node can reach is a value the caller can correct, so it declines the
request without terminating the run — see the next section.

### Declining a request the caller can correct

A rejection at ingest and a failure the agent cannot get past are different outcomes, and the
template reports them differently.

**Correctable by the caller — the run COMPLETES.** Empty or whitespace-only input, input over
4000 characters, a non-object `input_context`, an out-of-contract channel label, department
table or tuning override: none of these is carried out. Nothing is classified, no urgency is
assigned, no department is chosen, and the same audit event is emitted as before. What changes
is only how the outcome is reported — `ValidateInputNode` returns `status=SUCCESS` carrying a
reason code in `error_code`, and `SecurityGateOutputNode` renders the fixed sentence for that
code as the caller-facing body. The caller can correct the value and send the inquiry again on
the same conversation, instead of receiving a terminated run whose reason is reachable only
from the audit trail.

| `error_code` | Condition |
|---|---|
| `EMPTY_INPUT` | inquiry empty, whitespace-only, or absent |
| `QUESTION_TOO_LONG` | inquiry over 4000 characters |
| `INVALID_REQUEST` | any other ingest rejection — `input_context` shape, channel label, department table, tuning override, personal identifier in a label |

**Not correctable by rewording — the run TERMINATES with `status=ERROR`.** The output
boundary's refusal (`SecurityGateOutputNode`, recorded as `error_reason=output_withheld`) and
an upstream contract break reaching an inner node — such as `ClassifyIntentNode` receiving an
empty inquiry that ingest should have stopped — both end the run as an error. These are not
caller-fixable values, so they are not softened into a completed run.

`error_code` is an internal routing value, not a caller-facing vocabulary: it is a State field
and `Graph.get_output()` never projects it. The caller reads the reason as the body only — a
fixed sentence from `src/services/failure_message.py` naming WHAT to correct, never the
rejected value, a field path, or a validator's wording; those stay in `error_log`.

**Marker propagation.** Once `error_code` is set, every later node returns immediately without
doing work — otherwise the pipeline would keep running past the rejection and reach routing.
`GovInquiryWorkflowGraphNode.execute()` skips the inner graph entirely; the inner `get_output()`
carries `error_code` across the graph boundary, and `merge_output()` prefers a reason settled in
the outer graph over one from the inner run, so a specific reason is never overwritten by a
vaguer one. `Graph.get_output()` releases the sentence and nothing else: the structured decision
keys are absent from the envelope, because a run that did not carry out the request produced no
decision to publish.

### ClassifyIntentNode (`src/nodes/classify_intent_node.py`)

**Slot:** inner `classify_intent` — **Trust:** `ANONYMOUS`

```
Input:  state["user_input"] (the sanitized inquiry), state["min_confidence"]
Output: {"intent_category": str, "intent_confidence": float, "status": AgentStatus}
```

Keyword-rule classification into 7 categories:

| Category | Keyword signals |
|---|---|
| `public_safety` | 犯罪, 緊急, 警察, 事件, 事故, 危険, 不審 |
| `permit_inquiry` | 許可, 申請, 建築, 届出, 手続き, 認可, 免許 |
| `tax_question` | 税, 課税, 申告, 控除, 納税, 確定申告, 住民税, 固定資産税 |
| `social_welfare` | 福祉, 介護, 生活保護, 障害, 支援, 手当, 保育 |
| `infrastructure` | 道路, 水道, 下水, 公共施設, 公園, 街灯, ごみ |
| `complaint` | 苦情, クレーム, 不満, 問題, 困っている, おかしい, 改善 |
| `other` | fallback when nothing matches |

Confidence = 0.50 baseline + 0.15 per keyword hit, capped at 0.95. Ties are broken by the
order above, so a public-safety signal always wins. A best category scoring below the
effective confidence bar is filed as `other`; the reported confidence stays the measured
score, so the audit record shows why the inquiry went to the general desk.

### AssignUrgencyNode (`src/nodes/assign_urgency_node.py`)

**Slot:** inner `assign_urgency` — **Trust:** `ANONYMOUS`

```
Input:  state["intent_category"], state["user_input"]
Output: {"urgency_level": str, "status": AgentStatus}
```

- `high` — an urgent keyword (緊急, 至急, 今すぐ, 危険, 事件, 事故, 助けて) or the
  `public_safety` category
- `medium` — a medium keyword (早急, できるだけ早く, なるべく早く, 急ぎ) or the
  `complaint` / `infrastructure` / `social_welfare` categories
- `low` — everything else

Keyword signals are checked first, so an urgent permit request is not filed as routine.

### RouteToDepartmentNode (`src/nodes/route_to_department_node.py`)

**Slot:** inner `route_to_department` — **Trust:** `ANONYMOUS`

```
Input:  state["intent_category"], state["urgency_level"],
        state["routing_table_json"], state["urgency_factors_json"]
Output: {"department_assignment": str, "routing_metadata": dict, "status": AgentStatus}
```

The department directory is resolved by `DepartmentDirectoryService`
(`src/services/service.py`), which normalises every entry so a partial deployment table
cannot leave an inquiry unrouted. Precedence: validated caller table → `routing.table` in
`config/config.yaml` → the built-in directory:

| Category | Department | Standard deadline (hours) |
|----------|-----------|---------------------------|
| permit_inquiry | urban_planning_dept | 48 |
| tax_question | taxation_dept | 24 |
| social_welfare | welfare_dept | 12 |
| public_safety | public_safety_dept | 2 |
| infrastructure | public_works_dept | 24 |
| complaint | citizen_relations_dept | 8 |
| other | general_inquiry_dept | 72 |

The urgency multipliers then apply — `high` halves the deadline by default, never below one
hour — and a high-urgency inquiry always carries an escalation path, falling back to
`department_head` when the directory entry declares none.

### SecurityGateOutputNode (`src/nodes/security_gate_output_node.py`)

**Slot:** `post_process` (outer backbone) — **Trust:** `ANONYMOUS`

```
Input:  state["intent_category"], state["urgency_level"], state["department_assignment"],
        state["routing_metadata"], state["pii_detected"]
Output: {"formatted_output": str, "result": str, "audit_entry": dict, "status": AgentStatus}
```

The rendered decision has one shape:

```
Category: <category> | Urgency: <level> | Department: <dept> | SLA: <n>h[ | Escalation: <path>][ | Note: …]
```

See the next section for how that shape is enforced. The audit record carries the session and
trace identifiers, the channel label, the decision and the confidence — and never the inquiry
text. `emit_trace_event` is called inline in `execute()`, not as an
`_extra_security_gate_output()` instance method: the framework wraps a method of that name,
and a wrapped hook returning `None` is passed on as the next node's state.

---

## The output boundary

The stated invariant is narrow and absolute: **the caller-facing output carries the routing
decision and nothing else** — no inquiry text, no internal state, no free text of any origin.
Three independent layers enforce it, each with its own audit event.

| Layer | Rule | On violation |
|---|---|---|
| 1. Rendered-token grammar | Every rendered field is an inert identifier `[a-z0-9_]{1,48}`; the deadline is a whole number of hours in 1..720 | ERROR, nothing published, the field named but never the value |
| 2. Verbatim-embedding scan | The inquiry text held in State must not appear verbatim in the assembled string | The embedding is replaced with `[REDACTED]` and audited |
| 3. Disallowed-pattern scan | No credential-like material (API keys, JWTs, bearer tokens, secret assignments) and none of the personal-identifier patterns the ingest stage strips | ERROR, nothing published |

Layer 1 is an allow-list rather than a list of forbidden tokens on purpose: a forbidden-token
list only catches the representations someone thought of, while a grammar covers every
representation by construction. Layers 2 and 3 stay independent of it so that a value the
grammar happens to accept is still caught.

The boundary is not switchable. There is no configuration flag that disables it, because a
routing agent whose output boundary can be turned off is a different product.

The three layers apply to an ASSEMBLED decision — the path that renders category, urgency,
department and deadline out of State. The body returned for a declined request is outside them,
deliberately: it is a fixed module constant, built from no State field and no caller value, and
it carries no decision, no label and no identifier. There is nothing for the grammar to
constrain, nothing for the embedding scan to find and nothing for the pattern scan to strip —
so it is returned directly, and only an assembled decision reaches the layers.

`Graph.get_output()` surfaces the structured decision on the `invoke()` return, but only the
post-boundary values, and only when the status is SUCCESS and the run actually carried the
request out. On any non-success outcome the envelope carries closed-set labels only: `output`
is `null`, every structured field is `null`, and `error` is `{"reason": …}` with the reason
drawn from a set this template declares — `output_withheld` when the boundary above refused the
response (recorded in State as `error_reason`), `workflow_failed` for every other non-success
outcome (a trust denial, an inner-workflow error, a timeout — the backbone routes all of them
straight to finalize).

A request DECLINED over a correctable value is a third shape, and neither of the two above. It
completed, so it is not an error envelope; it produced no decision, so it is not a success
envelope either. `Graph.get_output()` detects it by the `error_code` marker and returns the
base envelope unchanged: `output` is the sentence saying what to correct, and the structured
decision keys are simply ABSENT rather than present-and-null — there was never a decision to
withhold. No `error` key is added, because nothing failed: the caller can fix the value and
send the inquiry again.

`error_log` is never projected: it is the internal channel the state reducer
appends to and the audit trail reads, and it carries node- and framework-authored text,
including a wrapped exception's message and traceback. Not publishing it is the contract;
truncating or redacting it would not be.

---

## File layout

```
src/
  api/
    server.py                         — standalone HTTP adapter (Bearer auth, size cap)
  graph/
    graph.py                          — outer AgentBaseGraph + GovInquiryWorkflowGraphNode
    domain_workflow_graph.py          — inner BaseGraph (classify → urgency → route)
    context_bridge.py                 — caller-settings hand-off outer → inner
  nodes/
    validate_input_node.py            — pre_process (VERIFIED_EXTERNAL)
    classify_intent_node.py           — inner: classify into 7 categories
    assign_urgency_node.py            — inner: rules-based urgency
    route_to_department_node.py       — inner: department routing
    security_gate_output_node.py      — post_process (output boundary + audit)
  schemas/
    state.py                          — State(AgentState), flat TypedDict + parsers
  services/
    service.py                        — DepartmentDirectoryService (directory lookup)
config/
  agent.yaml                          — static manifest
  config.yaml                         — runtime parameters
docs/
  02_design.md                        — this file
  03_test_spec.md                     — test specification
```

---

## References

- `src/examples/graph_cat2_sample.py` — the GraphNode / outer graph pattern
- `src/examples/domain_workflow_graph_sample.py` — the inner BaseGraph pattern
