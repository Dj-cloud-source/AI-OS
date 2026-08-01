# AIOps Agent Runtime Implementation Plan

Status: Phases 0–6 implemented; Phases 7–11 planned

## 1. Authority and scope

This plan is subordinate to the following governing documents:

1. `docs/VISION.md`
2. `docs/PHILOSOPHY.md`
3. `docs/ARCHITECTURE.md`
4. `docs/STATE_MACHINE.md`
5. `docs/TOOL_SPEC.md`
6. `AGENTS.md`

Implementation proceeds phase by phase. A later phase must not be used to
justify bypassing an earlier safety gate. Phases 0–3 implement only the local
Mock Runtime, its fail-closed Tool boundary, and deterministic reviewed Policy;
they add no real server capability.

Completed Approval Gate conformance means that every Policy-allowed Plan enters
`WAITING_FOR_APPROVAL`, an audited `NOT_REQUIRED` decision exits immediately,
human-approval-required work pauses without calling Executor, and direct bypass
is rejected. Phase 4 owns only in-process approval issuance, validation,
expiration, invalidation, consumption protocols, and L3 confirmation
contracts. Phase 5 owns the process-local linearized connection from
authorization to Executor; Phase 9 owns persistence and cross-process
resumption.

The decisions below are subordinate implementation guidance. When this plan
and a governing document differ, implementation stops and the governing
document wins; this plan must then be corrected before work resumes.

## 2. Fixed decisions

### 2.1 Project layout

- The project root is the directory containing this `docs` directory.
- Runtime code will use Python 3.12+ and the `src/ai_server` layout.
- Architecture modules live under `ai_server`: `cli`, `runtime`, `context`,
  `planner`, `policy`, `approval`, `executor`, `verifier`, `tools`, `memory`,
  `storage`, and `models`.
- Empty future interfaces are not created. A package is added only when its
  current phase has a concrete responsibility or when Phase 0 needs a package
  marker for the approved skeleton.
- Phase 0 components are the first concrete implementation and are enhanced in
  place. Later phases must not create a parallel "production" Runtime, Tool
  protocol, Policy, Executor, or Verifier.

### 2.2 Dependency and authority rules

```text
CLI
  |
Runtime
  +--> Context Builder (pure)
  +--> Planner (plan only)
  +--> Policy (deterministic)
  +--> Approval (authorization only)
  +--> Executor (the only Tool caller)
  |      |
  |      +--> Tool Gateway --> Tool
  |
  +--> Verifier (pure)
  +--> Memory (facts only)
```

- Runtime is the only application-level entry point.
- Planner never invokes a Tool or transport.
- Policy never invokes an LLM and never trusts model-provided risk.
- Policy receives one frozen Tool Registry and uses only its read-only Metadata
  view plus shared canonical-hashing integrity helpers. It never registers,
  freezes, resolves a handler, or invokes a Tool.
- Executor never plans or changes approved arguments.
- Context Builder and Verifier never invoke a Tool. Verification Steps pass
  through Runtime, Policy, and Executor. Through Phase 7, Context Builder only
  consumes supplied evidence; Phase 8 may create an explicit linked
  observation Task that follows the complete lifecycle.
- Tool never plans, approves, or performs policy decisions.
- Memory never affects Policy or Approval.

All Python constructor injection for Runtime components is a trusted,
in-process composition and testing seam. It is not an untrusted plugin API and
must not be selected by a model, Skill, environment variable, runtime config,
or deserialized request. The supported CLI constructs the built-in Executor,
Tool Gateway, Verifier, and frozen Registry. A future configurable Executor
requires a separate RFC and Runtime-owned dispatch provenance; ordinary
result/report hashes cannot prove that an injected Executor invoked a handler.

RuntimeOutcome is structured return data, not an authorization, resume token,
or authenticated cross-process record. Its ordinary SHA-256 hashes bind
content deterministically but do not prevent coordinated rewriting and
rehashing. Through Phase 8 no external Outcome deserialization path may resume
or authorize work. Phase 9 must reconstruct authority only from owner journals
and revalidate Plan, Policy, Tool Metadata, and verification semantics before
using persisted facts.

The authoritative execution data flow is:

```text
Runtime
  -> Policy
  -> Approval Decision (Policy determines whether Human Approval is required)
  -> Executor
  -> Tool Gateway
  -> Tool
  -> Runtime
  -> Verifier
```

The Tool Gateway only registers, resolves, and validates Tools. It never
performs Policy, Approval, planning, or verification.

Phase 3 Policy configuration is one package-resident, versioned, strict JSON
Policy Profile plus a separate human review record. Startup validates the
Profile Schema, its RFC 8785 canonical SHA-256 hash, and the review binding
before constructing Runtime. Policy configuration has no environment
override, wildcard, hot reload, executable expression, or dynamic language.
Changing Profile content invalidates the old review and requires an
independently reviewed record for the new hash.

L1 is fail-closed: absence of an exact capability rule is `DENY`; a matching
rule explicitly selects `NOT_REQUIRED` or `HUMAN_PLAN_APPROVAL`. Phase 4 denied
resolved L3 Steps while its single-use confirmation protocol was isolated from
dispatch. Phase 5 connects that protocol to the exact Tool invocation
boundary. This removes the temporary framework-level denial only after all
Phase 5 gates pass; it does not register an L3 capability. The default Registry
remains L0-only, and an L3 Tool must still pass exact Registry, Policy, Plan
Approval, and per-invocation Confirmation checks.

Diagnosis is the Planner's evidence-linked explanation and `reason` output, not
a separate component or state. Review and Commit are Approval events represented
by `WAITING_FOR_APPROVAL`.

### 2.3 Context and verification evidence flows

Phase 0–7 Context Builder accepts only Task data, static local configuration,
Memory facts when available, and evidence already supplied by Runtime. It does
not decide which remote Tools to run.

Before Phase 8 enables remote context, Runtime adds versioned Context Profile,
Context Profile Registry Record, and `ObservationRequest` contracts. Runtime
uses only Task fields, trusted target configuration, and sanitized Memory to
build `BootstrapContext`; a Context Skill can suggest only an exact registered
Profile ID, Version, and Hash. Neither Planner nor the model supplies host,
user, port, command, flags, or free arguments.

Runtime converts the validated Profile into a linked `OBSERVATION` Task. The
child follows the complete Runtime lifecycle and its read-only Plan passes
through Policy, Approval Decision, Executor, Tool Gateway, and Verifier. The
parent remains in `CONTEXT_BUILDING` and consumes only a finalized structured
child result. Observation Tasks cannot recursively create Observation Tasks;
there are no nested lifecycle events that secretly execute a Tool inside
`CONTEXT_BUILDING`.

Every ExecutionPlan v2 contains one or more ordered, structured, mandatory
`verification_criteria`. Criteria may reference an existing read-only result
when its Contract permits self-evidence. A separate `ExecutionStep` with role
`VERIFY` exists only when an executed Tool Contract declares that exact
read-only verification Tool; its identity, arguments, order, expected
conditions, and all criteria are included in Approval Snapshot v2 and the Plan
Hash. Runtime asks Executor to run Contract-required steps during `VERIFYING`,
then passes their typed results to Verifier. Verifier never invents or
dispatches a verification call.

ExecutionPlan role ordering is deterministic: zero or more `OBSERVE`/`ACTION`
steps form the execution prefix and zero or more `VERIFY` steps form the
verification suffix. No `OBSERVE` or `ACTION` step may follow a `VERIFY` step.
Runtime asks Executor to run only the prefix in `EXECUTING` and only the suffix
in `VERIFYING`. An L3 Tool is valid only in an `ACTION` step. Current read-only
`get_system_status@1.0.0` self-verifies from its original result and therefore
has no duplicate `VERIFY` call. Every mutating Tool must have at least one
independent read-only `VERIFY` step referenced by a criterion.

Every `VERIFY` Step must be Contract-declared, criterion-bound, read-only, and
non-L3. Every required verification reference on every executed source Tool
must have a matching Step and criterion; partial coverage is invalid. Phase 6
temporarily permits at most one mutating source Step per Plan. Every required
verifier for that mutation must be covered by a meaningful postcondition of
kind `numeric_bounds`, `expected_state`, or `health_status`; provenance-only
`equals` cannot close the mutation. Multiple mutations remain unavailable
until an explicit action-to-criterion/effect binding is separately reviewed.

### 2.4 Runtime states

The canonical enum contains:

```text
RECEIVED
CONTEXT_BUILDING
PLANNING
POLICY_CHECK
WAITING_FOR_APPROVAL
EXECUTING
VERIFYING
COMPLETED
FAILED
PARTIAL_SUCCESS
ROLLBACK
MANUAL_INTERVENTION_REQUIRED
```

`WAITING_APPROVAL` is not an alias. `WAITING_FOR_APPROVAL` is the only public
name.

Phase 0 implements this L0 path:

```text
RECEIVED
  -> CONTEXT_BUILDING
  -> PLANNING
  -> POLICY_CHECK
  -> WAITING_FOR_APPROVAL
  -> EXECUTING
  -> VERIFYING
  -> COMPLETED
```

For L0, `WAITING_FOR_APPROVAL` records the Policy-produced `NOT_REQUIRED`
decision and exits immediately without human input. When Policy requires Human
Execution Approval, the same state pauses until a valid exact-plan decision is
available.

Phase 0 defines all enum values but does not invent behavior for reserved
exceptional states. Phase 1 adds fail-closed transition behavior. No phase in
this plan may make a reserved state reachable without a prior governance
revision to `docs/STATE_MACHINE.md`. Rollback is always a separate Task and
ExecutionPlan with a fresh Policy check, applicable Approval, execution, and
verification; it is never an implicit state transition.

The cross-phase failure mapping is:

| Condition | Runtime state |
| --- | --- |
| Policy denial, human rejection, or definite failure before any Tool succeeds | `FAILED`; structured outcome records no confirmed effect |
| Definite failure after one or more earlier steps succeeded | `FAILED`; structured outcome records prior successes and any confirmed partial effect |
| Mutating Tool dispatched, followed by timeout, unknown outcome, or failed verification | `FAILED`; structured outcome marks the effect unknown and human intervention required |
| Every required verification passes | `COMPLETED` |
| Any rollback request | Create a separate governed Task and Plan; `ROLLBACK` remains unreachable |

Only Runtime changes Task state.

### 2.5 Core public data contracts

- `Task`: immutable task ID, user request, target reference, current state, and
  state history.
- `RuntimeContext`: Task facts plus structured evidence; no credentials,
  permissions, or executable callbacks.
- `Target`: immutable `target_id` and trusted local configuration reference;
  model output never supplies connection fields.
- `BootstrapContext`: bounded Task, trusted Target, and sanitized Memory fields
  available before remote evidence retrieval.
- `ContextProfile`: immutable Profile ID, Version, Content Hash, exact
  read-only Tool references, trusted argument bindings, budgets, and stop
  conditions.
- `ContextProfileRegistryRecord`: Profile status, Hash binding, reviewer, and
  timestamps stored outside immutable Profile content.
- `ObservationRequest`: Runtime-owned, typed read request generated from a
  versioned local context profile.
- `ExecutionStep`: step ID, Tool name and version, exact typed arguments,
  role (`OBSERVE`, `ACTION`, or `VERIFY`), reason, impact, human-readable
  verification summary, and recovery guidance.
- `VerificationCriterion`: one strict v1 discriminated-union variant
  (`equals`, `numeric_bounds`, `expected_state`, or `health_status`) with a
  unique ID, evidence Step binding, literal mandatory flag, freshness limit,
  and fixed evaluator version; no dynamic evidence path or expression.
- `ExecutionPlan`: schema version, plan ID, task ID, target, ordered immutable
  steps, and ordered Plan-level verification criteria. Phase 6 upgrades it to
  v2 so the canonical Plan digest includes the criteria.
- `ToolContract`: immutable exact Tool identity, implementation binding,
  description, static RiskLevel, deterministic approval implication, side
  effects, target scope, strict input/output Schemas, redaction, errors,
  timeout, retry boundary, Verification, rollback, and replay references.
- `ToolMetadata`: read-only Registry projection derived from one validated
  exact Tool Contract. It contains Tool ID and Version, Contract and
  Implementation Hashes, description, static RiskLevel, side effects, target
  scope, redaction, Verification, rollback, timeout, idempotency, Schema
  identities, and typed model bindings. Required approval mode is not
  independently configurable Metadata; Policy derives it from RiskLevel and the
  fixed risk matrix.
- `ToolRegistryRecord`: separate status and review record binding exact Tool
  identity, Contract Hash, Implementation Hash, reviewer, and UTC timestamps
  without mutating the immutable Contract.
- `ToolCall`: immutable Invocation ID, Plan Step ID, exact Tool ID and Version,
  Contract and Implementation Hashes, canonical Arguments Hash, structured
  Target Reference, and validated typed arguments.
- `ToolError`: stable code, redacted message, and retryable information; it
  never grants retry permission.
- `ToolResult`: Gateway-owned Invocation and Plan Step identity, Tool identity,
  Contract and Arguments Hashes, Target Reference, success flag, typed
  Tool-specific data, structured evidence, optional structured error, and
  non-negative duration. It is never a plain string.
- `PolicyDecision`: allow/deny, reason code, resolved risk, and required
  approval mode.
- `PlanApprovalSnapshot`: exact ordered Plan content plus frozen Registry risk,
  scope, side-effect, redaction, Verification, rollback projections, and all
  ordered criteria; Phase 6 Snapshot v2 is the sole v2 Plan Hash input and
  retains only arguments safe for human review.
- `ApprovalReview`: short-lived exact safe snapshot, Plan Hash, Policy
  identity/decision hash, risk, approval requirements, target, and expiration.
- `ApprovalRecord`: plan hash, ordered arguments-hash commitments, approver,
  approval mode, issued-at, expires-at, and immutable content hash.
- `ApprovalAuditEvent`: append-only non-secret Review, Approval, attempt, and
  L3 confirmation facts with ordered arguments-hash commitments.
- `ManualConfirmationRecord`: Approval ID, plan hash, exact L3 step ID,
  canonical arguments hash, confirmer, execution-attempt ID, issued-at,
  short expires-at, and one-time consumption event.
- `ExecutionAttemptAuthorization`: immutable attempt, Task, Plan,
  PolicyDecision, optional Approval, expiration, and content-hash binding that
  proves Executor consumed the authority for this attempt.
- `ManualConfirmationChallenge`: immutable exact L3 invocation commitment whose
  Hash covers the complete strict model except the Hash itself, including
  Schema Version, Authorization Hash, Approval ID, Approval Plan/Record Hashes,
  Approval Expiration, Step Index/ID/Role, Tool identity and integrity Hashes,
  Arguments Hash, Target, execution attempt, and invocation; it contains no
  confirmation response.
- `StepExecutionRecord`: one sanitized Tool invocation result plus dispatch and
  effect certainty.
- `ExecutionReport`: immutable ordered Step records, total plan duration,
  stopped failure position, human-intervention fact, next-state fact, and
  redacted audit references.
- `ExecutionUncertainty`: immutable attempt-level evidence used only when no
  trustworthy final ExecutionReport and no confirmed safe abort are available;
  it binds authorization, optional prior trusted report Hash, dispatch/effect
  certainty, human-intervention fact, stable reason, and content Hash without
  fabricating Step or Invocation facts.
- `VerificationContext`: Runtime-owned immutable Task, Plan digest, Execution
  Attempt and cumulative report binding plus local evidence acceptance time,
  conservative collection duration, the same trusted Runtime clock sample as
  evaluation time, and pending-mutation fact;
  its fields are `context_schema_version`, `task_id`, `plan_id`,
  `plan_digest`, `execution_attempt_id`, `execution_report_hash`,
  `evidence_accepted_at`, `evaluated_at`, `collection_duration_ms`, and
  `mutating_effect_pending`. A null acceptance time only models
  `CLOCK_UNAVAILABLE` and can never pass; Schema Version is `"1"`, duration is
  a 0–3600000 integer, and non-null times are exact UTC.
- `VerificationResult`: immutable identity-bound `PASSED | FAILED`, ordered
  criterion checks, structured evidence references, stable failure reasons,
  `NONE | VERIFIED | UNKNOWN` effect disposition, human-intervention fact, and
  content Hash. Its fields are `result_schema_version`, `task_id`, `plan_id`,
  `plan_digest`, `execution_attempt_id`, `execution_report_hash`,
  `evaluated_at`, `status`, `checks`, `evidence_references`,
  `failure_reasons`, `effect_disposition`, `human_intervention_required`, and
  `content_hash`; Schema Version is `"1"`.
- `VerificationCheckResult`: `criterion_id`, `evidence_step_id`,
  `evaluator_version`, `status`, and an optional stable `failure_reason`; it
  never retains a raw observed value.
- `VerificationEvidenceReference`: ordered Step/Invocation/Tool identity,
  Contract/Implementation/Arguments/Result Hashes, Target, and Runtime
  `accepted_at`; it never copies raw evidence.
- `IncidentRecord`: Task, Plan, Approval, execution, verification, and lessons,
  after redaction.

All timestamps use timezone-aware UTC. All IDs use stable string
representations. All public models use Pydantic validation and reject unknown
security-sensitive fields where appropriate.

The owning phase introduces each concrete contract rather than Phase 0 creating
empty future interfaces:

| Contract | Owning phase |
| --- | --- |
| Task, RuntimeContext, ExecutionPlan, ExecutionStep, ToolMetadata, ToolResult | Phase 0 |
| RuntimeOutcome and lifecycle events | Phase 1 |
| ToolContract, ToolRegistryRecord, ToolCall, ToolError, and replay/implementation artifacts | Phase 2 |
| PolicyDecision | Phase 3 |
| PlanApprovalSnapshot, ApprovalReview, ApprovalRecord, ManualConfirmationRecord, ApprovalAuditEvent | Phase 4 |
| ExecutionAttemptAuthorization, ManualConfirmationChallenge, StepExecutionRecord, ExecutionReport, ExecutionUncertainty | Phase 5 |
| VerificationCriterion, VerificationContext, VerificationResult, ExecutionPlan v2, PlanApprovalSnapshot v2 | Phase 6 |
| Target, BootstrapContext, ContextProfile, ContextProfileRegistryRecord, and ObservationRequest | Phase 8 |
| IncidentRecord | Phase 9 |

Task state changes only through Runtime. Plan, Tool Metadata, calls, approval
snapshots, and reports are immutable once accepted.

### 2.6 Approval hashing

The Plan Hash algorithm and coverage are normative in
`docs/ARCHITECTURE.md#plan-integrity-and-hash`. It is SHA-256 over UTF-8 RFC
8785 canonical JSON after strict Plan Schema validation. The hash input
includes every behavior-visible field, at least:

- plan schema version;
- task ID, plan ID, target reference, and target scope;
- ordered step IDs and roles;
- Tool ID, Version, Contract Hash, and Implementation Hash for every step;
- exact validated arguments;
- expected evidence, verification criteria, and execution order;
- rollback guidance or explicit `not_available`;
- selected Skill ID, Version, and Content Hash;
- reason, impact, limitations, and declared side effects.

The hash excludes the hash field itself, ApprovalRecord data, and volatile
display timestamps. Approval validation recomputes the hash before creating an
execution attempt and before every invocation, and also compares the stored
ordered arguments. Approval is consumed exactly once when Executor creates the
bound attempt, before Runtime enters `EXECUTING`; it is not described as
adjacent or atomic with dispatch. Every L3 Tool invocation uses the same plan
hash and requires its own separately timestamped, single-use Manual
Confirmation immediately before that exact Step is dispatched; local
single-user mode does not require a different second person.
It does not allow a changed Plan, Step, or Arguments.

Consumption binds the approval to one unique execution attempt. It remains
usable only by that in-progress attempt, subject to expiration and hash
revalidation, and can never start a second attempt.

### 2.7 Error and logging rules

- Domain failures use explicit exception types and stable error codes.
- Tool failures use a structured error containing a code, redacted message,
  and retryable flag. A retryable flag is information, not permission to retry.
- Dangerous operations are never silently retried.
- Logs are structured JSON. When a trusted per-Step ExecutionReport exists,
  execution audit includes Task ID, Plan ID, Approval ID when applicable,
  operator, target, Tool, redacted arguments, result status, duration, report
  Hash, and verification status.
- When only attempt-level closure uncertainty is trustworthy, a separate
  `execution_uncertainty_audit` includes Task/Plan/Attempt identifiers,
  Authorization/Uncertainty Hashes, optional prior-report Hash,
  dispatch/effect certainty, human-intervention flag, stable reason, and
  verification status. It does not invent Tool, Target, result, or duration.
- Every produced VerificationResult emits a `verification_audit` containing
  only Task/Plan/Attempt/report/result Hash bindings, status, stable reasons,
  effect/human closure, check identity/status, and evidence-reference
  Step/Invocation/result Hashes. Raw evidence, observed values, and unredacted
  arguments are forbidden.
- Passwords, private keys, tokens, and secret-bearing raw output are never
  logged, persisted, or returned unredacted.
- Model events add token count and latency; unavailable provider metrics are
  recorded explicitly as unknown rather than invented.
- Size limits and redaction apply before data enters logs, model prompts,
  ToolResults, CLI output, or persistence.

## Phase 0 — Foundation

Status: Implemented (2026-07-25); Approval Gate conformance completed
(2026-07-26)

### Goal

Create the smallest installable and testable Python project that proves the
Runtime boundaries without touching a real system.

### Inputs

- The six governing documents.
- The fixed decisions and contracts in this plan.
- Python 3.12 or newer.
- Typer, Pydantic, SQLAlchemy, Rich, pytest, Ruff, and mypy as declared
  dependencies or development dependencies.

### Outputs

- An installable `ai_server` package.
- `ai-server version` and `ai-server doctor`.
- The complete RuntimeState enum.
- Task, ExecutionPlan, ExecutionStep, ToolMetadata, RiskLevel, and ToolResult.
- A deterministic L0 `get_system_status` Mock Tool.
- A minimal Runtime outcome with exact state history.

### Deliverables

- `pyproject.toml` with build metadata, the `ai-server` console entry point,
  Python requirement, dependency groups, and pytest/Ruff/mypy configuration.
- The approved `src/ai_server` skeleton without speculative base classes.
- A version command that prints the program name and package version.
- A doctor command that checks only Python compatibility, package imports, and
  the local Mock Runtime.
- A Mock status payload with deterministic fake host, CPU, memory, disk, and
  service values clearly marked as simulated.
- Explicit invalid-transition, policy-denied, Tool, and verification
  exceptions.

### Acceptance Criteria

- `ai-server version` exits zero and reports the declared version.
- `ai-server doctor` performs no network, filesystem mutation, database,
  process, SSH, Docker, or model operation.
- The Mock Tool is statically L0 in Tool Metadata and returns a typed payload.
- The Runtime records the exact governed L0 happy path, including the
  `WAITING_FOR_APPROVAL` decision gate.
- Planner returns a plan and has no Tool dependency.
- Policy reads Tool Metadata and allows the L0 mock deterministically.
- Executor alone calls the Mock Tool.
- Verifier checks the ToolResult without calling another Tool.
- Every public API has a type hint and docstring.

### Test Requirements

- CLI tests cover output, exit code, and the absence of external checks.
- Model tests cover valid construction, invalid arguments, immutability where
  required, serialization, and rejection of malformed results.
- State tests cover every enum value, the L0 path, the approval branch
  definition, and invalid transitions.
- Tool tests cover metadata, deterministic output, typed data, and no external
  dependency.
- Policy tests prove L0 is derived from metadata rather than plan text.
- Runtime tests assert exact state history and component call order.
- Run `pytest`, `ruff check .`, `ruff format --check .`, and strict
  `mypy src tests`.

### Out of Scope

Real approval execution, state persistence, SQLAlchemy models, database writes,
LLMs, HTTP providers, SSH, Docker, systemd, arbitrary Shell, configuration
changes, deletion, Web UI, multi-user, multi-agent Runtime behavior, remote
agents, and cloud services.

## Phase 1 — Runtime

Status: Implemented (2026-07-25); Approval Gate conformance completed
(2026-07-26)

### Goal

Turn the Phase 0 vertical slice into an explicit fail-closed lifecycle
orchestrator while keeping all external behavior mocked.

### Inputs

- Phase 0 RuntimeState, Task, Runtime Context, plan, and Tool result models.
- The Phase 0 component implementations and tests.
- The dependency rules in section 2.2.

### Outputs

- A Runtime Engine that owns state changes and component order.
- Pure context construction from supplied structured evidence.
- Immutable lifecycle history and structured RuntimeOutcome.
- Defined failure behavior for every active Phase 1 stage.

### Deliverables

- A single Runtime orchestration service.
- A transition validator rather than direct state assignment.
- Structured lifecycle events with timestamps from an injectable clock for
  state entry, component completion, pause, rejection, and failure.
- Explicit exceptions for invalid transitions, unsupported requests, component
  failures, and terminal-state mutation.
- A boundary test that ensures forbidden component dependencies are absent.

### Acceptance Criteria

- No application service bypasses Runtime to execute a task.
- Every state change passes through the transition validator.
- `POLICY_CHECK → EXECUTING` is rejected. Every allowed Plan enters
  `WAITING_FOR_APPROVAL`; `NOT_REQUIRED` records an Approval Decision and exits
  immediately to `EXECUTING`.
- Any unrecoverable error from an active nonterminal state records `FAILED`
  before returning a structured failure or raising its documented exception.
- Human-approval-required work stops at `WAITING_FOR_APPROVAL`, returns control
  without a Tool call, and cannot resume until Phase 4 supplies a valid
  ApprovalRecord. Human rejection enters `FAILED`.
- `COMPLETED` and `FAILED` are immutable terminal states in Phase 1.
- `PARTIAL_SUCCESS`, `ROLLBACK`, and `MANUAL_INTERVENTION_REQUIRED` are reserved
  and reject all incoming transitions until their owning later phases define
  them.
- Context Builder accepts data and produces RuntimeContext; it performs no I/O.
- State history and events preserve execution order and cannot be rewritten.

### Test Requirements

- A transition matrix tests every permitted and rejected pair.
- Tests assert the exact L0 state/event sequence includes
  `POLICY_CHECK → WAITING_FOR_APPROVAL → EXECUTING` and that direct bypass is
  rejected.
- Failure injection covers Context Builder, Planner, Policy, Executor, and
  Verifier.
- Tests assert a single `FAILED` transition and no component calls after
  failure.
- Tests cover terminal-state immutability and unsupported reserved states.
- Tests cover the approval pause, no-execution guarantee, and human rejection.
- Architecture tests or import checks prevent Planner-to-Tool,
  Policy-to-model, Context-to-Tool, Verifier-to-Tool, and Memory-to-Policy
  dependencies.

### Out of Scope

Real Tool transports, scheduling, workflow graphs, concurrency, persistent
sessions, approval persistence, Incident Memory, and remote targets.

## Phase 2 — Tool Protocol

Status: Implemented (2026-07-29); only the reviewed local
`get_system_status@1.0.0` Mock artifact is registered

### Goal

Establish a small, typed, artifact-driven protocol through which every
operational capability is described, integrity-bound, validated, dispatched,
and reported without enabling real system access.

### Inputs

- The completed Phase 1 Runtime orchestration and Approval Gate.
- Phase 0 Tool Metadata, RiskLevel, and ToolResult models.
- The static L0 `get_system_status` Mock Tool.
- `docs/TOOL_SPEC.md`, including its fail-closed registration gates.

### Outputs

- Five versioned, package-resident JSON Schema Draft 2020-12 meta-schemas.
- Immutable Tool Contract, Registry Record, Implementation Bundle, ToolCall,
  ToolResult, ToolError, and replay-fixture models.
- A frozen artifact-driven Registry and deterministic Tool Gateway.
- Typed Tool-specific input and payload models.
- Structured, bounded, and redacted Tool results and errors.
- Sanitized offline Mock replay evidence and reproducible local build evidence.

### Deliverables

- Versioned local Schemas for:
  - Tool Contract;
  - Tool Result;
  - Tool Replay Fixture;
  - Tool Registry Record;
  - Tool Implementation Bundle.
- One package-resident reviewed artifact set for every exact Tool version:
  immutable `contract.json`, separate `registry-record.json`,
  `implementation-bundle.json`, deterministic dependency-lock evidence, and
  referenced sanitized fixtures.
- UTF-8 RFC 8785 canonical JSON plus SHA-256 helpers for Contract,
  Implementation, Arguments, and Fixture hashes. Each artifact defines its
  exact hash input and excluded self-hash or mutable status fields.
- An artifact loader that validates all five meta-schemas, validates nested
  Contract input/output Schemas, verifies ABI and dependency-lock identity,
  hashes every declared installed implementation file, rejects undeclared or
  unsafe paths, and verifies fixture references and contents.
- An explicit startup Registry keyed by immutable `(tool_id, version)`. It
  derives read-only authoritative Tool Metadata from the validated Contract
  rather than caller-supplied metadata, rejects duplicate or non-registered
  identities, and freezes before Policy or Gateway lookup.
- A strict immutable ToolCall binding Invocation ID, Plan Step ID, exact Tool
  identity, Contract Hash, Implementation Hash, canonical Arguments Hash,
  structured Target Reference, and typed arguments.
- A Gateway-owned ToolResult envelope with typed Tool-specific payload data,
  structured evidence, one sanitized ToolError on failure, and bounded
  duration. The complete envelope validates against the global Tool Result
  Schema and the exact Contract output Schema.
- A Gateway callable only by Executor. It exact-resolves through the frozen
  Registry; checks identity and hashes; validates input before dispatch with
  both Pydantic and the registered Schema; validates target scope; invokes once;
  validates output after return; applies redaction and retained-size bounds;
  and maps failures to stable errors.
- Migration of `get_system_status` to a deterministic typed payload-only Mock
  handler. It returns no trusted envelope fields and performs no external I/O.
- A sanitized offline replay-validation path that consumes only recorded Tool
  Results, local fixtures, or Mock Tools; never invokes production Tool code;
  and checks identity, Contract and Arguments hashes, invocation order,
  schemas, expected outcome or error, Verification evidence, and redaction
  evidence.
- A repository-local dependency lock generated and checked with `uv`. Each
  Tool's reviewed dependency-lock evidence is deterministically bound to its
  implementation manifest and to the locally resolved dependency set.
- Local-only build gates that create source and wheel artifacts, inspect
  packaged resources, run lint, formatting, type, and test checks on the
  source tree, install the wheel into a clean local environment, and run
  package-resource, import, CLI, and Mock Runtime smoke checks there.

### Acceptance Criteria

- No Tool becomes available unless all five normative Schemas load from the
  installed package and every Contract, Registry Record, Implementation Bundle,
  dependency lock, declared file, and referenced fixture passes strict
  validation.
- Contract Hash is recomputed over schema-validated raw Contract JSON using
  UTF-8 RFC 8785 canonical JSON and SHA-256, excluding only the explicitly
  defined self-hash or mutable Registry fields. Implementation, Arguments, and
  Fixture hashes use their separately defined canonical inputs and exclusions.
- The Registry Record exactly binds Tool ID, Version, Contract Hash,
  Implementation Hash, `registered` status, reviewer, and UTC timestamps
  without mutating the Contract.
- Registered Metadata is derived from the exact validated artifact set.
  RiskLevel, timeout, idempotency, schema identities, Contract Hash, and
  Implementation Hash cannot be supplied or overridden by Planner, caller
  arguments, Tool implementation, fixture, or Runtime text.
- Duplicate identity, design-only/disabled/deprecated status, missing review,
  ABI mismatch, dependency-lock drift, installed-file drift, undeclared code,
  malformed Schema, missing fixture, Secret/executable content, or any hash
  mismatch fails registration closed.
- Registry lookup is unavailable until startup registration is frozen. After
  freeze, mutation is rejected and resolution requires an exact registered
  `(tool_id, version)`.
- The Gateway accepts only a strict ToolCall received from Executor, exact-
  resolves the same Contract and implementation, and rejects identity, hash,
  arguments, target, or scope mismatch before handler dispatch.
- Defaults, when a Contract permits them, are materialized before Plan hashing
  and Approval. The Gateway neither injects hidden defaults nor changes
  approved arguments.
- The handler receives one typed validated arguments object and returns only
  one typed payload object. The Gateway owns trusted invocation, target, hash,
  timing, success/error, evidence, and envelope fields.
- Every success and failure is a complete ToolResult. The global Tool Result
  Schema and exact Contract output Schema validate the final envelope; free
  strings, raw exceptions, callbacks, transports, and command strings never
  cross the boundary.
- Redaction and payload-size limits are enforced before a result, fixture, log,
  Incident, or replay artifact can be retained. Redaction failure returns a
  safe structured failure and does not preserve unsafe raw content.
- The Gateway dispatches at most once. A timeout observed after return is a
  structured failure and never authorizes retry; Phase 2 adds no process
  cancellation or real transport timeout mechanism.
- Tool code imports no Planner, Policy, Approval, Memory, LLM/model adapter,
  SSH, Docker, database, network, shell, or subprocess capability.
- Only Executor may invoke the Gateway in application code. The Registry does
  not expose its private handler binding through a public API to Planner,
  Policy, Approval, Skills, Memory, or Runtime callers; isolated unit tests may
  call the Mock handler directly.
- Fixture and replay validation runs offline with production connections
  disabled and never invokes production Tool code. It rejects mismatched
  identity, version, hashes, sequence, Schema, outcome, redaction report, or
  fixture content.
- The repository-local `uv` lock check is clean and a frozen local dependency
  sync is reproducible without global installation.
- Source distribution and wheel build successfully. The wheel contains all
  five Schemas and the complete reviewed Mock Tool artifact set. A clean local
  wheel installation passes package-resource loading, import,
  `ai-server version`, `ai-server doctor`, and Mock Runtime smoke checks. The
  corresponding source tree passes pytest, Ruff, formatting, and strict mypy.
- No Phase 2 test or runtime path opens SSH, invokes an LLM/model, runs shell
  commands, calls Docker or Kubernetes, accesses a database, opens a network
  connection, resolves a remote target, or performs other real system I/O.

### Test Requirements

- Schema tests validate and negatively mutate all five meta-schemas, their
  stable local `$id` values, unknown-field rejection, nested enums, and
  expressible cross-field constraints.
- Hash tests use independent RFC 8785 vectors and cover raw-artifact hashing,
  Unicode and numeric canonicalization, ordering, every exclusion rule, and
  one-bit drift.
- Artifact tests cover missing and malformed files, Contract/Registry/manifest
  mismatch, status and review rules, dependency lock and ABI mismatch,
  installed-file digest drift, undeclared/duplicate/traversal/symlink paths,
  missing or duplicate fixtures, Secret and executable-payload scanning, and
  artifact-derived Metadata.
- Registry tests cover duplicate identity, pre-freeze lookup, post-freeze
  mutation, exact-version resolution, unavailable status, immutable snapshots,
  and private handler access.
- Gateway tests cover strict ToolCall reconstruction, wrong Contract or
  Implementation Hash, arguments hash mismatch, Pydantic and JSON Schema input
  failure, target expansion, one dispatch, handler exception, malformed typed
  payload, global and Contract output validation, clock failure, declared
  timeout, redaction, retained-size bounds, and stable sanitized errors.
- Replay tests cover deterministic Mock and recorded fixtures, exact sequence
  matching, identity and hash drift, expected outcomes, Verification evidence,
  redaction report, fixture content hash, production-handler non-invocation,
  and technical external-I/O blocking.
- Boundary tests prove only Executor application code can call Gateway and no
  forbidden dependency enters Tool modules.
- The manual release packaging gate builds both source and wheel artifacts
  locally, inspects package data, installs the wheel into a clean local
  environment, and repeats import, CLI, Mock Runtime, schema, artifact, and
  replay smoke checks. It is recorded separately from the hermetic unit-test
  suite because dependency installation may require a pre-populated local
  package cache.
- Required gates are `pytest`, `ruff check .`, `ruff format --check .`, strict
  `mypy src tests`, a clean repository-local `uv` lock/frozen-sync check, and
  successful local source/wheel build plus clean-wheel smoke tests.

### Out of Scope

Dynamic discovery, plugins, arbitrary user Tools, SSH or remote credentials,
LLMs or model adapters, Docker, Kubernetes, HTTP, database or network access,
arbitrary Shell or subprocesses, real transport timeouts, process isolation,
automatic retry, remote execution, production registration, and every mutating
operation.

## Phase 3 — Policy Engine

Status: Implemented (2026-07-29); the active reviewed Profile grants only
`local-user → local-mock → get_system_status@1.0.0`

### Goal

Make all permission, allowlist, risk, and approval-requirement decisions
deterministic and independent of model reasoning.

### Inputs

- A validated ExecutionPlan.
- One frozen Tool Registry, exposed to Policy only through its immutable
  Metadata snapshot.
- A typed local operator context and target reference.
- A package-resident, versioned Policy Profile and independent review record,
  both loaded and validated once at startup.

### Outputs

- A PolicyDecision for the plan and each step.
- Stable `effect: ALLOW | DENY`, reason codes, and a separate
  `approval_requirement: NOT_REQUIRED | HUMAN_PLAN_APPROVAL`.
- For each L3 Step, a separate
  `manual_confirmation_requirement: PER_INVOCATION`; it is not a third
  Policy decision value.

### Deliverables

- A fail-closed Policy Engine.
- A fixed risk matrix:
  - L0: automatic only when an exact capability rule allows the operator,
    target, Tool identity, Version, Contract Hash, and Implementation Hash; a
    rule may raise the requirement to human plan approval.
  - L1: a missing exact rule is denied; a matching rule explicitly selects
    `NOT_REQUIRED` or `HUMAN_PLAN_APPROVAL`.
  - L2: allowed only with exact-plan approval.
  - L3: every resolved Step is denied throughout Phase 3. A Step that passes
    identity, integrity, and target-scope checks uses
    `l3_confirmation_unavailable`; earlier validation failures retain their
    specific reason. The decision reports that exact-plan approval and a
    per-invocation Manual Confirmation would be required, but those fields
    never override `DENY`.
- Exact operator, target, Tool identity, Version, Contract Hash, and
  Implementation Hash capability rules, without wildcards.
- Validation that plan references match registered Tool Metadata.
- A strict JSON Policy Profile with a stable Profile ID and Version, and a
  separate review record binding its RFC 8785 canonical SHA-256 content hash,
  review status, the fixed single-user reviewer `local-owner`, and a
  timezone-aware UTC review timestamp.

### Acceptance Criteria

- Policy has no LLM or model adapter dependency.
- Policy imports no Tool subsystem module except `ai_server.tools.registry` for
  read-only frozen Metadata access and `ai_server.tools.hashing` for integrity
  checks. It has no Gateway, Executor, Planner, Approval, Memory, network,
  Shell, or filesystem-write dependency.
- Tool risk is read from the frozen Registry, never accepted from model output
  or independently supplied caller Metadata.
- Resolved step risk is read-only, and plan risk is the highest resolved step
  risk.
- L1 requires an exact rule; a missing rule denies rather than upgrading or
  guessing. An exact rule can choose only `NOT_REQUIRED` or
  `HUMAN_PLAN_APPROVAL`.
- Any unknown Tool, Tool version, operator, target, risk, policy field, or
  capability binding produces deny.
- A plan containing one denied step is denied as a whole.
- A plan containing an L2 step cannot reach Executor without human plan
  approval. A plan containing an L3 step cannot reach Executor in Phase 3,
  regardless of profile content or approval data.
- Malformed Policy Profile or review JSON, an invalid content hash, an
  unapproved review status, or a mismatched review binding prevents Runtime
  construction. Profile content is immutable for that process lifetime.
- Policy Profile changes cannot take effect through hot reload, environment
  override, a dynamic policy language, or user-defined executable rules.
- The same inputs always produce the same PolicyDecision.

### Test Requirements

- A complete L0–L3 decision table, including L1 missing-rule denial, both
  explicit L1 approval modes, and the fixed Phase 3 L3 denial.
- Tests for mixed-risk plans, unknown metadata, target denial, Tool denial,
  operator denial, exact capability mismatches, plan risk forgery, malformed
  Profile and review JSON, review/hash drift, and unknown policy fields.
- Determinism tests repeat identical decisions.
- Negative architecture tests confirm Policy cannot import a model adapter,
  Gateway, Executor, Planner, Approval, Memory, transport, Shell, or
  filesystem-write capability, and cannot mutate or resolve a handler from the
  Tool Registry.
- Policy tests exist for every Tool added in later phases.

### Out of Scope

Approval issuance or persistence, L3 confirmation, Tool invocation, hot
reload, environment overrides, wildcard rules, user-defined or executable
policy code, dynamic policy languages, model-based risk classification, RBAC,
multi-user identity, and remote policy services.

## Phase 4 — Approval

Status: Implemented (process-local authorization only; no human-approved
dispatch)

### Goal

Create a transactional human authorization that applies only to one exact,
unexpired ExecutionPlan.

### Inputs

- A PolicyDecision requiring `HUMAN_PLAN_APPROVAL`, including Policy-raised
  L0/L1 restrictions and mandatory L2/L3 approval.
- A validated ExecutionPlan and registered Tool Metadata.
- The fixed local operator `local-user` and local approver `local-owner`.
- An injectable timezone-aware UTC clock.

### Outputs

- An immutable ApprovalRecord.
- A structured approval validation result.
- Audit events for approve, reject, expire, invalidate, and per-Step L3
  confirmation.

### Deliverables

- A strict `PlanApprovalSnapshot` derived from the Plan, Policy Decision, and
  frozen Registry Metadata; this snapshot is the only plan-hash input.
- Canonical snapshot serialization and SHA-256 hashing defined in section 2.6.
- Plan review data containing why, what, impact, verification, and recovery for
  every step.
- A reviewed Policy Profile v1.1 using Profile Schema v2. It fixes maximum
  Review, Approval, and L3 confirmation TTLs at 300, 300, and 30 seconds.
- Approval issuance with Policy-controlled expiration.
- Approval revalidation and consumption APIs for the Phase 5 Executor boundary.
- A single-use L3 confirmation bound to the unchanged Plan, exact Step and
  Arguments, execution attempt, and invocation.
- Thread-safe, process-local linearized one-time Approval and Confirmation
  ledger protocols, exercised only against a fake dispatch boundary in Phase
  4. They do not claim atomicity with dispatch or a remote side effect.
- An in-process `ai-server run` Review/Commit interaction that displays the
  exact plan and hash; Phase 4 stops after recording authorization, and Phase 5
  connects the same-process flow to Executor.

### Acceptance Criteria

- Approval applies to the complete ordered plan, not an informal message or
  independently mutable step.
- Stored ordered arguments exactly match the canonical plan.
- Any target, Tool, version, argument, order, verification, or recovery change
  invalidates approval.
- Expired (`now >= expires_at`), rejected, consumed, already invalidated,
  wrong-mode, or wrong-plan approval is unusable.
- Consumption binds approval to one execution-attempt ID; only that in-progress
  attempt may continue to revalidate it, and it cannot authorize a retry.
- L3 requires base Plan approval plus a separate valid confirmation for every
  L3 Step invocation.
- In local single-user mode the two L3 events may have the same approver, but
  they are separate, ordered, timestamped actions.
- Only an explicit interactive TTY `COMMIT <full-plan-hash>` may create an
  Approval. There is no `--yes`, piped approval, environment override, or
  model-callable approval path.
- Phase 4 authorization and CLI interaction exist only in one process. Commit
  leaves the Task in `WAITING_FOR_APPROVAL`; human-approved execution is not
  connected until Phase 5. Persistence and cross-process resume are not
  introduced before Phase 9.
- The default Registry stays L0-only; human approval behavior is tested with
  synthetic metadata that has no Tool handler or dispatch capability.

### Test Requirements

- Golden hash vectors prove canonicalization is stable.
- Mutation tests change each hash input independently and expect rejection.
- Clock-controlled tests cover expiry boundaries.
- Tests cover L2 success, missing approval, rejected approval, invalidation,
  L3 without confirmation, per-Step L3 confirmation, confirmation replay,
  multiple L3 Steps, expiry, and hash mismatch.
- Serialization never includes secrets or unredacted arguments.

### Out of Scope

Persistent approval storage, Web UI, multi-user workflows, multiple approvers,
RBAC, external identity, approval delegation, approval reuse, any real or
registered L2/L3 Tool, and dispatch or Runtime resumption of a human-approved
Plan.

## Phase 5 — Executor

Status: Implemented (2026-08-01)

### Goal

Execute validated plans exactly as approved while remaining incapable of
planning or changing authority.

### Inputs

- A validated ExecutionPlan.
- Its PolicyDecision.
- An Approval ID referencing an Approval Engine-owned process-local
  ApprovalRecord when required; caller-supplied records are never authority.
- Authoritative frozen Tool Metadata.
- The deterministic Tool Gateway.
- An injectable monotonic timer.

### Outputs

- Ordered ToolResults.
- Structured execution events and a plan execution summary.
- A stopped failure outcome when a step fails.
- A structured ExecutionReport with execution-attempt identity, per-Step
  dispatch and effect certainty, total plan elapsed time, failure position,
  human-intervention fact, and next-state fact for Runtime.
- A structured ExecutionUncertainty for the exceptional case where the final
  report is missing or untrusted and attempt closure cannot be confirmed.

### Deliverables

- Sequential execution in exact plan order.
- Validation of the complete Plan shape before dispatch and immediate Policy,
  Approval, and Confirmation revalidation for every precondition applicable to
  the current invocation. A future Step's short-lived L3 Confirmation is not a
  precondition for an earlier invocation.
- Process-local Approval consumption linearized when Executor creates the
  exact attempt and before Runtime enters `EXECUTING`. A crash or later failure
  burns the authorization even if no Tool was dispatched; this does not claim
  transactional atomicity with a remote side effect.
- Per-invocation L3 challenge hashing over Approval, Plan, Step, exact Tool
  identity and integrity hashes, Arguments, Target, execution attempt, and
  invocation, plus Authorization Hash, Approval Record Hash, and Approval
  Expiration as part of the complete strict Challenge model; exact
  `CONFIRM <challenge-hash>` input; one-time Confirmation consumption
  immediately adjacent to the already-bound dispatch.
- An internal Gateway dispatch receipt that records whether the handler
  boundary was entered without changing Tool Protocol v1, ToolResult v1, or
  Contract Hashes.
- Plan elapsed-time recording. The current synchronous Mock Gateway remains
  authoritative for the registered single-invocation timeout and returns
  `tool_timeout` after a post-return elapsed check; Executor stops the attempt
  and does not retry, preempt, cancel, or claim transport-timeout behavior.
- Stop-on-failure behavior with no silent retries.
- `RuntimeEngine.resume_approved(outcome, approval_id, *,
  confirmation_reader=None)` for same-process governed resumption. The
  interactive CLI calls it immediately after exact Commit; the lower-level
  approval issuance API remains non-executing.
- A pre-approved verification-evidence path through Runtime, Policy, and
  Executor. Context collection remains a pure consumer of supplied evidence
  through Phase 7; linked observation Tasks remain Phase 8 scope.
- Exact sibling-Approval single consumption per Plan Hash, non-reentrant active
  Attempt calls, strict revalidation of abort reports, and bounded
  ExecutionUncertainty plus attempt-level audit when closure remains unknown.
- Independent Executor and Runtime revalidation of every retained ToolResult
  against the exact frozen Registry Contract, including immutable redacted
  evidence, declared errors, and registered timeout.
- Monotonic cumulative reports: verification and abort must preserve the last
  trusted report's exact records/events prefix and add closure progress.

### Acceptance Criteria

- Executor is the only module that calls a Tool Gateway dispatch method.
  Runtime may import and construct the Gateway only as the composition root.
- Executor never creates steps, changes arguments, substitutes Tools, or
  changes order.
- ExecutionPlan contains 1–64 Steps; model and Executor defense-in-depth checks
  reject an oversized or bypass-constructed Plan before dispatch.
- Unknown, denied, expired, consumed, or tampered work is rejected before its
  affected invocation.
- An ordered Plan contains only an `OBSERVE`/`ACTION` prefix followed by a
  `VERIFY` suffix. Runtime invokes the first segment in `EXECUTING` and the
  second in `VERIFYING`.
- An L3 Tool can appear only in an `ACTION` Step. L3 `OBSERVE` or `VERIFY`
  fails before invocation.
- L2 and L3 require a valid Plan Approval. Each L3 invocation additionally
  requires one valid, unexpired, exact-challenge Confirmation. Confirmation is
  not a Runtime state.
- The only supported application adapter for production Confirmation in Phase
  5 is the bundled CLI. It requires interactive input and output TTYs and exact
  `CONFIRM <challenge-hash>` input. The injectable Runtime/Executor reader is a
  trusted process-local test seam, not proof of human provenance; it must not
  be connected to a model, Skill, Tool, pipe, or environment value. The
  default Registry has no L3 Tool. Before any production L3 Tool is registered,
  an independently reviewed design must replace or encapsulate this seam with
  a verifiable interactive source; otherwise production L3 remains unavailable.
- Through the supported CLI adapter, a missing TTY, wrong Challenge Hash,
  rejection, or expiry produces no affected dispatch.
- On a failed step, later steps do not run.
- Any definite step failure enters `FAILED`. ExecutionReport preserves prior
  results and distinguishes `NOT_DISPATCHED`, handler dispatch, no effect,
  effect pending independent Verification, and an unknown possible effect.
  A mutating invocation never becomes a confirmed effect solely because its
  handler returned successfully.
- If final report validation and safe abort both fail, Runtime enters `FAILED`
  with `execution_abort_uncertain`. Before any dispatch-capable Executor call,
  certainty is `NOT_DISPATCHED/NONE`; after such a call it is
  `UNKNOWN/UNKNOWN` and requires human intervention. A prior trusted
  `AWAITING_VERIFICATION_DISPATCH` report may be retained only by exact content
  Hash. No Step, Invocation, or ToolResult is fabricated.
- All successful action steps enter `VERIFYING`; Executor never returns
  `COMPLETED`.
- A retry requires a new explicit Runtime action; dangerous work is never
  retried internally.
- ToolResults and events contain redacted values only.
- L0/L1 Contracts cannot declare remote mutation; both model validation and
  Registry registration fail closed on a forged mismatch.

### Test Requirements

- Recording Fake Tools assert exact order and arguments.
- Tests cover no-approval, expired approval, hash mismatch, policy denial,
  unknown Tool, timeout, malformed result, mid-plan failure, and stop behavior.
- Tests cover process-local concurrent consumption, injected boundary failure
  after consumption but before dispatch, burned authorization, and at-most-once
  dispatch without claiming durable crash recovery or remote transactional
  atomicity. Durable recovery remains Phase 9 scope.
- L3 tests cover complete Challenge Hash binding, wrong input, expiry, replay,
  missing TTY, multiple L3 Steps, current-invocation preconditions, reader
  reentrancy rejection, and consumption-to-dispatch adjacency.
- Runtime and CLI tests cover immediate same-process execution after Commit,
  exact paused-Outcome binding, and the non-executing approval issuance API.
- Tests prove the internal Gateway receipt identifies handler entry while the
  public ToolResult v1 shape and existing Contract Hashes remain unchanged.
- Tests cover valid and invalid Step role ordering and prove Runtime dispatches
  `VERIFY` only in `VERIFYING`; tests reject L3 `OBSERVE` and `VERIFY` roles
  before dispatch.
- Tests distinguish first-step failure from a later failure by structured
  outcome facts while asserting both enter `FAILED`; successful execution
  enters only `VERIFYING`.
- Tests prove no invocation occurs before all preconditions applicable to that
  exact invocation pass.
- Tests prove Context Builder and Verifier receive evidence but never call the
  gateway.
- Tests confirm retryable errors do not cause automatic retries.
- Hostile tests cover success-without-dispatch receipts, unregistered or
  contradictory Gateway error claims, fake/unconsumed Confirmation evidence,
  forged or malformed final and abort reports, pre-dispatch abort double
  failure, post-action clock/abort double failure, verification-close
  tampering, cumulative-report truncation, prior-report Hash retention,
  contract-invalid and secret-bearing results, retained-evidence mutation,
  sibling Approval consumption, and secret-free uncertainty audit output.

### Out of Scope

Parallel steps, background jobs, real remote transports, persistence, automatic
rollback, compensation, and mutating server Tools.

## Phase 6 — Verifier

Status: Implemented (2026-08-01)

### Goal

Evaluate whether the stated objective was achieved using fresh, structured
evidence without gaining execution authority.

### Inputs

- ExecutionPlan v2 with at least one ordered, Plan-level,
  `mandatory=true` `verification_criteria` entry.
- Approval Snapshot v2 / Plan digest binding every criterion and any
  Contract-required `VERIFY` Step.
- Trusted cumulative ExecutionReport and its ordered ToolResults. Evidence may
  come from the original read-only Step when the Contract permits self-evidence,
  or from an independent read-only `VERIFY` Step.
- One Runtime-owned trusted UTC clock sample used for both local
  `evidence_accepted_at` and `evaluated_at`, a conservatively derived
  `collection_duration_ms`, and the `mutating_effect_pending` fact.

### Outputs

- An immutable, canonical-hash-bound VerificationResult with exact
  Task/Plan/Attempt/Report identity.
- `VerificationStatus.PASSED | FAILED`, ordered
  `VerificationCheckStatus.PASSED | FAILED` per-criterion checks, structured
  redacted evidence references, and stable `VerificationFailureReason` values.
- Effect closure `NONE | VERIFIED | UNKNOWN` and an explicit
  `human_intervention_required` flag, represented by
  `VerificationEffectDisposition`; never a Tool call or recovery request.

`VerificationFailureReason` is the closed, non-secret enum:

```text
MALFORMED_PLAN
MALFORMED_EVIDENCE
PLAN_BINDING_MISMATCH
MISSING_EVIDENCE
EXTRA_EVIDENCE
EVIDENCE_ORDER_MISMATCH
EVIDENCE_IDENTITY_MISMATCH
TOOL_VERSION_MISMATCH
TARGET_MISMATCH
DUPLICATE_INVOCATION_ID
UNSUCCESSFUL_TOOL_RESULT
STALE_EVIDENCE
CONTRADICTORY_EVIDENCE
CRITERION_EVIDENCE_MISSING
CRITERION_MISMATCH
VERIFIER_RESULT_INVALID
VERIFIER_FAILED
CLOCK_UNAVAILABLE
```

### Deliverables

- Strict criterion union with exactly four `evaluator_version="1"` kinds:
  - `equals`: `source=data|evidence`,
    `field=source|simulated|target|hostname`, strict `str|bool` expected value;
  - `numeric_bounds`:
    `field=cpu_percent|memory_percent|disk_percent`, finite inclusive
    `minimum` and `maximum`;
  - `expected_state`: exact `service_name` and
    `expected_state=running|stopped`;
  - `health_status`: `expected_status=healthy|unhealthy` and
    `maximum_utilization_percent`; healthy means a non-empty set of unique
    service names, every service running, and CPU/memory/disk utilization no
    greater than the cap.
- No dynamic paths, generic field selectors, expressions, regex, callbacks, or
  executable evaluator extensions. All selectors are closed enums and all
  extraction is statically typed for the exact Tool result.
- Runtime-owned `VerificationContext` and conservative freshness calculation:

  ```text
  collection_duration_ms = min(
      max(
          ExecutionReport.total_duration_ms,
          ceil_ms(evidence_accepted_at - Runtime EXECUTING entered_at)
      ),
      3_600_000
  )

  conservative_age_ms =
      evaluated_at - evidence_accepted_at + collection_duration_ms
  ```

  Runtime takes one UTC sample after report validation and uses it for both
  acceptance and evaluation. The acceptance time is a local receipt fact, not
  a remote event timestamp.
- Evidence identity, Plan order, Target, success, content, freshness, and
  contradiction checks. Duplicate service names are contradictory rather than
  ordinary `unhealthy` evidence.
- Runtime integration for `VERIFYING`, including second validation of result
  exact types, identity, order, content Hash, and effect closure semantics.
  Runtime gives an injected Verifier only strictly rebuilt isolated copies,
  then independently recomputes the expected result from private trusted
  inputs with pure evaluators; a Hash-valid Verifier verdict is never accepted
  on trust alone.
- Exact RuntimeOutcome closure for every check identity and every ordered
  evidence reference/result Hash, plus a `verification_audit` that omits raw
  evidence and observed values.
- Full exact criterion payloads in terminal CLI Review, and distinct handling of a
  Verifier exception (`VERIFIER_FAILED`) versus an invalid or recomputation-
  mismatched return (`VERIFIER_RESULT_INVALID`).
- ExecutionPlan v2 and Approval Snapshot v2 migration; v1 authorization is not
  reusable and criteria changes invalidate approval.

### Acceptance Criteria

- Verifier has no Tool Gateway, transport, Planner, or Approval dependency.
- Every Plan has at least one unique, ordered mandatory criterion. Every
  criterion references an exact Plan Step and produces exactly one ordered
  check.
- Every verification Tool, version, argument, order, expected condition, and
  complete criterion payload was declared in ExecutionPlan v2 and included in
  its Approval Snapshot v2 Hash.
- A separate `VERIFY` Step appears only when required by the Tool Contract and
  is referenced by a criterion. It is read-only and non-L3, and every required
  verification reference declared by an executed source Tool has its own
  matching Step and criterion. The current read-only
  `get_system_status@1.0.0` reuses its original result without a duplicate Tool
  call. Its default mandatory criteria are the Contract-required
  `simulated`/`source`/`target` safe evidence fields; `hostname` is not required
  by that Contract. Every mutating source Step uses the `ACTION` role; mutation
  cannot be mislabeled as `OBSERVE`. Phase 6 permits at most one mutating source
  Step per Plan.
  Every required verifier for that mutation must have a meaningful
  `numeric_bounds`, `expected_state`, or `health_status` postcondition; its
  Action result or provenance-only `equals` cannot confirm the mutation.
- Success requires every mandatory criterion to pass against the correct target
  and sufficiently fresh evidence.
- Missing, stale, malformed, or contradictory evidence produces failure.
- Runtime takes one clock sample and uses it for both `evidence_accepted_at`
  and `evaluated_at`. `collection_duration_ms` is the greater of Executor's
  report duration and Runtime's `EXECUTING`-entry-to-acceptance elapsed time
  rounded up to milliseconds, capped at 3,600,000 ms. Tool-supplied timestamps
  cannot replace it; invalid clock order/duration and conservative age beyond
  `maximum_age_ms` fail closed.
- `PASSED` is legal only with all checks passed and no failure reasons. A
  read-only result closes with effect `NONE`; a pending mutation closes as
  `VERIFIED` only after an all-pass result.
- `FAILED` has at least one stable reason. A read-only failure has effect
  `NONE`; every failed or inconclusive pending mutation has effect `UNKNOWN`
  and `human_intervention_required=true`.
- Evidence references are unambiguous: Invocation IDs are unique in the
  cumulative report. Runtime rejects a duplicate before evaluation.
- RuntimeOutcome binds every ordered check identity and every report result's
  exact evidence reference and result Hash. Missing, reordered, or re-signed
  closure data is rejected.
- Runtime passes only strict rebuilt copies of Plan, results, and context to an
  injected Verifier; mutation of those copies cannot change retained Runtime
  authority. The returned result is strictly rebuilt and independently
  recomputed.
- `VERIFIER_FAILED` means the Verifier threw or could not complete.
  `VERIFIER_RESULT_INVALID` means the returned type, structure, binding, order,
  Hash, closure, or independently recomputed result was invalid.
- If Contract-required evidence acquisition fails before Verifier runs,
  Runtime fabricates no VerificationResult and applies the same conservative
  effect closure: `NONE` for read-only work, `UNKNOWN` plus human intervention
  for any pending mutation.
- A verification failure stops Runtime; Verifier cannot initiate retry,
  request/initiate/execute recovery, or rollback. It can only report that a
  human should consider recovery.
- Every verification failure enters `FAILED`. If a mutation was dispatched and
  cannot be verified, the structured outcome marks the remote effect unknown
  and requires human intervention.
- Verification evidence and reasons are structured and redacted. The terminal
  CLI Review displays the complete exact criterion payload, while
  `verification_audit` contains only IDs, hashes, status, stable reasons,
  effect/human closure, and check/reference metadata—never raw evidence.

### Test Requirements

- Tests cover strict success/failure for all four evaluator kinds, including
  type coercion rejection, finite/inclusive numeric bounds, missing and
  duplicate service names, utilization caps, and healthy/unhealthy outcomes.
- Tests cover multiple criteria, target mismatch, stale evidence, missing
  evidence, malformed evidence, contradictory evidence, and all-pass success.
- Tests reject an unknown check ID, unplanned evidence, reordered evidence, or
  evidence for a different Tool version, Plan digest, Attempt, or report Hash.
- Freshness tests use an injected clock and prove the same single sample binds
  acceptance/evaluation, collection duration is the capped maximum of Executor
  and rounded-up Runtime elapsed evidence, the boundary is inclusive, and
  invalid/future timestamps fail closed.
- Plan/Approval tests prove every Plan requires criteria, v2 Hashes change with
  criterion content/order, and v1 snapshots or changed criteria cannot reuse
  approval.
- Contract/Plan tests prove current `get_system_status` is called once, unused
  or undeclared/non-read-only/L3 `VERIFY` Steps fail, every required reference
  is criterion-bound, multiple mutation Steps fail, provenance-only mutation
  criteria fail, and a mutating Tool without meaningful independent read-only
  verification is rejected before dispatch.
- Runtime integration tests cover `EXECUTING → VERIFYING → COMPLETED` and
  verification failure to `FAILED` for non-mutating work, plus
  `PENDING_VERIFICATION → VERIFIED` and failed mutation to
  `UNKNOWN`/human-intervention closure.
- Hostile result tests reject forged pass status, content Hash drift, missing or
  reordered checks, duplicate Invocation IDs, inconsistent effect/human flags,
  mutated injected inputs, malformed or rebound evidence references, and a
  Verifier exception. They assert distinct `VERIFIER_FAILED` and
  `VERIFIER_RESULT_INVALID` reasons, hash-only audit content, and full terminal
  CLI criterion display. A dedicated test proves Runtime rejects a well-formed,
  Hash-valid forged `PASSED` by independently recomputing the expected result.
- Import/boundary tests prove Verifier cannot dispatch Tools or request
  recovery.

### Out of Scope

LLM-based verification, direct Tool calls, automatic retry, rollback,
Incident persistence, mutating verification actions, multiple mutating source
Steps, and implicit action-to-criterion/effect inference.

## Phase 7 — Local Model Adapter

### Goal

Use a replaceable local model for planning while keeping model output outside
all authority and execution boundaries.

### Inputs

- RuntimeContext.
- A read-only view of allowed Tool names, versions, argument schemas, and
  descriptions.
- A local Ollama-compatible or OpenAI-compatible endpoint configuration.
- Response size and timeout limits.

### Outputs

- A `ModelPlanDraft` validated by Pydantic, followed by a Runtime-normalized
  ExecutionPlan with authoritative Tool Metadata.
- Structured adapter errors for timeout, connection, response size, malformed
  JSON, schema failure, and unknown Tool.

### Deliverables

- One local HTTP adapter with compatible request modes rather than
  provider-specific authority logic.
- A strict ModelPlanDraft schema that contains no risk, Policy, or Approval
  fields and rejects extra fields.
- Planner integration that validates the selected Tool/version and
  deterministically adds read-only resolved risk from Tool Metadata before
  Policy.
- Loopback-only endpoint validation for this phase.
- Disabled HTTP redirects and environment proxy use, input redaction, and
  strict response-size limits.

### Acceptance Criteria

- The model receives no credentials, approval records, Tool callbacks, or
  Executor reference.
- Only loopback endpoints are accepted in this phase; redirects, environment
  proxies, and non-loopback destinations are rejected.
- Risk, Policy, or Approval fields are not part of the model wire schema; their
  presence is an extra-field validation error. Registered Tool Metadata and
  Policy are authoritative.
- Unknown Tools, invalid arguments, excessive output, and malformed responses
  fail closed.
- Changing model provider does not change permissions.
- No third-party API is required for normal operation.

### Test Requirements

- Fake local HTTP tests cover both compatible request modes.
- Tests cover valid plans, timeout, refusal, connection failure, malformed JSON,
  schema violation, unknown Tool, risk forgery, excessive output, and endpoint
  validation.
- Tests cover redirect rejection, environment proxy isolation, extra
  security-sensitive fields, and redaction before prompt construction.
- No real model or Internet access is required in the default test suite.

### Out of Scope

Cloud model APIs, API keys, remote provider fallback, model training, Tool
calling by the model, autonomous approval, autonomous execution, and Internet
search.

## Phase 8 — SSH Read Only

### Goal

Acquire remote Linux evidence through typed, narrowly scoped, read-only Tools
without exposing SSH or command execution to Planner or users.

### Inputs

- Phase 2 Tool Protocol.
- Phase 3 Policy, Phase 5 Executor, and Phase 6 verification contracts.
- An allowlisted `target_id` bound locally to host, port, user, and trusted
  host-key fingerprint.
- Authentication supplied only through an external SSH agent.
- Output size, timeout, and redaction limits.

### Outputs

- Typed system status, service status, network status, container status, and
  redacted log evidence.
- Structured connection and read-operation errors.

### Deliverables

- An AsyncSSH transport internal to the Tool layer.
- Typed read-only Tools with fixed operation identifiers and strict argument
  validation.
- Normative Context Profile and Context Profile Registry Record schemas.
- Target, BootstrapContext, Context Profile Registry, and deterministic
  ObservationRequest contracts.
- Linked, non-recursive `OBSERVATION` Tasks that traverse the complete Runtime
  lifecycle; no hidden Tool execution inside parent `CONTEXT_BUILDING`.
- Mandatory host-key verification, connection timeout, operation timeout,
  output limits, and redaction.
- No public `exec`, command string, session, or raw SSH client API.

### Acceptance Criteria

- Every remote read is Policy-checked and dispatched only by Executor.
- Every remote context read belongs to a linked `OBSERVATION` Task with an
  exact Profile ID, Version, Hash, Target, Arguments, Plan Hash, and terminal
  structured result.
- L1 observation work pauses in its child `WAITING_FOR_APPROVAL` state when
  Policy requires Human Execution Approval; the parent never bypasses it.
- An Observation Task cannot create another Observation Task.
- Only a fixed allowlist of L0/L1 read operations can reach the transport.
- System, service, network, and container status Tools are L0; bounded log reads
  are L1 and require an explicit L1 allow rule.
- Each Tool owns a source-defined fixed remote operation and typed parameters;
  no generic command, argv, flags, or Shell surface exists.
- Planner and model output can select only an allowed `target_id`; they cannot
  provide host, user, port, or executable material.
- Strict known-host verification is mandatory, TOFU is forbidden, and
  host-key verification cannot be disabled through normal configuration.
- Runtime neither reads nor persists private keys or passwords.
- Credentials and secret-like output never enter logs, ToolResults, plans, or
  Memory.
- No read-only Tool can change remote state.

### Test Requirements

- Fake transport or local isolated SSH-server tests cover successful reads,
  DNS/connection failure, authentication failure, host-key mismatch, timeout,
  output truncation, invalid encoding, allowlist rejection, and redaction.
- Tests cover unknown target IDs, attempted connection-field overrides, TOFU
  rejection, absence of SSH agent, and fixed-operation enforcement.
- Tests cover Profile Schema/Hash drift, deterministic Profile selection,
  ambiguous selection, linked lifecycle order, L0 `NOT_REQUIRED`, L1 approval
  pause, child failure, parent evidence handoff, and recursion rejection.
- Injection-oriented tests cover host, service, container, and log selector
  arguments.
- Policy tests cover every L0/L1 Tool.
- Real-server integration tests require an explicit opt-in marker and isolated
  test target.

### Out of Scope

Remote writes, restart, file transfer, arbitrary Shell, package management,
credential persistence, private-key inspection, Docker mutation, Kubernetes,
and production-server testing by default.

## Phase 9 — Incident Memory

### Goal

Persist redacted operational experience for future context without allowing
Memory to influence authority.

### Inputs

- Completed or failed Task lifecycle events.
- ExecutionPlan and Approval references.
- ToolResults and VerificationResult.
- Redacted operator, target, evidence, and lessons.

### Outputs

- Durable IncidentRecords.
- Read-only incident queries suitable for Context Builder.
- Persisted, non-authoritative Approval audit projections, migration metadata,
  and retention metadata.
- An Approval-owned AuthorizationJournal and Runtime/Executor-owned
  ExecutionJournal for authoritative durable recovery facts.

### Deliverables

- SQLite database through SQLAlchemy and Alembic migrations. Incident Memory,
  AuthorizationJournal, and ExecutionJournal may share that physical database
  but use separate logical schemas, repositories, owners, and dependency
  directions.
- An initial documented Alembic migration and migration workflow.
- Append-only repository methods for create, get by ID, and bounded
  recent-history query; no update or delete business API.
- An Approval-owned append-only AuthorizationJournal for `ISSUED`,
  `CONFIRMED`, `CONSUMED`, `REJECTED`, `INVALIDATED`, and `EXPIRED` events.
  Only Approval may reconstruct authorization from this journal; Incident
  Memory receives a redacted, non-authoritative audit projection.
- A Runtime/Executor-owned append-only ExecutionJournal for
  `ATTEMPT_STARTED`, `DISPATCH_INTENT`, `TOOL_RESULT`,
  `VERIFICATION_RESULT`, `ATTEMPT_CLOSED`, and bounded
  `ATTEMPT_CLOSURE_UNCONFIRMED` evidence; Incident Memory separately records
  `INCIDENT_FINALIZED` and redacted projections.
- A unique execution-attempt key for idempotent local event recovery and
  ingestion only. It never authorizes or triggers another Tool dispatch.
- Transaction boundaries that prevent partially written incidents.
- Redaction before persistence and explicit prohibited-field validation.

### Acceptance Criteria

- Memory has no dependency path to PolicyDecision or Approval validation.
  Approval may read only its AuthorizationJournal, never Incident Memory.
- No credential, private key, password, token, or unredacted secret is stored.
- Incident records link Task, Plan, Approval when present, execution,
  verification, and lessons.
- Before any dispatch, one local transaction persists the Task and plan
  snapshot/hash, approval `CONSUMED` event, and `ATTEMPT_STARTED`. Failure rolls
  back locally, fails closed, and dispatches nothing.
- Before each mutating Tool call, `DISPATCH_INTENT` is committed. Tool result,
  verification result, and final Incident are appended in subsequent local
  transactions. The plan never claims database atomicity with a remote side
  effect.
- On recovery, a mutating `DISPATCH_INTENT` without a conclusive ToolResult is
  treated as an unknown outcome and requires manual intervention.
- Missing `ATTEMPT_CLOSED` evidence or persisted closure uncertainty must never
  be inferred as `NOT_DISPATCHED`; recovery preserves `UNKNOWN` whenever a
  dispatch-capable boundary may have been called.
- Phase 9 enables cross-process pre-dispatch resume only when Approval
  reconstructs from its own AuthorizationJournal an unexpired, unconsumed
  record that still matches the canonical Plan, current Policy, and frozen Tool
  Metadata.
- After consumption, only the exactly bound execution-attempt ID may be
  recovered, and only while Approval remains unexpired, Plan/Policy/Metadata
  all revalidate, and no mutating `DISPATCH_INTENT` has an unknown outcome. It
  cannot start another attempt or automatically redispatch a mutating Tool.
- Database writes occur only through the Storage boundary, with authority
  preserved by the separate owning repositories.
- Every schema change requires migration, documentation, and tests.
- Historical context is bounded and treated as untrusted facts, not authority.
- Persisted RuntimeOutcome and Incident projections are never accepted as
  authenticated authority merely because their ordinary content hashes match.
  Recovery revalidates semantic bindings against the owner journals; the
  storage integrity design must detect coordinated record rewriting before
  any persisted verification fact is treated as trustworthy.
- If otherwise verified work cannot be persisted, the transaction rolls back,
  Runtime enters `FAILED`, and a structured storage error records that the
  remote effect was verified but the local evidence commit failed. There is no
  silent retry; idempotent local event recovery uses the unique attempt key to
  prevent duplicate records and never implies remote replay.

### Test Requirements

- Temporary SQLite tests cover initial migration, append, retrieval, bounded
  history, foreign-key relationships, transaction rollback, prohibited fields,
  redaction, and migration replay.
- Tests prove append-only behavior, unique-attempt handling, no update/delete
  business API, and the persistence-failure transition to `FAILED` with an
  explicit evidence-persistence disposition.
- Crash-recovery tests cover `ATTEMPT_STARTED` without dispatch,
  `DISPATCH_INTENT` without result, result without final Incident, and
  idempotent local event recovery without duplicate records or remote
  redispatch.
- Tests cover Approval-owned journal reconstruction, cross-process expiry,
  Plan/Policy/Metadata drift, consumed approval rejection, unknown dispatch
  intent, and pre-execution authorization-storage failure.
- Tests prove Memory content cannot change Policy outcomes.
- Concurrency is not claimed or tested beyond SQLite's selected transaction
  model.

### Out of Scope

Candidate Skill generation or enablement, Incident deletion, vector search,
cloud sync, remote databases, multi-user tenancy, Memory-based permissions, and
secret storage.

## Phase 10 — Restart Service

### Goal

Restart exactly one approved and allowlisted systemd service, then verify it
with fresh independent evidence.

### Inputs

- Completed Phases 2–6, 8, and 9.
- An L2 ExecutionPlan containing one typed restart step and its verification
  step.
- A valid exact-plan ApprovalRecord.
- The Phase 8 SSH transport.
- A target-specific service allowlist.
- Fresh pre-operation service status.

### Outputs

- A structured restart ToolResult.
- Fresh service status and configured health evidence.
- VerificationResult, lifecycle events, and IncidentRecord.
- Recovery guidance if verification fails.

### Deliverables

- A versioned, typed, explicitly non-idempotent `restart_service` Tool with one
  fixed transport operation and no generic systemd surface.
- Strict service-name schema and allowlist validation.
- Pre-operation state capture through a read-only Tool.
- Post-operation service-status and start-identity reads dispatched through
  Executor.
- A documented manual-intervention failure path.
- A pre-implementation reconciliation gate for `docs/TOOL_SPEC.md`: its current
  `Idempotent: true` and `Restart previous service state` statements conflict
  with the confirmed non-idempotent, no-automatic-rollback design. The
  governing spec must be updated or re-confirmed before Phase 10 code begins.

### Acceptance Criteria

- Risk is statically L2 in Tool Metadata.
- Policy, plan hash, Tool version, exact arguments, verification criteria, and
  unexpired approval are revalidated immediately before dispatch.
- Only one allowlisted service can be addressed; arguments cannot inject or
  append commands.
- Fresh pre-state must be `active`; otherwise no mutation occurs and Runtime
  requires a new plan.
- Pre-state, execution result, and fresh post-state evidence are recorded.
- Success requires `active` plus a changed systemd InvocationID or equivalent
  start identity relative to pre-state.
- Verifier does not perform I/O.
- A rejection or definite failure before dispatch enters `FAILED` with no
  remote mutation. Once restart has been dispatched, failure, timeout, unknown
  outcome, or failed verification also enters `FAILED`; its structured outcome
  marks the possible remote effect and human intervention requirement, with
  redacted recovery guidance.
- Executor dispatches restart exactly once and never retries it.
- No automatic rollback or false claim that a restart can be undone is made.
  Pre-state is evidence for human recovery guidance, not an automatic action.

### Test Requirements

- Fake transport tests cover restart success, service not found, permission
  denial, timeout, transport failure, and malformed response.
- Security tests cover invalid names, separators, whitespace, Unicode edge
  cases, and allowlist bypass attempts.
- Approval tests cover absent, expired, wrong-target, changed arguments,
  changed Tool version, and changed verification criteria.
- Verification tests cover active/inactive state, unchanged start identity,
  stale evidence, one-dispatch enforcement, pre-dispatch failure, and
  manual-intervention transition.
- Real-server tests are opt-in and use an isolated disposable service only.

### Out of Scope

Arbitrary `systemctl`, start/stop/enable/disable, bulk services, deployment,
package or configuration changes, deletion, permission changes, automatic
rollback, and default production testing.

## Phase 11 — Restart Container

### Goal

Restart exactly one approved and allowlisted Docker container, then verify its
state and configured health without exposing Docker or Shell authority.

### Inputs

- Completed Phases 2–6 and 8–10, including the fixed mutating-Tool safety
  pattern from Phase 10.
- An L2 ExecutionPlan containing one typed container restart and verification
  steps.
- A valid exact-plan ApprovalRecord.
- The controlled Phase 8 SSH transport.
- A target-specific container allowlist, approved container name, and approved
  immutable container ID.
- Fresh pre-operation container status.

### Outputs

- A structured restart ToolResult.
- Fresh container state and health evidence.
- VerificationResult, lifecycle events, and IncidentRecord.
- Recovery guidance on failure.

### Deliverables

- A versioned, typed `restart_container` Tool using one fixed internal transport
  operation and no generic Docker surface.
- Strict container identity and timeout schemas.
- Pre-operation status capture.
- Separate read-only status and health Tools dispatched by Executor.
- A documented manual-intervention path.

### Acceptance Criteria

- Risk is statically L2 in Tool Metadata.
- No Docker API, raw Shell, raw Docker command, or client object is public.
- Plan Hash binds target, container name, approved immutable container ID, Tool
  version, exact arguments, and verification criteria.
- Immediately before dispatch, Executor resolves identity again. An ID mismatch
  fails closed with no mutation and requires a new plan and approval.
- Policy and exact-plan approval are revalidated immediately before dispatch.
- Executor dispatches restart exactly once. Bounded read-only verification may
  poll only as declared in the plan and can never dispatch another restart.
- Success requires the same container ID, `running`, a newer `StartedAt`, and
  `healthy` when a health check is declared. If required health evidence does
  not exist, verification fails closed.
- A rejection or definite failure before dispatch enters `FAILED` with no
  mutation. Once dispatched, failure, timeout, unknown identity/outcome, or
  failed verification also enters `FAILED`; its structured outcome marks the
  possible remote effect and human intervention requirement. No automatic
  retry or rollback is attempted.
- Remote permissions are pre-provisioned as a precise minimal capability;
  Runtime never grants docker-group membership or changes server permissions.

### Test Requirements

- Fake transport tests cover success, missing container, stopped pre-state,
  timeout, transport failure, restart failure, unhealthy result, and malformed
  status.
- Security tests cover identity injection, ambiguous names, Unicode edge cases,
  whitespace, and allowlist bypass.
- Approval tests cover absent, expired, tampered target, arguments, Tool
  version, and verification criteria.
- Runtime tests cover exact state/event order and manual intervention.
- Tests cover immutable-ID drift, unchanged `StartedAt`, required-but-missing
  health evidence, one restart dispatch, bounded read polling, and no retry.
- Real-server tests are opt-in and use an isolated disposable container only.

### Out of Scope

Docker Engine API exposure, Compose, Swarm, bulk restart, image pull/build,
deployment, deletion, volume or network changes, Kubernetes, automatic
rollback, remote permission configuration, and default production testing.

## 3. Cross-phase Definition of Done

A phase is complete only when:

1. Its Deliverables exist and no Out of Scope capability has been introduced.
2. Every public API has type hints and a docstring.
3. All outputs are structured and validated.
4. Explicit failure behavior and stable errors exist.
5. Structured logging is present where execution occurs.
6. Verification and recovery or rollback consideration are documented.
7. Its Test Requirements pass.
8. `pytest`, Ruff, and mypy pass for all implemented code.
9. Security-sensitive negative tests pass.
10. Documentation reflects the implemented behavior.

No phase may be marked complete on the basis of a happy-path demonstration
alone.
