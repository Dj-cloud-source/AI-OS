# Tool Contract Specification

Version: MVP v1

Status: Design only; no Tool may be registered from this prose alone

---

# Purpose

A Tool is a small, typed capability exposed through the Tool Gateway.

This specification defines the minimum contract that a Tool must satisfy before
it can be registered. It does not authorize execution and does not describe a
command pass-through interface.

A Tool:

- performs one bounded operation
- accepts only schema-validated arguments
- returns a structured result
- declares its side effects and target scope
- exposes deterministic metadata for Policy
- can be tested and replayed without production access

A Tool never plans, approves, changes Policy, or expands its own permissions.

---

# Security Boundary

Only the Executor may invoke a registered Tool.

Every invocation remains subject to:

```text
Planner
→ Policy
→ Approval Decision (`NOT_REQUIRED` or valid Human Execution Approval)
→ Executor
→ Tool Gateway
→ Tool
→ Structured Tool Result
→ Runtime
→ Verifier
→ Runtime Outcome
```

Tool contracts, Skills, models, fixtures, and replay data must not contain or
accept arbitrary Shell commands, scripts, command templates, or executable
payloads.

A Tool contract must never contain:

- credentials
- private keys
- passwords
- Tokens
- raw Secrets
- unredacted environment variables
- production connection details in replay fixtures

---

# Contract Identity

Every contract must define:

- `contract_schema_version`
- `schema_dialect`
- stable `tool_id`
- immutable semantic `version`
- immutable `implementation_hash`
- human-readable `description`

Execution Plans must reference the exact `tool_id` and `version`.

The pair `(tool_id, version)` is immutable. Any behavioral, implementation,
schema, risk,
redaction, target-scope, verification, or rollback change requires a new Tool
version. A contract hash must be calculated from a canonical representation
when it is registered.

Mutable Registry Status is stored outside the immutable Contract. Only a Tool
whose separate Registry Record has `status: registered` may be invoked. A
`design_only` example is documentation and has no execution capability.

---

# Required Contract Fields

The machine-readable Tool contract must contain the following fields.

| Field | Requirement |
| --- | --- |
| `contract_schema_version` | Version of this Tool contract schema |
| `schema_dialect` | Exact schema dialect used by input and output schemas |
| `tool_id` | Stable Tool identifier |
| `version` | Immutable semantic Tool version |
| `implementation_hash` | Hash of the exact reviewed implementation artifact |
| `description` | Bounded operation described without executable commands |
| `risk_level` | Authoritative `L0`, `L1`, `L2`, or `L3` Tool metadata |
| `approval` | Deterministic implication of the authoritative risk level |
| `side_effects` | Mutation class and whether the Tool changes remote state |
| `target_scope` | Target type, selector schema, and scope restrictions |
| `input_schema` | Strict input schema with unknown fields rejected |
| `output_schema` | Structured success and failure result schema |
| `redaction` | Fields and data classes that must never be recorded |
| `errors` | Stable structured error codes |
| `timeout_ms` | Maximum invocation duration |
| `idempotent` | Whether retrying has no additional externally visible effect |
| `automatic_retry` | Whether automatic retry is permitted |
| `verification` | Independent verification requirements |
| `rollback` | Rollback requirements and boundary |
| `replay_fixtures` | Sanitized recorded or mock fixtures |

Unknown contract fields must be rejected unless a later contract schema version
explicitly defines them.

---

# Normative Artifacts Required Before Registration

This document defines design requirements, not the complete machine-readable
meta-schema. Before any Tool can become `registered`, Phase 2 must add, review,
and test versioned local artifacts equivalent to:

```text
schemas/tool-contract-v1.json
schemas/tool-result-v1.json
schemas/tool-replay-fixture-v1.json
schemas/tool-registry-record-v1.json
schemas/tool-implementation-bundle-v1.json
```

The schemas must publish stable local `$id` values, use JSON Schema Draft
2020-12, reject unknown fields, define every nested object and enum, and encode
cross-field constraints where JSON Schema can express them. Deterministic
Runtime validators must enforce remaining invariants.

Until all five schemas exist, every Tool and example remains `design_only` and
must fail registration closed.

Contract Hash is SHA-256 over UTF-8 RFC 8785 canonical JSON produced after
strict schema validation. It excludes Registry Status and the Hash field itself.
The Registry Record binds Tool ID, Version, Contract Hash, Implementation Hash,
Status, Reviewer, and timestamps without rewriting the immutable Contract.

`implementation_hash` is SHA-256 over a sealed
`tool-implementation-bundle-v1` manifest, not over an arbitrary working
directory. Its normative Schema requires exactly:

```json
{
  "artifact_format": "tool-implementation-bundle-v1",
  "tool_id": "stable-tool-id",
  "version": "1.0.0",
  "runtime_abi": "python-3.12",
  "dependency_lock_sha256": "sha256:<lowercase-hex>",
  "files": [
    {
      "path": "normalized/relative/path.py",
      "size_bytes": 1,
      "sha256": "sha256:<lowercase-hex>"
    }
  ]
}
```

All fields shown are required Hash inputs; unknown fields are rejected.
`files` contains only files the Tool loads and is sorted lexicographically by
normalized POSIX `path`. The manifest contains no timestamps, absolute paths,
caches, symlinks, or Hash field. Undeclared files, path traversal, duplicate
paths, and mutable external code are rejected. The final
`implementation_hash` is SHA-256 over the UTF-8 RFC 8785 canonical JSON
manifest. Registration must prove that the installed executable bundle
recomputes to the same Hash.

---

# Risk and Approval

Risk level belongs only to Tool metadata.

The model, Planner, Skill, Memory, Evolution Engine, and Tool implementation
must never infer, lower, or override it. Policy resolves the exact registered
Tool version and reads its risk level directly from the Tool Registry.

The `approval` field is a deterministic, system-derived projection of
`risk_level`, not an author-controlled permission field. Registration rejects
any mismatch.

The minimum approval implications are:

| Risk | Approval implication |
| --- | --- |
| `L0` | Policy may allow automatic execution |
| `L1` | Policy-controlled execution |
| `L2` | Explicit human Approval / Commit |
| `L3` | Explicit human Approval / Commit plus immediate Manual Confirmation |

The immediate L3 confirmation occurs at the execution boundary and expires if
execution does not begin within the Policy-configured confirmation window.
It is single-use and binds the Execution Approval ID, Plan Hash, exact Tool
Step, concrete Arguments, and expiration. Replay or reuse is forbidden.

The Effective Plan Risk is at least the maximum authoritative risk among its
ordered Tool Steps. Policy may raise the requirement for target scope or
combined side effects, but cannot lower it. The current MVP rejects L3
execution until this confirmation protocol is implemented and tested.

An execution approval binds:

- the exact Plan Hash
- the concrete Tool Arguments
- the Expiration

Any plan or argument change invalidates approval. Expired approval is invalid.
A Skill approval or Tool registration never substitutes for execution
approval.

Plan canonicalization and Hash coverage follow `ARCHITECTURE.md`. Contract or
Implementation Hash drift invalidates both the Plan and its approval.

---

# Input Contract

For MVP v1, `input_schema` and `output_schema` use JSON Schema Draft 2020-12.
The declared `schema_dialect` must match that version.

The input schema must:

- define every accepted field and type
- reject unknown fields
- constrain identifiers, lengths, enumerations, and numeric ranges
- identify the exact target selector
- reject command strings and executable content
- reject credentials and Secrets unless a future separately approved design
  explicitly permits a credential reference

Tools receive validated values, not free-form instructions.

Target expansion is forbidden. A Tool must operate only on the target resolved
from its approved arguments and declared target scope.

Canonical Arguments Hash is:

```text
sha256(UTF8(RFC8785(exact_validated_arguments_object)))
```

All schema defaults must be materialized into the Execution Plan before Policy,
Approval, and hashing. The Hash includes every property in the validated input
object, including its target selector when the contract places that selector
inside `arguments`; it excludes only outer invocation-envelope fields such as
Invocation ID, timestamps, result data, and the Hash field itself. Runtime or
transport layers must not inject credentials, defaults, or hidden execution
parameters after hashing. The object dispatched to the Tool must be canonically
equivalent to the object bound by the Plan and this Hash.

---

# Output and Error Contract

Every invocation returns a structured object for both success and failure.
Plain strings are forbidden.

The result must identify:

- `invocation_id` and `plan_step_id`
- `tool_id`
- `tool_version`
- Contract Hash and canonical Arguments Hash
- structured opaque Target Reference
- success status
- bounded execution duration
- structured evidence
- structured error, when present

Errors must contain a stable code, category, sanitized message, and retry
classification. They must not expose raw remote output, stack traces,
credentials, Secrets, or unrelated target data.

For every result, `success: true` requires `error: null`, and `success: false`
requires a structured non-null `error`.

Tool Gateway, not the model or Tool text, supplies and validates the trusted
result envelope: Invocation ID, Plan Step ID, Contract Hash, canonical Arguments
Hash, and structured Target Reference. Runtime rejects a result that does not
match the approved invocation. It also deterministically checks
`duration_ms <= timeout_ms`; this cross-document invariant is not delegated to
model output.

Unknown failures must map to a safe generic structured error. They must not
silently become success.

---

# Side Effects and Target Scope

Every Tool must declare:

- whether it reads or mutates state
- the type of side effect
- the resource type it can target
- whether it can affect one or multiple resources
- the constraints used to prevent target expansion

Policy validates declared scope against the concrete approved arguments.

A Tool must fail closed when:

- its target cannot be resolved exactly
- the target exceeds approved scope
- its registered contract is unavailable
- its contract hash does not match
- its input does not match the registered schema

---

# Redaction

Redaction is part of the Tool contract, not an optional logging feature.

The contract must declare:

- a versioned central Redaction Profile reference
- input fields that must never be logged
- output fields that must be removed or summarized
- evidence fields that are safe to persist
- maximum retained payload size

Logs, Tool Results, fixtures, Incident Memory, and Evolution inputs must use the
same redaction rules. Failure to redact causes the Tool Result to fail closed
and prevents the unsafe payload from entering replay or Memory. Unsafe raw data
must be discarded through the approved local handling path and must not appear
in debug logs or exception messages.

---

# Verification

Verification is independent from Tool execution.

The Tool Contract declares expected evidence and exact registered read-only
Tool references. Planner includes every verification Step, Tool Version,
concrete Argument, and expected condition in the Execution Plan before Policy
and Approval. The executing Tool must not declare its operation successful
solely because the invocation returned.

Verification:

- uses structured evidence
- uses only Plan Steps executed by Executor through Tool Gateway
- remains subject to Policy and the same Plan Hash
- never uses arbitrary Shell
- cannot be removed by a Skill

Verifier never invokes a Tool. Runtime asks Executor to run the pre-approved
verification Steps during `VERIFYING`, validates their result bindings, and
then passes the structured results to Verifier. An unplanned evidence request
ends the current attempt as `FAILED` with a stable expansion reason and creates
a linked new attempt with a new Plan and, when required, a new Human Execution
Approval. Runtime never jumps from `VERIFYING` or `EXECUTING` back to
`PLANNING`.

A failed or inconclusive Verification must not be reported as completed
success.

---

# Rollback

A mutating Tool must declare whether rollback is required and how the previous
state is represented.

Rollback guidance:

- never executes inline as a hidden side effect
- produces a separate explicit Execution Plan
- is re-evaluated by Policy
- receives its own human approval when its Tool risk requires it
- uses registered Tools only
- is verified independently

If rollback is unavailable, the contract must say so explicitly and declare
the required manual intervention. A model or Skill must not invent a rollback
capability.

---

# Recorded and Mock Replay

Every Tool must provide sanitized recorded or mock fixtures for tests and
Historical Replay.

A replay fixture contains:

- exact `tool_id` and `version`
- sanitized input
- canonical Arguments Hash and invocation sequence position
- structured Tool Result
- structured Verification Result, when applicable
- expected error code or outcome
- fixture provenance class: `recorded_sanitized` or `mock`
- fixture schema version and content hash
- Redaction Policy Version and sanitized redaction report

Replay:

- never opens SSH, Docker, HTTP, database, or other production connections
- runs with external network access technically disabled
- never invokes the production Tool implementation
- uses recorded results, Mock Tools, or local fixtures only
- rejects fixtures containing Secrets or executable content
- fails closed on contract, version, schema, or hash mismatch
- matches exact Tool ID, Version, canonical Arguments Hash, and invocation order
- records Redaction Policy Version and a sanitized redaction report

Replay success is evaluation evidence. It is not production authorization.

---

# Registration Gates

A Tool may become `registered` only after:

- normative Contract, Result, Fixture, and Registry Record schemas exist
- strict contract-schema validation
- unique identity and version validation
- Contract Hash and Implementation Hash binding
- Tool implementation review
- input and output schema tests
- Policy tests for its authoritative risk level
- target-scope and side-effect tests
- redaction and Secret scanning
- structured error tests
- Mock and sanitized replay tests
- verification and rollback review
- arbitrary Shell and executable-payload scanning
- explicit human registration

Any failed gate leaves the Tool unavailable to Planner, Policy, Executor,
Skills, and production retrieval.

---

# Design Example: `restart_service`

The following YAML is a design example. It is not a registered Tool and must
not connect to a server. It may become executable only after a future
implementation satisfies every registration gate.

A separate Registry Record would mark this example `design_only`; that mutable
record is not part of the YAML below. `implementation_hash: null` intentionally
makes this design-only example impossible to register.

```yaml
contract_schema_version: "1"
schema_dialect: "https://json-schema.org/draft/2020-12/schema"
tool_id: restart_service
version: 1.0.0
implementation_hash: null
description: Restart one allowlisted systemd service.

risk_level: L2
approval:
  derived_from_risk_level: true
  implication: explicit_human_approval
  binds:
    - plan_hash
    - arguments
    - expiration

side_effects:
  mutates_remote_state: true
  kind: service_state_change

target_scope:
  resource_type: systemd_service
  maximum_targets: 1
  selector_field: service_name

input_schema:
  type: object
  additionalProperties: false
  required:
    - service_name
  properties:
    service_name:
      type: string
      minLength: 1
      maxLength: 128
      pattern: "^[A-Za-z0-9][A-Za-z0-9_.@-]*$"

output_schema:
  type: object
  additionalProperties: false
  required:
    - invocation_id
    - plan_step_id
    - tool_id
    - tool_version
    - contract_hash
    - arguments_hash
    - target
    - success
    - previous_state
    - observed_state
    - duration_ms
    - evidence
    - error
  properties:
    invocation_id:
      type: string
      format: uuid
    plan_step_id:
      type: string
      minLength: 1
      maxLength: 128
    tool_id:
      const: restart_service
    tool_version:
      const: 1.0.0
    contract_hash:
      type: string
      pattern: "^[a-f0-9]{64}$"
    arguments_hash:
      type: string
      pattern: "^[a-f0-9]{64}$"
    target:
      type: object
      additionalProperties: false
      required:
        - target_id
        - resource_type
        - resource_id
      properties:
        target_id:
          type: string
          minLength: 1
          maxLength: 128
        resource_type:
          const: systemd_service
        resource_id:
          type: string
          minLength: 1
          maxLength: 128
    success:
      type: boolean
    previous_state:
      enum: [active, inactive, failed, unknown]
    observed_state:
      enum: [active, inactive, failed, unknown]
    duration_ms:
      type: integer
      minimum: 0
    evidence:
      type: object
      additionalProperties: false
      required:
        - transition_observed
      properties:
        transition_observed:
          type: boolean
    error:
      oneOf:
        - type: "null"
        - type: object
          additionalProperties: false
          required:
            - code
            - category
            - message
            - retryable
          properties:
            code:
              enum:
                - target_not_allowed
                - service_not_found
                - operation_timeout
                - operation_failed
                - result_redaction_failed
            category:
              enum:
                - policy_boundary
                - target
                - timeout
                - execution
                - safety
            message:
              type: string
              maxLength: 256
            retryable:
              const: false
  allOf:
    - if:
        properties:
          success:
            const: true
        required:
          - success
      then:
        properties:
          error:
            type: "null"
      else:
        properties:
          error:
            type: object

redaction:
  profile_ref: tool-redaction-default@1
  never_record:
    - credentials
    - private_keys
    - passwords
    - tokens
    - raw_environment
  persist_allowlist:
    - target
    - success
    - previous_state
    - observed_state
    - duration_ms
    - evidence.transition_observed
    - error.code
    - error.category
    - error.retryable
  maximum_persisted_payload_bytes: 16384

errors:
  - code: target_not_allowed
    category: policy_boundary
    retryable: false
  - code: service_not_found
    category: target
    retryable: false
  - code: operation_timeout
    category: timeout
    retryable: false
  - code: operation_failed
    category: execution
    retryable: false
  - code: result_redaction_failed
    category: safety
    retryable: false

timeout_ms: 30000
idempotent: false
automatic_retry: forbidden

verification:
  required: true
  tool_ref: get_service_status@1.0.0
  expected:
    state: active

rollback:
  available: false
  reason: A completed restart cannot be undone.
  recovery_if_verification_fails:
    separate_plan_required: true
    policy_recheck_required: true
    manual_intervention_if_no_registered_recovery_tool: true

replay_fixtures:
  - fixtures/restart_service/success.mock.yaml
  - fixtures/restart_service/timeout.mock.yaml
  - fixtures/restart_service/target-not-allowed.mock.yaml
```

The referenced verification Tool and replay fixture paths are also design-only
until separately registered and created. Their appearance in this example does
not imply that they exist.

---

# Failure Handling

Tool contract, registration, invocation, verification, and replay failures
follow:

```text
Fail Closed
Do Not Execute
Do Not Guess
Preserve Sanitized Evidence
Require Human Intervention When Safety Is Uncertain
```

No failure may cause a risk downgrade, approval bypass, target expansion,
automatic dangerous retry, or fallback to arbitrary Shell.

---

# Acceptance Criteria

This specification is satisfied when:

- normative Contract, Result, Fixture, and Registry Record schemas exist
- every registered Tool has an immutable exact identity and contract hash
- Registry binds the reviewed Implementation Hash without mutating the Contract
- input, output, errors, side effects, target scope, and redaction are
  machine-validated
- Policy obtains risk only from exact registered Tool metadata
- L2 and L3 approval implications are enforced consistently
- approval is bound to exact Plan Hash, Arguments, and Expiration
- all results and errors are structured and sanitized
- Verification and rollback remain explicit and independently governed
- Verifier consumes Runtime-provided results and never invokes Tool
- replay uses only sanitized recorded data, Mock Tools, or local fixtures
- replay cannot connect to production
- arbitrary Shell and executable payloads are rejected
- design-only examples cannot be invoked

---

# Out of Scope

This document does not implement:

- Tool code
- SSH, Docker, Kubernetes, HTTP, or database connections
- production registration
- Executor behavior
- Policy code
- Approval code
- Verifier code
- replay infrastructure
- Skill Registry
- Evolution Engine
- arbitrary command templates
