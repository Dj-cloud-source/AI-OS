# Tool Contract Specification

Version: MVP v1

Status: Phases 2 and 5 implemented; the default Registry remains local
Mock-only, and this prose alone never registers a Tool

---

# Purpose

A Tool is a small, typed capability exposed through the Tool Gateway.

This specification defines the minimum contract that a Tool must satisfy before
it can be registered. Phase 2 permits only the package-resident, reviewed,
deterministic `get_system_status@1.0.0` Mock Tool to bootstrap as `registered`.
It performs no real system I/O. This document does not authorize another Tool,
production execution, or a command pass-through interface.

A Tool:

- performs one bounded operation
- accepts only schema-validated arguments
- returns a structured result
- declares its side effects and target scope
- exposes deterministic metadata for Policy
- can be tested and replayed without production access

A Tool never plans, approves, changes Policy, or expands its own permissions.

Phase 2 is synchronous and local. It contains no SSH, LLM/model adapter, shell,
subprocess, Docker, Kubernetes, HTTP, database, network, credential, remote
target, or mutating capability.

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
schema, risk, redaction, target-scope, verification, or rollback change requires
a new Tool version. A Contract Hash must be calculated from the exact validated
Contract artifact before registration.

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
| `output_schema` | Complete Gateway-owned ToolResult success/failure envelope Schema |
| `redaction` | Fields and data classes that must never be recorded |
| `errors` | Stable structured error codes |
| `timeout_ms` | Maximum invocation duration |
| `idempotent` | Whether retrying has no additional externally visible effect |
| `automatic_retry` | Whether automatic retry is permitted |
| `verification` | Required structured evidence plus exact registered read-only verification Tool references; an empty Tool list has the self-evidence meaning defined below |
| `rollback` | Rollback requirements and boundary |
| `replay_fixtures` | Sanitized recorded or mock fixtures |

Unknown contract fields must be rejected unless a later contract schema version
explicitly defines them.

---

# Normative Artifacts Required Before Registration

The five normative package Schemas are:

```text
src/ai_server/schemas/tool/tool-contract-v1.json
src/ai_server/schemas/tool/tool-result-v1.json
src/ai_server/schemas/tool/tool-replay-fixture-v1.json
src/ai_server/schemas/tool/tool-registry-record-v1.json
src/ai_server/schemas/tool/tool-implementation-bundle-v1.json
```

They are installed as package resources under `ai_server.schemas.tool`. All
five must load together, publish their expected unique local `$id`, use JSON
Schema Draft 2020-12, reject unknown fields, define nested objects and enums,
and pass meta-schema validation. Deterministic Pydantic and Runtime validators
enforce invariants that JSON Schema cannot express.

Each exact Tool artifact set is package-resident at:

```text
src/ai_server/tool_artifacts/<tool_id>/<version>/
├── contract.json
├── registry-record.json
├── implementation-bundle.json
├── dependency-lock.json
└── fixtures/
    └── <fixture>.json
```

Missing package data, a missing Schema, an unexpected `$id`, or any artifact
validation failure leaves the Tool unavailable.

## Digest Encoding and Canonical Inputs

Protocol identity fields use lowercase, unprefixed 64-character hexadecimal:

- Contract Hash in Registry Records, Plans, ToolCalls, ToolResults, and replay
  fixtures;
- `implementation_hash` in Contracts and Registry Records;
- canonical Arguments Hash;
- replay fixture `content_hash`.

File- and lock-level byte digests use lowercase
`sha256:<64-character-hexadecimal>`:

- `dependency_lock_sha256`;
- every Implementation Bundle `files[].sha256`;
- every dependency artifact `packages[].artifacts[].sha256`.

The algorithms are:

1. Contract Hash: parse `contract.json` as one JSON object; validate the raw
   object against `tool-contract-v1`; encode that complete raw object as UTF-8
   RFC 8785 canonical JSON; then apply SHA-256. `contract.json` has no
   `contract_hash` field, and its `implementation_hash` is included.
2. Implementation Hash: validate the complete
   `implementation-bundle.json`; encode the entire manifest as UTF-8 RFC 8785
   canonical JSON; then apply SHA-256. The manifest has no
   `implementation_hash` or volatile timestamp field.
3. Arguments Hash: materialize all schema defaults before Planning, Policy,
   Approval, or hashing; encode the complete validated arguments object as
   UTF-8 RFC 8785 canonical JSON; then apply SHA-256.
4. Fixture Content Hash: parse and validate the fixture, remove only the root
   `content_hash` member, encode the remaining object as UTF-8 RFC 8785
   canonical JSON, then apply SHA-256.

Contract and Implementation Hashes are returned as unprefixed lowercase
digests. File and dependency-lock byte hashes retain the `sha256:` prefix.
Serialization through a Pydantic model must not replace the raw JSON artifact
as the Contract, Implementation, or Fixture hash input.

The Registry Record stores mutable availability and review evidence separately.
It binds Tool ID, Version, Contract Hash, Implementation Hash, status, reviewer,
and UTC timestamps without rewriting the immutable Contract.

## Implementation Bundle and Executable Binding

`implementation_hash` is SHA-256 over a sealed
`tool-implementation-bundle-v1` manifest, not over an arbitrary working
directory. Its identity and binding fields are:

```json
{
  "artifact_format": "tool-implementation-bundle-v1",
  "tool_id": "stable-tool-id",
  "version": "1.0.0",
  "runtime_abi": "python-source-v1.requires-python-ge-3.12",
  "handler_entry_point": "ai_server.tools.example:ExampleTool.invoke",
  "input_model_entry_point": "ai_server.models.example:ExampleArguments",
  "output_model_entry_point": "ai_server.models.example:ExamplePayload",
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

All fields shown are required Hash inputs; unknown fields are rejected. Entry
points use `module:qualified.name`. At startup, the qualified entry points
derived from the bound Python handler, input model, and output model must
exactly match the manifest. Registry then resolves each reviewed entry point
from the installed package and requires exact Python object identity for both
models and the handler function. A bound method must also be bound to an
instance whose exact type is the reviewed owner class. Caller-controlled
`__module__` or `__qualname__` strings are never sufficient authority. Each
entry point's module source file must also appear in `files`. A caller-supplied
object cannot select or impersonate a different reviewed handler or model.

`files` is sorted lexicographically by normalized package-relative POSIX path.
Each entry binds the installed package file's exact byte size and prefixed
SHA-256 digest. The manifest contains no timestamps, absolute paths, caches,
symlinks, or Hash field. Undeclared entry-point files, path traversal, duplicate
paths, symlinks, missing files, byte drift, and mutable external code are
rejected.

## Reviewed Dependency Lock

Every Phase 2 Tool artifact set contains `dependency-lock.json`. Its
`format` is exactly `uv-tool-lock-v1`, its `requires_python` is exactly
`>=3.12`, and its top-level fields are exactly:

```text
format
source_lock
requires_python
roots
packages
```

`source_lock` records positive `format_version` and `revision` integers from
the repository-local `uv` lock source. `roots` is a non-empty sorted unique
list. `packages` is a sorted unique dependency closure by name and version.
Every dependency name must exist in that closure. Phase 2 accepts only the
reviewed PyPI simple-index identity and `files.pythonhosted.org` artifact URLs;
each listed artifact binds an exact filename, positive byte size, and prefixed
SHA-256 digest. This lock is registration evidence only: Runtime never
downloads or installs dependencies while registering or invoking a Tool.

The Implementation Bundle binds the exact raw `dependency-lock.json` bytes in
both `dependency_lock_sha256` and its declared `files` entry. Any lock drift
invalidates the Implementation Hash and registration. The only accepted Phase
2 runtime ABI is:

```text
python-source-v1.requires-python-ge-3.12
```

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

`L0` and `L1` are read-only classes. Their Contract must declare
`mutates_remote_state: false`; Contract validation and Registry registration
both reject a low-risk mutating declaration, including a bypass-constructed
model. `L2` and `L3` may still describe read-only operations, but that never
lowers their approval requirement.

The immediate L3 confirmation occurs at the execution boundary and expires if
execution does not begin within the Policy-configured confirmation window.
It is single-use. Its Challenge Hash covers the complete strict
`ManualConfirmationChallenge` except `challenge_hash` itself: Schema Version,
Authorization Hash, Approval ID, Approval Plan Hash, Approval Record Hash,
Approval Expiration, Execution Attempt ID, Invocation ID, Step Index/ID/Role,
Tool ID/Version, Contract Hash, Implementation Hash, Arguments Hash, and Target
Reference. The issued record additionally has a short expiration. CLI accepts only exact
`CONFIRM <challenge-hash>`. Replay or reuse is forbidden. Confirmation is an
invocation gate, not a Runtime state. The bundled CLI is the only supported
Phase 5 production adapter and requires interactive input and output TTYs.
Runtime/Executor reader injection is a trusted process-local test seam, not
proof of human provenance, and must not be connected to a model, Skill, Tool,
pipe, or environment value. The default Registry has no L3 Tool. Before a
production L3 Tool is registered, an independently reviewed design must bind
Confirmation to a verifiable interactive source; otherwise production L3
remains unavailable.

The reviewed local Policy Profile v1.1 uses Profile Schema v2 to provide
bounded Approval configuration: Review Session and Plan Approval TTLs are at
most 300 seconds, and the L3 Manual Confirmation TTL is at most 30 seconds.
Planner, model output, CLI flags, and environment variables cannot increase
these values.

The Effective Plan Risk is at least the maximum authoritative risk among its
ordered Tool Steps. Policy may raise the requirement for target scope or
combined side effects, but cannot lower it. Phase 4 implemented the
confirmation record and consumption protocol without dispatch and therefore
kept a fixed L3 denial. Phase 5 connects confirmation consumption to the exact
invocation boundary. The default Registry remains L0-only; removing the
temporary framework denial neither registers nor authorizes an L3 Tool.

For Phase 5, Plan Approval consumption is process-local and thread-safe when
Executor creates the exact attempt; Runtime enters `EXECUTING` only after that
linearization. A crash before dispatch still burns the Approval. For an L3
invocation, Confirmation consumption is additionally followed immediately by
dispatch of the already-bound ToolCall in the same Executor call stack. No
consumed authority can be restored or reused for an automatic retry. This is
not transactional atomicity with a remote side effect.

An execution approval binds:

- the exact Plan Hash
- the concrete ordered Tool Arguments and their canonical Hash commitments
- exact Tool identity, Contract Hash, and Implementation Hash
- target, Registry risk, target scope, side effects, redaction, Verification,
  and rollback requirements
- the complete ordered Plan-level `verification_criteria`, including expected
  values, evidence Step binding, evaluator version, and freshness limit
- the applicable Policy Decision Hash and approval requirements
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

`target_scope.selector_field` must name a required property in
`input_schema`. Executor, not Planner, the Tool, or Gateway, deterministically
constructs the `TargetReference` from the already validated and approved Plan.
For the Phase 2 single-target Mock protocol:

- `TargetReference.resource_type` must equal the registered
  `target_scope.resource_type`;
- the validated selector value must be a string;
- `TargetReference.target_id` and `TargetReference.resource_id` must both equal
  that selector value;
- `maximum_targets` must be `1`;
- Gateway performs no remote lookup or target discovery.

Any mismatch produces a structured `target_not_allowed` failure after exact
Tool resolution and before handler dispatch. That failure preserves the
strictly typed attempted Target Reference for audit; the exact Contract output
Schema therefore accepts a structurally valid Target Reference on failure but
requires the registered allowed Target on success.

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

The handler boundary and result boundary are intentionally different:

1. A Tool handler receives one already validated typed arguments model.
2. The handler returns only one typed Tool-specific `data` payload model.
3. The handler does not return Invocation ID, Plan Step ID, hashes, Target,
   success, evidence, error, or duration.
4. Gateway validates the payload model, creates the complete ToolResult
   envelope, derives safe evidence, and validates the final object.

The global `tool-result-v1` Schema and every Contract `output_schema` both
describe the complete ToolResult envelope, not only `data`. The Contract Schema
may narrow Tool identity, payload, evidence, target, duration, and allowed
errors, but it must contain exactly these top-level fields:

```text
invocation_id
plan_step_id
tool_id
tool_version
contract_hash
arguments_hash
target
success
data
evidence
error
duration_ms
```

Gateway validates the complete result first against the global Result Schema
and then against the exact registered Contract output Schema. Before retaining
any receipt or report result, Executor and Runtime independently resolve the
same frozen Registry entry and revalidate exact identity, arguments, target,
payload model, Contract output Schema, declared error, timeout, forbidden
content, redaction, and evidence projection. A failed revalidation discards the
result and fails closed. Retained `evidence` JSON containers are deeply
immutable; serialization returns fresh ordinary JSON objects and arrays.

For every structured result:

- `success: true` requires typed non-null `data` and `error: null`;
- `success: false` requires `data: null`, empty evidence, and one non-null
  structured error;
- `duration_ms` is a non-negative integer no greater than the registered
  timeout;
- plain strings, raw exceptions, stack traces, callbacks, transport objects,
  credentials, Secrets, and unrelated target data are forbidden.

Every error contains a stable code, category, contract-supplied sanitized
message, and retry classification. The eight Phase 2 Gateway invocation
failures are:

```text
arguments_hash_mismatch
gateway_clock_failed
invalid_arguments
malformed_tool_output
result_redaction_failed
target_not_allowed
tool_execution_failed
tool_timeout
```

Every registered Contract must declare all eight with the required category and
`retryable: false`, and its output Schema must accept a full failure envelope
for each. A Contract may add bounded Tool-specific errors. Gateway never
accepts a Tool-provided error message as trusted envelope content.

## Exception Versus ToolResult Boundary

A ToolResult is created only after Gateway has established a trustworthy exact
registered Tool identity and verified the ToolCall's Contract and
Implementation Hashes.

| Failure boundary | Representation | Handler dispatch |
| --- | --- | --- |
| Invalid Gateway construction | Sanitized `invalid_gateway_configuration` exception | Zero |
| Malformed or untrusted ToolCall | Sanitized `invalid_tool_call` exception | Zero |
| Unknown Tool or unsafe Registry resolution | Sanitized `tool_resolution` exception | Zero |
| Contract or Implementation Hash mismatch | Sanitized `tool_integrity` exception | Zero |
| Valid exact identity followed by arguments, target, hash, clock, handler, timeout, output, or redaction failure | Structured ToolResult using a declared Gateway error | At most one; pre-dispatch checks remain zero |

These exceptions carry stable codes and generic sanitized messages only.
Executor catches them and converts them into its explicit Runtime domain
failure; Gateway does not fabricate a result envelope from an untrusted call.
No boundary failure may silently become success or authorize a retry.

## Internal Dispatch Receipt

Phase 5 adds a Gateway-produced, process-local internal receipt that records
whether the private typed handler boundary was entered. It exists only so
Executor can construct a truthful ExecutionReport. It is not part of Tool
Protocol v1 or ToolResult v1, is not returned by a Tool, and does not change any
Contract Schema, Contract Hash, or public result field. Executor strictly
validates its shape; a missing or untrusted receipt is handled conservatively
as unknown rather than inferred as non-dispatch.

ExecutionReport is cumulative. After Runtime accepts an
`AWAITING_VERIFICATION_DISPATCH` report, every verification or abort report
must preserve its exact records and events as an unchanged prefix and must add
closure progress. A shorter, rewritten, or replayed report is rejected; it
cannot erase earlier handler-dispatch evidence.

The receipt supports these conservative facts:

| Handler boundary | Dispatch certainty | Effect certainty |
| --- | --- | --- |
| Proven not entered | `NOT_DISPATCHED` | `NONE` |
| Entered for a read-only Tool | `HANDLER_DISPATCHED` | `NONE` |
| Entered for a mutating Tool and returned success | `HANDLER_DISPATCHED` | `PENDING_VERIFICATION` |
| Entered for a mutating Tool but the outcome is uncertain | `HANDLER_DISPATCHED` | `UNKNOWN` |

Entering a handler does not prove that a side effect occurred or succeeded.
When Gateway cannot prove a failure happened before handler entry, Executor
must not report `NOT_DISPATCHED`. An `UNKNOWN` mutating effect fails closed and
requires human intervention. If Executor cannot obtain a trustworthy receipt
at all, both dispatch and effect are `UNKNOWN`; this is an Executor fallback,
not a value emitted by the receipt.

## Attempt Closure Uncertainty

An internal receipt supports per-invocation facts only after Executor produces
a fully revalidated ExecutionReport. If the final report is missing or
untrusted and Runtime cannot confirm that the attempt was safely aborted,
Runtime emits a separate hash-bound `ExecutionUncertainty` rather than
fabricating Tool protocol evidence.

- It binds the exact execution-attempt authorization and optionally the Hash of
  the last trusted `AWAITING_VERIFICATION_DISPATCH` report.
- Before any dispatch-capable Executor call, failed closure is represented as
  `NOT_DISPATCHED` / `NONE` without invented Step or Invocation fields.
- After a dispatch-capable call, missing trustworthy closure is represented as
  `UNKNOWN` / `UNKNOWN` with `human_intervention_required=true`.
- The stable reason is `execution_abort_uncertain`; this never authorizes retry
  or redispatch.
- `execution_uncertainty_audit` contains attempt-level IDs, authorization and
  evidence Hashes, certainty, reason, and verification status. Per-Step Tool,
  Target, result, and duration fields appear only when backed by a trusted
  ExecutionReport.

`ExecutionUncertainty` is a Runtime/Executor evidence contract, not ToolResult
v1 and not a new receipt value. It does not alter Tool Contract Schemas or
Contract Hashes.

## Phase 2 Synchronous Timeout

Phase 2 invokes only the deterministic local Mock handler synchronously. Gateway
uses a monotonic nanosecond clock around the handler call. It compares raw
elapsed nanoseconds with `timeout_ms`; an overrun produces `tool_timeout` and
takes precedence over a handler exception. Public failure duration remains
bounded by the registered timeout.

This is a post-return elapsed-time check. It does not interrupt, cancel, kill,
isolate, or preempt a running handler and is not a network or transport
timeout. Phase 2 has no automatic retry.

Phase 5 preserves Gateway as the authority for this current synchronous
single-invocation timeout. Executor records monotonic elapsed time for the
whole plan, treats a `tool_timeout` result as terminal for the attempt, and
never automatically retries it. Plan elapsed-time recording does not add
preemption, cancellation, or a transport timeout. Real cancellation and
transport timeouts require a later transport-specific approved design.

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
- output fields that must not be retained
- evidence fields that are safe to persist
- maximum retained payload size

In MVP v1, `input_fields`, `output_fields`, and `safe_evidence_fields` contain
top-level property names only. Dotted paths do not mean nested traversal.
Nested field-path redaction requires a later versioned Redaction Profile and
Schema change. The generic forbidden-key and forbidden-marker scan remains
recursive. Artifact validation and Gateway use the same centrally defined
forbidden key and executable-marker set, comparing string markers
case-insensitively so the two safety boundaries cannot silently diverge.

Gateway applies the Phase 2 output boundary in this exact order:

1. require the handler return value to be the exact registered payload model;
2. strictly reconstruct and serialize that payload to a JSON object;
3. reject the payload if any declared top-level `output_fields` property is
   present;
4. recursively scan keys and string values for forbidden Secret or executable
   markers;
5. RFC 8785-canonicalize the complete validated payload and reject it when its
   byte length exceeds `max_retained_payload_bytes`;
6. construct `evidence` by selecting only present top-level
   `safe_evidence_fields`;
7. construct the Gateway-owned ToolResult envelope;
8. validate that full envelope against the global Tool Result Schema and exact
   Contract output Schema;
9. only then return or retain the structured result.

Phase 2 does not transform or partially preserve a payload that contains a
declared output field or forbidden content. It discards that payload and
returns a safe `result_redaction_failed` ToolResult with `data: null` and empty
evidence. The maximum byte limit applies to the complete validated payload
before envelope construction, not only to the evidence projection.

Declared top-level `input_fields` are prohibited from logs, diagnostics, and
persisted invocation summaries. Phase 2 audit events retain only the constant
`{"redacted": true}` marker in the `arguments` field and emit no raw Tool
argument values. A future logging phase must apply the same profile before
retaining any safe argument projection.

Tool Results, fixtures, Incident Memory, and Evolution inputs must use the same
versioned redaction rules. Unsafe raw data must not appear in debug logs,
exception messages, replay, or Memory.

---

# Verification

The Verification decision is independent from Tool execution. A read-only Tool
may still supply its own Contract-valid result as evidence under the explicit
self-evidence rule below; this does not let the Tool decide the verdict.

The Tool Contract declares expected evidence and exact registered read-only
Tool references. `verification.tools` has one precise meaning:

- for a read-only Tool, an empty list means its own successful, structured,
  Contract-valid ToolResult may provide verification evidence;
- a non-empty list requires a matching, criterion-bound `VERIFY` Step for every
  listed Tool reference; required references cannot be partially covered;
- a mutating Tool must declare at least one independent read-only verification
  Tool; its own Action result is never sufficient to close the side effect.

Every referenced verification Tool must itself have
`mutates_remote_state=false`. A `VERIFY` Step must also be non-L3 even when an
L3 Tool is read-only. Contract registration and Plan validation reject a
mutating or L3 verification Tool, an undeclared reference, a duplicate
reference, an unbound required reference, or a mutating Contract without
independent verification. A Skill, Planner, or model cannot weaken these
rules.

`verification.evidence_fields` is the minimum safe evidence projection the
Contract requires Runtime to retain; it is not an open-ended field selector
and is not a list of every typed `data` field. A criterion with
`source=evidence` may use only a field present in this exact list. A typed-data
criterion may use only the closed fields implemented for the exact registered
output model, and those reads still pass output Schema and redaction checks.
No criterion can use this distinction to retain an otherwise redacted field.

Every ExecutionPlan v2 contains one or more ordered Plan-level
`verification_criteria`; every criterion is mandatory in MVP v1. Planner
includes every Contract-required verification Step, exact Tool Version,
concrete Arguments, and criterion before Policy and Approval. The Approval
Snapshot v2 Hash binds the complete criteria and their order. A v1 Plan or
Approval Snapshot cannot be used at this boundary.
The terminal CLI Review displays each criterion's complete exact serialized
definition, not only a human summary, so the reviewer can inspect the same
content that the Snapshot Hash binds.

Verification:

- uses structured evidence
- uses only Plan Steps executed by Executor through Tool Gateway
- remains subject to Policy and the same Plan Hash
- never uses arbitrary Shell
- cannot be removed by a Skill
- never treats a Tool-provided timestamp as Runtime evidence acceptance time

Each criterion binds unique `criterion_id`, exact `evidence_step_id`, literal
`mandatory=true`, integer `maximum_age_ms` from 0 through 30000 (default
30000), literal `evaluator_version="1"`, and
exactly one of these four kinds:

| Kind | Required behavior |
| --- | --- |
| `equals` | `source=data|evidence`, `field=source|simulated|target|hostname`, and a strict `str|bool` expected value; no coercion |
| `numeric_bounds` | `field=cpu_percent|memory_percent|disk_percent` with inclusive `minimum` and `maximum`; boolean and non-finite values fail |
| `expected_state` | Exact `service_name` and `expected_state=running|stopped`; a missing or duplicate name fails |
| `health_status` | `expected_status=healthy|unhealthy` plus `maximum_utilization_percent`; healthy means a non-empty set of unique service names, all services running, and all three utilization values at or below the cap |

The four variants are a strict discriminated union with kind-specific expected
fields. There is no generic fifth evaluator, dynamic JSONPath, dotted path,
regex, expression, callback, code hook, or script. Evidence extraction is
statically implemented for the exact Tool ID, Tool Version, Result Schema, and
evaluator version. The listed selectors are closed enums, not paths. Duplicate
service names are contradictory evidence and fail even when the expected
health status is `unhealthy`; with non-contradictory evidence, `unhealthy` is
the negation of the defined healthy predicate. Unknown kinds or versions fail
Plan validation before dispatch.

Plan roles are ordered as an `OBSERVE`/`ACTION` prefix followed by a `VERIFY`
suffix; an execution Step cannot occur after the first `VERIFY` Step. Runtime
asks Executor to run only the prefix in `EXECUTING` and only the suffix in
`VERIFYING`. An L3 Tool is valid only as an `ACTION` Step; L3 `OBSERVE` and
`VERIFY` are rejected before dispatch. A `VERIFY` Step must be Contract-declared
and referenced by a criterion; extra, unused, or duplicate evidence calls are
invalid. Current read-only `get_system_status@1.0.0` declares no verification
Tool and its criteria consume its original result, so Runtime does not invoke
it a second time. Its default mandatory criteria use only the Contract-required
`simulated`, `source`, and `target` projection. Although `hostname` is a
supported safe fixed selector, it is not listed as required by this Contract
and is not a default mandatory criterion.

Every source Step whose Metadata declares `mutates_remote_state=true` must use
the `ACTION` role. Phase 6 permits at most one mutating source Step per Plan.
For that Step, every
required verification Tool reference must have at least one meaningful
postcondition criterion of kind `numeric_bounds`, `expected_state`, or
`health_status`. An `equals` criterion that proves only provenance or target
identity is insufficient to close a remote mutation. Multiple mutating source
Steps remain unavailable until a separately reviewed design binds every action
to its own criteria and effect disposition explicitly.

Verifier never invokes a Tool. Runtime asks Executor to run only pre-approved,
Contract-required verification Steps during `VERIFYING`, validates their
result bindings, and then constructs a strict `VerificationContext`. The
context binds Task, Plan digest, Execution Attempt, cumulative report Hash,
Runtime-owned `evidence_accepted_at`, `collection_duration_ms`, the same
trusted clock sample as `evaluated_at`, and `mutating_effect_pending`.

The strict `VerificationContext` fields are `context_schema_version`,
`task_id`, `plan_id`, `plan_digest`, `execution_attempt_id`,
`execution_report_hash`, `evidence_accepted_at`, `evaluated_at`,
`collection_duration_ms`, and `mutating_effect_pending`. A null acceptance time
exists only to represent Runtime clock unavailability; it always produces
`CLOCK_UNAVAILABLE` and can never pass verification. Context schema version is
`"1"`, duration is an integer from 0 through 3600000, and every non-null time
is exact timezone-aware UTC.

After accepting the fully validated report, Runtime reads its trusted UTC clock
once and uses the same sample for both `evidence_accepted_at` and
`evaluated_at`. The acceptance time is not a remote event timestamp. Runtime
sets the bounded collection duration to:

```text
collection_duration_ms = min(
    max(
        ExecutionReport.total_duration_ms,
        ceil_ms(evidence_accepted_at - Runtime EXECUTING entered_at)
    ),
    3_600_000
)
```

Freshness then uses the conservative lower bound on collection time:

```text
conservative_age_ms =
    evaluated_at - evidence_accepted_at + collection_duration_ms
```

There is no second evaluation-time clock read. Invalid time order, malformed
duration, or age beyond any criterion's hashed `maximum_age_ms` fails closed.

Verifier returns an immutable, canonical-hash-bound `VerificationResult`
binding the same identities. Its strict fields are `result_schema_version`,
`task_id`, `plan_id`, `plan_digest`, `execution_attempt_id`,
`execution_report_hash`, `evaluated_at`, `status`, `checks`,
`evidence_references`, `failure_reasons`, `effect_disposition`,
`human_intervention_required`, and `content_hash`. `VerificationStatus` and
`VerificationCheckStatus` contain only `PASSED | FAILED`;
`VerificationEffectDisposition` contains only `NONE | VERIFIED | UNKNOWN`.
Result schema version is `"1"`.
There is one ordered `VerificationCheckResult` per criterion and all
`VerificationEvidenceReference` values are structured, Hash-only, and
redacted. The closed `VerificationFailureReason` values and meanings are
normative in `ARCHITECTURE.md#layer-9`; no free-string reason is permitted.
Only all-pass mandatory checks can produce `PASSED`. Missing, stale,
malformed, contradictory, unplanned, reordered, target-mismatched, or
identity/hash-mismatched evidence produces `FAILED`.

The content Hash is an integrity commitment, not proof that the verdict is
true. Before calling an injected Verifier, Runtime exact-type rebuilds the
Plan, each ToolResult, and VerificationContext and passes only those isolated
copies; the injected component cannot mutate Runtime's retained authority.
Runtime then exact-type rebuilds the returned result and independently invokes
the trusted pure evaluator functions on its private original context. The
returned result must exactly equal the recomputed result. Reused Invocation
IDs, a Hash-valid forged pass, or any changed identity, check order, evidence
reference, reason, effect, or human flag fails closed.

`VERIFIER_FAILED` is reserved for a Verifier exception or inability to
complete evaluation. `VERIFIER_RESULT_INVALID` is used when a returned object
has an invalid type, structure, binding, order, Hash, effect closure, or differs
from Runtime's independent recomputation. These reasons must remain distinct.

For read-only work, a pass or fail has effect `NONE`. A pending mutating effect
becomes `VERIFIED` only after a valid all-pass result; every other outcome is
`UNKNOWN` with human intervention required. Verifier can only report that
recovery should be considered. It cannot request, initiate, execute, or
authorize recovery or rollback.

If a Contract-required verification Tool fails before Verifier evaluation,
Runtime does not fabricate a VerificationResult. It enters `FAILED`; read-only
work retains effect `NONE`, while every pending mutation becomes `UNKNOWN` and
requires human intervention. A terminal RuntimeOutcome binds the accepted
VerificationResult and Hash, every check's criterion/evidence-Step/evaluator
identity, and every ordered report result through its complete hash-bound
evidence reference.

Runtime emits a structured `verification_audit` for every produced
VerificationResult. It contains only identity and content hashes, status,
stable failure reasons, effect/human closure, check metadata, and
Step/Invocation/result-Hash references. It never records raw evidence, raw
observed values, or unredacted arguments.

An unplanned evidence need ends the current attempt as `FAILED` with a stable
expansion reason. A future phase may permit an explicit new Task and new Plan
which follow the entire lifecycle and, when required, receive a new Human
Execution Approval. Runtime never jumps from `VERIFYING` or `EXECUTING` back to
`PLANNING`, and a failed or inconclusive Verification never produces
`COMPLETED`.

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

- exact `fixture_id` bound to a normalized package-relative Contract reference
- exact `tool_id` and `version`
- sanitized input
- canonical Arguments Hash and invocation sequence position
- structured Tool Result
- structured Verification Result, when applicable
- expected error code or outcome
- fixture provenance class: `recorded_sanitized` or `mock`
- fixture schema version and content hash
- Redaction Policy Version and sanitized redaction report

At registration, every referenced fixture is loaded as package data and
validated in this order:

1. validate the complete fixture against
   `tool-replay-fixture-v1`;
2. match the Contract reference's `fixture_id` and reject duplicate IDs;
3. remove only the root `content_hash`, RFC 8785-canonicalize the remaining
   object, apply SHA-256, and compare the unprefixed lowercase digest;
4. recompute the Arguments Hash from `input`;
5. match Tool ID, Version, Contract Hash, Arguments Hash, structured Target,
   timeout bound, expected outcome, and expected error;
6. validate `input` against the exact Contract input Schema and the complete
   `result` against both global and exact Contract output Schemas;
7. require matching Redaction Profile Version and `sanitized: true`;
8. require successful structured Verification evidence when the Contract says
   Verification is required;
9. recursively reject Secret and executable markers;
10. require fixture sequence positions to be unique and ascending.

Registration also constructs a safe failure envelope for every declared
Contract error and proves that both result Schemas accept it. This prevents a
failure path from becoming unrepresentable only after dispatch.

Phase 2 Mock replay:

- never opens SSH, Docker, HTTP, database, or other production connections
- runs with external network access technically disabled
- never invokes the production Tool implementation
- uses recorded results, Mock Tools, or local fixtures only
- rejects fixtures containing Secrets or executable content
- fails closed on contract, version, schema, or hash mismatch
- matches exact Tool ID, Version, canonical Arguments Hash, and invocation order
- records Redaction Policy Version and a sanitized redaction report

Replay success is evaluation evidence. It is not production authorization.
Phase 2 implements artifact and fixture validation, not production Historical
Replay or traffic simulation.

---

# Registration Gates

A Tool may become `registered` only after:

- all five normative Contract, Result, Fixture, Registry Record, and
  Implementation Bundle Schemas load from the installed package
- strict contract-schema validation
- unique identity and version validation
- Contract Hash and Implementation Hash binding
- reviewed handler, input model, and output model entry points exactly match the
  Implementation Bundle and declared package files
- runtime ABI, dependency-lock byte hash, dependency closure, installed-file
  byte size, and installed-file digest validation
- input and output schema tests
- Policy tests for its authoritative risk level
- target-scope and side-effect tests
- redaction and Secret scanning
- structured error tests
- Mock and sanitized replay tests
- verification and rollback review
- arbitrary Shell and executable-payload scanning
- a package-resident reviewed bootstrap Registry Record
- source distribution, wheel package-resource, and clean local install tests

Any failed gate leaves the Tool unavailable to Planner, Policy, Executor,
Skills, and production retrieval.

## Phase 2 Reviewed Bootstrap Record

Phase 2 does not implement a mutable registry database, approval CLI, reviewer
authentication, or production activation workflow. It accepts only a
package-resident bootstrap `registry-record.json` reviewed as repository
content. The Mock bootstrap validator requires:

- exact Tool ID and Version;
- exact unprefixed Contract and Implementation Hashes;
- `status: registered`;
- `reviewer: local-owner`;
- timezone-aware UTC `reviewed_at` and `registered_at`;
- exact binding to the immutable Contract and Implementation Bundle.

Runtime cannot create, edit, approve, or upgrade this record. Registration is
explicit at process startup; all artifact gates run before insertion. Duplicate
identity fails. Registry must then be frozen before Policy metadata lookup or
Gateway resolution; mutation after freeze fails.

This bootstrap record authorizes only availability of the local deterministic
Mock capability. It is not Execution Approval and cannot authorize remote or
mutating behavior. A general human Tool-review and status-transition workflow
requires a later architecture phase or RFC.

---

# Registered Phase 2 Example: `get_system_status`

The only Phase 2 bootstrap Tool is
`get_system_status@1.0.0`. Its complete machine-readable artifacts are:

```text
src/ai_server/tool_artifacts/get_system_status/1.0.0/contract.json
src/ai_server/tool_artifacts/get_system_status/1.0.0/registry-record.json
src/ai_server/tool_artifacts/get_system_status/1.0.0/implementation-bundle.json
src/ai_server/tool_artifacts/get_system_status/1.0.0/dependency-lock.json
src/ai_server/tool_artifacts/get_system_status/1.0.0/fixtures/success.mock.json
```

Its immutable properties are:

| Property | Value |
| --- | --- |
| Tool identity | `get_system_status@1.0.0` |
| Risk | `L0`, derived approval implication `automatic_execution` |
| Side effects | `mutates_remote_state: false`, kind `none` |
| Target | one `local_system` selected by required argument `target` |
| Allowed selector | exactly `local-mock` |
| Timeout | 1000 ms, synchronous post-return measurement |
| Automatic retry | `false` |
| Verification | required structured Mock evidence; no verification Tool call |
| Rollback | not required |
| Runtime ABI | `python-source-v1.requires-python-ge-3.12` |
| Handler entry point | `ai_server.tools.get_system_status:GetSystemStatusTool.invoke` |
| Input model entry point | `ai_server.models.system_status:GetSystemStatusArguments` |
| Output payload model entry point | `ai_server.models.system_status:SystemStatus` |

Executor creates the typed arguments and Target Reference:

```json
{
  "arguments": {
    "target": "local-mock"
  },
  "target": {
    "target_id": "local-mock",
    "resource_type": "local_system",
    "resource_id": "local-mock"
  }
}
```

The handler returns only deterministic simulated payload data. Gateway wraps it
as a complete result such as the reviewed Mock fixture:

```json
{
  "invocation_id": "00000000-0000-4000-8000-000000000001",
  "plan_step_id": "observe_status",
  "tool_id": "get_system_status",
  "tool_version": "1.0.0",
  "contract_hash": "90e2295d172ba8188d986e4aee9ce9665a8b8e2d6694b8ef4015ef711d820a94",
  "arguments_hash": "5b2b197431ad05295ca97b9dab0e2638a389f2f1d4c2d2c32c7d12409c98dcd0",
  "target": {
    "target_id": "local-mock",
    "resource_type": "local_system",
    "resource_id": "local-mock"
  },
  "success": true,
  "data": {
    "source": "mock",
    "simulated": true,
    "target": "local-mock",
    "hostname": "mock-server",
    "cpu_percent": 12.5,
    "memory_percent": 34.0,
    "disk_percent": 45.5,
    "services": [
      {
        "name": "mock-api",
        "state": "running"
      }
    ]
  },
  "evidence": {
    "source": "mock",
    "simulated": true,
    "target": "local-mock",
    "hostname": "mock-server"
  },
  "error": null,
  "duration_ms": 0
}
```

The artifact, not this duplicated example, is authoritative. Contract or
fixture edits change their hashes and require the Registry Record and all bound
Plans to be regenerated and reviewed. This Tool reads no host state and cannot
be changed into a real status probe under the same version.

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

- all five normative Contract, Result, Replay Fixture, Registry Record, and
  Implementation Bundle Schemas load from the installed package
- every registered Tool has an immutable exact identity, unprefixed Contract
  Hash, and unprefixed Implementation Hash
- Registry derives Metadata from the validated artifact set and binds the
  reviewed Implementation Hash without mutating the Contract
- manifest entry points, runtime ABI, dependency lock, and installed package
  files all match their reviewed bindings
- Registry resolves only after startup freeze and exposes no public handler
- input, output, errors, side effects, target scope, and redaction are
  machine-validated
- Executor deterministically constructs the Target Reference and Gateway rejects
  target expansion before dispatch
- Policy obtains risk only from exact registered Tool metadata
- L2 and L3 approval implications are enforced consistently
- approval is bound to exact Plan Hash, Arguments, and Expiration
- the handler returns payload data only, while Gateway owns and validates the
  complete ToolResult envelope
- pre-trust Gateway failures are explicit sanitized exceptions with zero
  dispatch, and post-resolution invocation failures are structured ToolResults
- the Gateway-internal dispatch receipt reports handler entry for
  ExecutionReport without altering ToolResult v1 or existing Contract Hashes
- all results and errors are structured, bounded, and sanitized in the
  specified order
- Phase 2 timeout is documented and tested as a synchronous post-return
  elapsed-time check without cancellation or retry
- Executor treats `tool_timeout` as terminal, records plan elapsed time, and
  does not claim preemption or transport-timeout behavior
- Plan Approval consumption is process-local and single-use when Executor
  creates the exact attempt, before Runtime enters `EXECUTING`; it is not
  adjacent or atomic with dispatch
- per-L3 Confirmation consumption is process-local, single-use, immediately
  adjacent to the already-bound dispatch, and never described as atomic with a
  remote side effect
- every invocation waits only for the preconditions applicable to that exact
  invocation; later L3 Confirmations are obtained only when their Step is next
- ExecutionPlan contains 1–64 Steps; oversized or bypass-constructed Plans are
  rejected before dispatch
- Step roles are an `OBSERVE`/`ACTION` prefix followed by a `VERIFY` suffix,
  with Runtime invoking each segment only in its matching state
- every `VERIFY` Step is Contract-declared, criterion-bound, read-only, and
  non-L3; every required verification reference is covered
- Phase 6 Plans contain at most one mutating source Step, whose required
  verification references use meaningful postconditions rather than only
  provenance equality
- Verification and rollback remain explicit and independently governed
- Verifier consumes Runtime-provided results and never invokes Tool
- Runtime uses one clock sample for evidence acceptance/evaluation, derives the
  bounded conservative collection duration from both Executor and Runtime
  elapsed evidence, and gives an injected Verifier only isolated rebuilt inputs
- terminal outcomes and `verification_audit` preserve exact hash-bound closure
  without retaining raw verification evidence
- replay uses only sanitized recorded data, Mock Tools, or local fixtures
- Fixture Hash removes only root `content_hash` before RFC 8785 and SHA-256
- replay cannot invoke a production Tool or connect to production
- arbitrary Shell and executable payloads are rejected
- only the reviewed package-resident `get_system_status@1.0.0` Mock bootstrap
  record can register in Phase 2
- design-only, disabled, deprecated, unknown, duplicate, or drifted Tools cannot
  be invoked

---

# Out of Scope

Phase 2 explicitly excludes:

- any real status collection or Tool other than the deterministic local Mock
- SSH, Docker, Kubernetes, HTTP, database, filesystem, subprocess, shell,
  network, credential, remote-target, or mutating operations
- restart service, restart container, deletion, and configuration change
- LLM or model adapters
- dynamic Tool discovery, plugins, arbitrary user Tool installation, or
  runtime artifact download
- mutable Registry persistence, registration CLI, reviewer authentication,
  online status transitions, or production activation
- preemptive cancellation, process isolation, real transport timeout, or
  automatic retry
- production Historical Replay or traffic simulation
- Skill Registry
- Evolution Engine
- arbitrary command templates
