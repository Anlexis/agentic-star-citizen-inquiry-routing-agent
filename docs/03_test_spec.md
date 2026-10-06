# Test Specification — GOV-C2-003

## Strategy

- **Unit tests** (`tests/unit/`) exercise each node's domain logic in isolation, calling
  `node.execute(state)` directly.
- **Boundary tests** (`tests/proof_of_boundary/`) exercise the contracts that only hold when
  the pieces are assembled: the trust boundary, the invoke order, the state schema, the HTTP
  adapter, and the full path from `POST /invoke` through both graphs and back.
- **Coverage target:** 80%+ of `src/nodes/` and `src/graph/`.
- **No stubbing of `shared.*`.** CI installs the real framework wheel; a test that replaces
  `sys.modules["shared"]` would pass against a fiction.
- **Audit events are muted at the node module**, never through `sys.modules` —
  `monkeypatch.setattr("src.nodes.<module>.emit_trace_event", …)` as an autouse fixture.
- When asserting on an audit spy, inspect `call.args[1]` (the payload). `repr(call_args_list)`
  includes `call.args[2]`, the state, which legitimately still holds the inquiry text.

The three properties worth stating outright, because most cases below exist to hold them:

1. **Every caller input is bounded, inert and fail-closed.** A rejected value never becomes a
   silently ignored value, and never appears in the response.
2. **Two rejection classes, asserted differently.** Every ingest rejection is a value the caller
   can correct, so it **declines** the request instead of terminating the run: `ValidateInputNode`
   returns `status=SUCCESS` with a reason code in `error_code`, publishes no `validated_input`,
   and the output node renders the fixed reason sentence as the body. "declined" in the tables
   below means exactly that, and those cases are asserted on `status=SUCCESS` **together with**
   the absence of any product of the request (no `validated_input`, no decision field) — a bare
   status assertion would not distinguish a decline from a normal run. The output boundary's own
   refusal and an upstream contract break reaching an inner node still TERMINATE with
   `status=ERROR`, and those cases are asserted as errors.
3. **The output carries the routing decision and nothing else** — enforced for every
   representation, not only the convenient ones.

---

## Unit test cases — ValidateInputNode

`tests/unit/test_validate_input_node.py`

| TC-ID | Input | Expected |
|-------|-------|----------|
| GOV-TC-001 | `""` | declined (correctable): `status=SUCCESS`, `error_log` names "empty or missing" |
| GOV-TC-002 | `"   "` | declined (correctable): `status=SUCCESS` |
| GOV-TC-002b | no `user_input` key | declined (correctable): `status=SUCCESS` |
| GOV-TC-003 | 4001 characters | declined (correctable): `status=SUCCESS`, `error_log` names the 4000-character bound |
| GOV-TC-003b | exactly 4000 characters | `status=SUCCESS` (inclusive boundary) |
| GOV-TC-004 | `"住民税の申告について教えてください"` | `status=SUCCESS`, `pii_detected=False`, text unchanged |
| GOV-TC-005 | contains `123456789012` (individual number) | `pii_detected=True`, `[PII_REDACTED]` in place of the number |
| GOV-TC-006 | contains `〒123-4567` | `pii_detected=True`, the code stripped |
| GOV-TC-007 | contains `090-1234-5678` | `pii_detected=True`, the number stripped |
| GOV-TC-008 | leading/trailing whitespace | `validated_input` trimmed |

---

## Unit test cases — the caller-data contract

`tests/unit/test_caller_data_contract.py` — the `input_context` bounds in
`docs/02_design.md`. Each numeric row is parametrized over the full non-finite matrix
(`NaN`, `Infinity`, `-Infinity`, numeric strings, bools, `None`, lists).

| TC-ID | Case | Expected |
|-------|------|----------|
| GOV-TC-070 | no `input_context` | `status=SUCCESS`, no caller keys written |
| GOV-TC-071 | valid department table | normalised table stored in `caller_routing_table_json` |
| GOV-TC-072 | entry with unknown extra keys | the extra keys are dropped, not stored |
| GOV-TC-073 | valid tuning overrides | stored in `caller_tuning_json` |
| GOV-TC-074 | valid channel label | recorded in `channel`, never rendered |
| GOV-TC-075 | `sla_hours` = 1 and 720 | accepted (inclusive bounds) |
| GOV-TC-076 | non-object `input_context` | declined: `error_log` names `input_context` |
| GOV-TC-077 | channel with spaces / hyphen / >32 chars / non-string | declined: `error_log` names `channel` |
| GOV-TC-077b | a channel value carrying a phone number / postal code / individual number | declined; the value never echoed — the context channel is locked to an inert identifier, so no context field accepts free text |
| GOV-TC-078 | table that is a list / empty / >12 entries | declined: `error_log` names `routing_table` |
| GOV-TC-079 | non-inert category key | declined: `error_log` names `routing_table` |
| GOV-TC-080 | entry missing `department` / `sla_hours` / `contact_key` | declined: `error_log` names the field |
| GOV-TC-081 | non-inert `department` or `escalation_path` | declined: `error_log` names the field |
| GOV-TC-081b | a table label or category key that is itself a personal identifier | declined: `error_log` names the field; the value never echoed |
| GOV-TC-082 | `sla_hours` 0 / -5 / 721 / 24.5 / bool / string / None / NaN / Infinity | declined: `error_log` names `sla_hours` |
| GOV-TC-083 | non-finite or out-of-range `min_confidence` | declined: `error_log` names `min_confidence` |
| GOV-TC-084 | non-finite or out-of-range `high_sla_factor` / `medium_sla_factor` | declined: `error_log` names the field |
| GOV-TC-085 | non-object `classification` / `urgency` block | declined: `error_log` names the block |
| GOV-TC-086 | rejected value that looks like a credential | the value never appears in `error_log` |
| GOV-TC-087 | malformed table with a valid inquiry | rejected before any `validated_input` is produced |

---

## Unit test cases — ClassifyIntentNode

`tests/unit/test_classify_intent_node.py`

| TC-ID | Input | Expected category | Confidence |
|-------|-------|-------------------|-----------|
| GOV-TC-010 | `"住民税の申告について教えてください"` | `tax_question` | ≥ 0.5 |
| GOV-TC-010b | `"税の申告と課税の控除について知りたい"` | `tax_question` | > 0.5 (multiple hits) |
| GOV-TC-011 | `"建築許可の申請方法を教えてください"` | `permit_inquiry` | ≥ 0.5 |
| GOV-TC-012 | `"障害者福祉の相談がしたい"` | `social_welfare` | ≥ 0.5 |
| GOV-TC-013 | `"緊急事態が発生した"` | `public_safety` | ≥ 0.5 |
| GOV-TC-014 | `"道路に穴が開いている"` | `infrastructure` | ≥ 0.5 |
| GOV-TC-015 | `"サービスへの苦情があります"` | `complaint` | ≥ 0.5 |
| GOV-TC-016 | `"hello world how are you"` | `other` | 0.5 baseline |
| GOV-TC-017 | `""` | `status=ERROR` — an upstream contract break, not a caller-correctable value: ingest declines an empty inquiry before this node, so reaching it empty means the pipeline is wrong | — |
| GOV-TC-018 | 税 + 警察 together | `public_safety` (highest-priority rule wins) | ≥ 0.5 |
| GOV-TC-019 | many tax keywords | any | ≤ 0.95 ceiling |

GOV-TC-012 deliberately uses an unambiguous welfare inquiry. `介護保険の手続きについて` scores
equally for `permit_inquiry` (手続き) and `social_welfare` (介護), which makes the outcome
rule-order-dependent rather than a statement about the classifier.

---

## Unit test cases — AssignUrgencyNode

`tests/unit/test_assign_urgency_node.py`

| TC-ID | Category | Inquiry | Expected urgency |
|-------|----------|---------|------------------|
| GOV-TC-020 | `public_safety` | any | `high` |
| GOV-TC-021 | `permit_inquiry` | `"至急手続きしたい"` | `high` (keyword override) |
| GOV-TC-022 | `complaint` | no urgency keyword | `medium` |
| GOV-TC-023 | `infrastructure` | no urgency keyword | `medium` |
| GOV-TC-024 | `social_welfare` | no urgency keyword | `medium` |
| GOV-TC-025 | `permit_inquiry` | no urgency keyword | `low` |
| GOV-TC-026 | `tax_question` | no urgency keyword | `low` |
| GOV-TC-027 | `other` | no urgency keyword | `low` |
| GOV-TC-028 | `tax_question` | `"早急に対応してください"` | `medium` (keyword override) |
| GOV-TC-029 | `other` | `"緊急です"` / `"危険な状況です"` | `high` |

---

## Unit test cases — RouteToDepartmentNode

`tests/unit/test_route_to_department_node.py`

| TC-ID | Category | Urgency | Expected department | Deadline |
|-------|----------|---------|---------------------|----------|
| GOV-TC-030 | `permit_inquiry` | `low` | `urban_planning_dept` | 48h |
| GOV-TC-031 | `tax_question` | `medium` | `taxation_dept` | 24h |
| GOV-TC-032 | `social_welfare` | `medium` | `welfare_dept` | 12h |
| GOV-TC-033 | `public_safety` | `high` | `public_safety_dept` | 1h (halved from 2h) |
| GOV-TC-034 | `infrastructure` | `low` | `public_works_dept` | 24h |
| GOV-TC-035 | `complaint` | `low` | `citizen_relations_dept` | 8h |
| GOV-TC-036 | `other` | `low` | `general_inquiry_dept` | 72h |
| GOV-TC-037 | `permit_inquiry` | `high` | `urban_planning_dept` | 24h; escalation set |
| GOV-TC-038 | unknown category | `low` | `general_inquiry_dept` (catch-all) | 72h |
| GOV-TC-039 | `tax_question` + seeded table | `low` | the table's department | the table's deadline |
| GOV-TC-039b | corrupt `routing_table_json` | `low` | built-in directory applies, no failure | 24h |
| GOV-TC-040 | any | any | `routing_metadata` carries department, sla_hours, urgency_level, intent_category, and stays JSON-serializable |

---

## Unit test cases — SecurityGateOutputNode

`tests/unit/test_security_gate_output_node.py`

| TC-ID | State | Expected |
|-------|-------|----------|
| GOV-TC-050 | a clean routing decision | `status=SUCCESS`; the decision fields render; `SLA: 24h` present |
| GOV-TC-050b | any success path | `formatted_output` and `result` are the same post-boundary value |
| GOV-TC-051 | `pii_detected=True` | the output says personal information was removed, naming nothing |
| GOV-TC-052 | high urgency with an escalation path | the path renders |
| GOV-TC-052b | no escalation path | no escalation section at all |
| GOV-TC-053 | all fields present | `audit_entry` carries session, decision, deadline and contact key; no `user_input` |
| GOV-TC-054 | inquiry text in state | the inquiry never appears in `formatted_output` |
| GOV-TC-055 | audit spy | no payload contains the inquiry text |
| GOV-TC-056 | a rendered field that is not an inert identifier (inquiry text, spaces, uppercase, empty, `None`, a number, >48 chars) | `status=ERROR`; nothing published; the field named |
| GOV-TC-057 | `sla_hours` 0 / -1 / 721 / 24.5 / bool / string / `None` / `NaN` / `Infinity` | `status=ERROR`; nothing published |
| GOV-TC-058 | non-inert `escalation_path` | `status=ERROR` naming `escalation_path` |
| GOV-TC-059 | missing `routing_metadata` | `status=ERROR`; nothing published |
| GOV-TC-060 | a rejected value that looks like a credential | the value appears in neither the error nor the audit payload |
| GOV-TC-061 | `sla_hours` 1 / 24 / 720 | renders byte-identically at both ends of the range |
| GOV-TC-062 | any success path | `execute()` returns exactly `formatted_output`, `result`, `audit_entry`, `status` |

---

## Unit test cases — trust boundary and runtime config

| File | What it holds |
|------|---------------|
| `tests/unit/test_trust_gate.py` | An ANONYMOUS caller is denied at `ValidateInputNode` and `execute()` never runs; VERIFIED_EXTERNAL and INTERNAL pass; a missing trust level reads as ANONYMOUS; the inner and output nodes admit ANONYMOUS; the output refusal comes from this node's own grammar, not the framework scan; the declared trust matrix matches `docs/02_design.md` |
| `tests/unit/test_runtime_config.py` | `config/config.yaml` parses and declares the expected keys; `_parent_config()` forwards them; a missing or malformed file degrades to `{}`; a tightened confidence bar, a changed deadline multiplier and a replaced department table each flip an observable outcome through a full compiled invoke; a partial table entry degrades to the catch-all values; a caller override beats the configured value |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06 / TC-07 — replacing the framework's default input or output gate raises at class definition |
| `tests/unit/test_error_envelope_closed_set.py` | The caller-visible ERROR envelope is a closed set. `Graph.get_output()` on every non-success shape (a run carrying `status=ERROR` into finalize, a timeout, a boundary refusal, a foreign or non-string value in the reason slot, a surviving pre-gate `result` / `formatted_output` / structured field) returns `output: null`, `error: {"reason": …}` drawn from `ERROR_REASONS`, no `error_log` key and a fixed key set; a runtime-assembled sentinel seeded into `error_log` — alongside trust-denial and wrapped-exception entries of the framework's shape — appears nowhere in the returned mapping, walking nested values; the success envelope is unchanged and a stale reason cannot turn it into an error. `SecurityGateOutputNode`'s refusal delta, on every layer that can refuse and through both `execute()` and the framework pipeline, records `error_reason=output_withheld`, publishes no `formatted_output` / `result` / `audit_entry`, writes exactly one line of its own and re-emits nothing; the refusal audit event carries the reason and no value |

---

## Boundary tests (`tests/proof_of_boundary/`)

| PB-ID | File | Assertion |
|-------|------|-----------|
| PB-1 | `test_import_isolation.py` | No platform-SDK import anywhere in `src/` |
| PB-2 / PB-5 | `test_state_safety.py` | `State` carries no credential-like field names and no prohibited types |
| PB-6 | `test_pb_invoke_order.py` | Every node runs trust → node_start → input gate → `execute()` → output gate → node_complete; a full `Graph().invoke()` visits initialize → pre_process → main → post_process → finalize and returns a decision without the inquiry text |
| PB-7 | `test_pb7_hitl_interrupt_propagation.py` | Skip stub — this template registers no interrupt checkpoint |
| PB-8 | `test_server_boot.py` | The adapter builds; the Bearer boundary rejects a missing / wrong / malformed / empty / non-ASCII token with a generic 401; a correct token elevates to VERIFIED_EXTERNAL; an unconfigured or empty token never grants trust; middleware-established trust is never demoted |
| PB-9 | `test_invoke_e2e.py` | End-to-end through the real ASGI `/invoke` — see below |

### PB-9 — end-to-end through `POST /invoke`

| Case | Assertion |
|------|-----------|
| Plain inquiry | A real category, urgency, department, deadline and contact key come back |
| Caller department table | The routed department changes, which also proves the caller settings crossed the outer→inner boundary |
| Urgent inquiry | High-urgency path: deadline halved to 1h, escalation path set |
| Unclassified inquiry | The general desk, at the low-urgency deadline |
| Caller confidence bar | A tightened bar reroutes to the general desk |
| Caller deadline multiplier | The deadline changes accordingly |
| Inquiry with a phone number | Stripped before anything is decided; the number appears nowhere in the raw response |
| Malformed caller table | Declined, not terminated: `status=success`, `output` is the reason sentence, no `error_log` key, and `department_assignment` is absent — no routing decision was made. Neither the rejected value nor the field-level wording appears in the raw response |
| Non-finite `min_confidence` / `high_sla_factor` (as strings and as raw JSON `NaN`/`Infinity`) | Declined with no decision: `status=success`, `output` is the reason sentence and `department_assignment` is absent — never a success that publishes a routing decision with the check silently disabled |
| Oversized `input_context` | `413` from the adapter, before the graph runs |
| Missing Bearer token | `401` |
| Output scan | The response renders the documented decision shape only — no inquiry text, no internal state, no channel label, and it passes the same disallowed-pattern scan the boundary enforces |
| Caller-data rejection carrying a sentinel | The ingest validator is made to author a runtime-assembled sentinel (a name, an email, a token-shaped fragment) into its rejection line; the body is `status=success` carrying only the reason sentence, no `error_log`, every structured decision key absent, and the sentinel appears nowhere in the body, walking nested values. A separate case confirms the sentinel does reach the node's own `error_log` — the internal channel keeps it, the envelope does not |
| Inner-node exception | An inner node raises with the sentinel as its message; the framework wraps message + traceback into `error_log` (inner wrapper, then the outer wrapper around the re-raised `SubgraphError`); the body carries `workflow_failed` and neither the message, `Traceback`, `File "` nor the exception class |
| Output-boundary refusal | The boundary's grammar check is made to name the sentinel as the refused field; the body carries `error: {"reason": "output_withheld"}` and nothing from the refusal line |
| Trust denial | No token configured, no middleware: the framework's denial line stays internal; the body carries `workflow_failed` and no "trust gate denied" |
| Verify the verifier | The replaced validator does put the sentinel into the ingest node's own `error_log`, and the same walk finds it there |
