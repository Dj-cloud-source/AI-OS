# AIOps Agent Runtime Implementation Plan

Status: Phases 0–1 baselines implemented; approval-gate conformance remediation
and Phases 2–11 planned

## 1. Authority and scope

This plan is subordinate to the following governing documents:

1. `docs/VISION.md`
2. `docs/PHILOSOPHY.md`
3. `docs/ARCHITECTURE.md`
4. `docs/STATE_MACHINE.md`
5. `docs/TOOL_SPEC.md`
6. `AGENTS.md`

Implementation proceeds phase by phase. A later phase must not be used to
justify bypassing an earlier safety gate. Phases 0–1 implement only the local
Mock Runtime and its fail-closed lifecycle; they add no real server capability.

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
- Executor never plans or changes approved arguments.
- Context Builder and Verifier never invoke a Tool. Runtime sends their
  evidence requests through Policy and Executor.
- Tool never plans, approves, or performs policy decisions.
- Memory never affects Policy or Approval.

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

Verification reads are `ExecutionStep` entries with role `VERIFY`. Their Tool
name and version, exact arguments, order, and expected conditions are included
in the plan hash. Runtime asks Executor to run them during `VERIFYING`, then
passes their typed results to Verifier. Verifier never invents or dispatches a
verification call.

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
  role (`OBSERVE`, `ACTION`, or `VERIFY`), reason, impact, verification
  criteria, and recovery guidance.
- `ExecutionPlan`: schema version, plan ID, task ID, target, ordered immutable
  steps, and plan hash when canonicalized.
- `ToolMetadata`: Tool name, version, description, static RiskLevel, timeout,
  idempotency declaration, and argument/result model identifiers. Required
  approval mode is not independently configurable Metadata; Policy derives it
  from RiskLevel and the fixed risk matrix.
- `ToolCall`: immutable Tool name/version and validated typed arguments.
- `ToolError`: stable code, redacted message, and retryable information; it
  never grants retry permission.
- `ToolResult`: Tool identity, success flag, typed Tool-specific data, optional
  structured error, and non-negative duration. It is never a plain string.
- `PolicyDecision`: allow/deny, reason code, resolved risk, and required
  approval mode.
- `ApprovalRecord`: plan hash, exact ordered arguments, approver, approval mode,
  issued-at, and expires-at.
- `ManualConfirmationRecord`: Approval ID, plan hash, exact L3 step ID,
  canonical arguments hash, confirmer, execution-attempt ID, issued-at,
  short expires-at, and one-time consumption event.
- `ExecutionReport`: ordered calls, results, durations, dispatch certainty, and
  redacted audit references.
- `VerificationResult`: success, criteria, structured evidence references, and
  failure reasons.
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
| ToolCall and ToolError | Phase 2 |
| PolicyDecision | Phase 3 |
| ApprovalRecord | Phase 4 |
| ExecutionReport | Phase 5 |
| VerificationResult | Phase 6 |
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
display timestamps. Approval validation recomputes the hash immediately before
execution and before every mutating step, and also compares the stored ordered
arguments. Approval is consumed once execution dispatch begins. Every L3 Tool
invocation uses the same plan hash and requires its own separately timestamped,
single-use Manual Confirmation immediately before that exact Step is
dispatched; local single-user mode does not require a different second person.
It does not allow a changed Plan, Step, or Arguments.

Consumption binds the approval to one unique execution attempt. It remains
usable only by that in-progress attempt, subject to expiration and hash
revalidation, and can never start a second attempt.

### 2.7 Error and logging rules

- Domain failures use explicit exception types and stable error codes.
- Tool failures use a structured error containing a code, redacted message,
  and retryable flag. A retryable flag is information, not permission to retry.
- Dangerous operations are never silently retried.
- Logs are structured JSON and include Task ID, Plan ID, Approval ID when
  applicable, operator, target, Tool, redacted arguments, result status,
  duration, and verification status.
- Passwords, private keys, tokens, and secret-bearing raw output are never
  logged, persisted, or returned unredacted.
- Model events add token count and latency; unavailable provider metrics are
  recorded explicitly as unknown rather than invented.
- Size limits and redaction apply before data enters logs, model prompts,
  ToolResults, CLI output, or persistence.

## Phase 0 — Foundation

Status: Baseline implemented (2026-07-25); approval-gate conformance pending

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

Status: Baseline implemented (2026-07-25); approval-gate conformance pending

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

### Goal

Establish a small, typed protocol through which every operational capability is
described, validated, dispatched, and reported.

### Inputs

- Phase 1 Runtime orchestration.
- Phase 0 Tool Metadata and ToolResult models.
- The static L0 `get_system_status` Mock Tool.

### Outputs

- A stable Tool contract and deterministic Tool Gateway.
- Typed Tool-specific input and output models.
- Structured, redacted Tool errors.

### Deliverables

- One minimal Tool protocol with metadata and an invoke operation.
- Explicit Tool registration by immutable name and version.
- Input validation before dispatch and output validation after return.
- Stable errors for unknown Tool, duplicate registration, invalid arguments,
  timeout declaration violations, and malformed output.
- Migration of the Mock Tool to the final protocol.
- Gateway behavior limited to registration, exact resolution, input/output
  validation, and bounded invocation on Executor's request.

### Acceptance Criteria

- Tool Metadata owns RiskLevel; a plan may reference but cannot redefine it.
- The gateway rejects an unknown Tool name or version.
- Duplicate name/version registration fails at startup.
- Every invocation returns ToolResult with Tool-specific typed data or a
  structured error.
- Raw transport objects, callbacks, and command strings never cross the Tool
  boundary.
- Tool code never imports Planner, Approval, or Policy.
- Production code invokes a Tool only through Executor; direct invocation is
  limited to isolated Tool unit tests.

### Test Requirements

- Contract tests run against every registered Tool.
- Tests cover argument coercion rejection where it could weaken safety,
  malformed output, unknown versions, duplicate registration, and stable error
  codes.
- Tests prove risk cannot be overridden through ExecutionStep arguments.
- Redaction tests cover error messages and Tool-specific payloads.
- The Mock Tool remains deterministic and external-I/O-free.

### Out of Scope

Dynamic discovery, plugins, arbitrary user Tools, SSH, Docker, real timeouts,
remote execution, retries, and mutating operations.

## Phase 3 — Policy Engine

### Goal

Make all permission, allowlist, risk, and approval-requirement decisions
deterministic and independent of model reasoning.

### Inputs

- A validated ExecutionPlan.
- The immutable Tool Metadata catalog.
- A typed local operator context and target reference.
- Static policy configuration loaded and validated at startup.

### Outputs

- A PolicyDecision for the plan and each step.
- Stable `decision: ALLOW | DENY`, reason codes, and a separate
  `approval_requirement: NOT_REQUIRED | HUMAN_PLAN_APPROVAL`.
- For each L3 Step, a separate
  `manual_confirmation_requirement: PER_INVOCATION`; it is not a third
  Policy decision value.

### Deliverables

- A fail-closed Policy Engine.
- A fixed risk matrix:
  - L0: automatic when Tool and target are allowed.
  - L1: allowed or denied by explicit read policy.
  - L2: allowed only with exact-plan approval.
  - L3: allowed only with exact-plan approval and a per-invocation Manual
    Confirmation.
- Target and Tool allowlists.
- Validation that plan references match registered Tool Metadata.

### Acceptance Criteria

- Policy has no LLM or model adapter dependency.
- Tool risk is read from the registry, never accepted from model output.
- Resolved step risk is read-only, and plan risk is the highest resolved step
  risk.
- L1 requires an explicit allow rule; a missing rule denies rather than
  upgrading or guessing.
- Any unknown Tool, Tool version, target, risk, or policy field produces deny.
- A plan containing one denied step is denied as a whole.
- A plan containing any L2/L3 step cannot reach Executor without the required
  approval mode.
- The same inputs always produce the same PolicyDecision.

### Test Requirements

- A complete L0–L3 decision table.
- Tests for mixed-risk plans, unknown metadata, target denial, Tool denial,
  plan risk forgery, and malformed policy.
- Determinism tests repeat identical decisions.
- Negative tests confirm no model adapter is imported or invoked.
- Policy tests exist for every Tool added in later phases.

### Out of Scope

Approval issuance, Tool invocation, user-defined policy code, model-based risk
classification, RBAC, multi-user identity, and remote policy services.

## Phase 4 — Approval

### Goal

Create a transactional human authorization that applies only to one exact,
unexpired ExecutionPlan.

### Inputs

- A PolicyDecision requiring L2 or L3 approval.
- A validated ExecutionPlan and registered Tool Metadata.
- A local approver identity.
- An injectable timezone-aware UTC clock.

### Outputs

- An immutable ApprovalRecord.
- A structured approval validation result.
- Audit events for approve, reject, expire, invalidate, and per-Step L3
  confirmation.

### Deliverables

- Canonical plan serialization and SHA-256 hashing defined in section 2.6.
- Plan review data containing why, what, impact, verification, and recovery for
  every step.
- Approval issuance with explicit expiration.
- Approval revalidation immediately before Executor dispatch.
- A single-use L3 confirmation bound to the unchanged Plan, exact Step and
  Arguments immediately before each L3 invocation.
- One-time approval consumption when execution dispatch begins.
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
- Phase 4 storage and CLI interaction are in one process; persistence and
  cross-process resume are not introduced before Phase 9.

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
RBAC, external identity, approval delegation, and approval reuse.

## Phase 5 — Executor

### Goal

Execute validated plans exactly as approved while remaining incapable of
planning or changing authority.

### Inputs

- A validated ExecutionPlan.
- Its PolicyDecision.
- A valid ApprovalRecord when required.
- The deterministic Tool Gateway.
- An injectable monotonic timer.

### Outputs

- Ordered ToolResults.
- Structured execution events and a plan execution summary.
- A stopped failure outcome when a step fails.
- A structured ExecutionReport and next-state fact for Runtime.

### Deliverables

- Sequential execution in exact plan order.
- Immediate Policy and Approval revalidation before the first Tool and before
  each approval-sensitive step.
- Timeout enforcement at the Executor boundary.
- Stop-on-failure behavior with no silent retries.
- An evidence-request path used by Runtime for context and verification reads;
  it still passes through Policy and Executor.

### Acceptance Criteria

- Executor is the only module that imports and invokes Tool Gateway dispatch.
- Executor never creates steps, changes arguments, substitutes Tools, or
  changes order.
- Unknown, denied, expired, or tampered work is rejected before invocation.
- On a failed step, later steps do not run.
- Any definite step failure enters `FAILED`. The structured outcome preserves
  which earlier steps succeeded and whether they produced a confirmed effect.
- All successful action steps enter `VERIFYING`; Executor never returns
  `COMPLETED`.
- A retry requires a new explicit Runtime action; dangerous work is never
  retried internally.
- ToolResults and events contain redacted values only.

### Test Requirements

- Recording Fake Tools assert exact order and arguments.
- Tests cover no-approval, expired approval, hash mismatch, policy denial,
  unknown Tool, timeout, malformed result, mid-plan failure, and stop behavior.
- Tests distinguish first-step failure from a later failure by structured
  outcome facts while asserting both enter `FAILED`; successful execution
  enters only `VERIFYING`.
- Tests prove no invocation occurs before all preconditions pass.
- Tests prove Context Builder and Verifier receive evidence but never call the
  gateway.
- Tests confirm retryable errors do not cause automatic retries.

### Out of Scope

Parallel steps, background jobs, real remote transports, persistence, automatic
rollback, compensation, and mutating server Tools.

## Phase 6 — Verifier

### Goal

Evaluate whether the stated objective was achieved using fresh, structured
evidence without gaining execution authority.

### Inputs

- Approved ExecutionPlan `VERIFY` steps and their hashed verification criteria.
- Execution ToolResults.
- Fresh verification ToolResults acquired by Runtime through Policy and
  Executor in the declared step order.
- An injectable clock for evidence freshness.

### Outputs

- An immutable VerificationResult.
- Stable verification failure reasons.
- Structured per-check results and evidence references for Runtime; never a
  Tool call.

### Deliverables

- Deterministic criterion evaluators for equality, bounded numeric values,
  expected state, and health status needed by approved Tools.
- Evidence freshness and target-correlation checks.
- Missing, malformed, stale, and contradictory evidence handling.
- Runtime integration for `VERIFYING`.

### Acceptance Criteria

- Verifier has no Tool Gateway, transport, Planner, or Approval dependency.
- Every verification Tool, version, argument, order, and expected condition was
  declared in ExecutionPlan and included in its approval hash.
- Success requires every mandatory criterion to pass against the correct target
  and sufficiently fresh evidence.
- Missing, stale, malformed, or contradictory evidence produces failure.
- A verification failure stops Runtime; it cannot initiate retry or rollback.
- Every verification failure enters `FAILED`. If a mutation was dispatched and
  cannot be verified, the structured outcome marks the remote effect unknown
  and requires human intervention.
- Verification evidence and reasons are structured and redacted.

### Test Requirements

- Tests cover each evaluator, multiple criteria, target mismatch, stale
  evidence, missing evidence, malformed evidence, contradictory evidence, and
  all-pass success.
- Tests reject an unknown check ID, unplanned evidence, reordered evidence, or
  evidence for a different Tool version.
- Runtime integration tests cover `EXECUTING → VERIFYING → COMPLETED` and
  verification failure to `FAILED` for non-mutating work.
- Import/boundary tests prove Verifier cannot dispatch Tools.

### Out of Scope

LLM-based verification, direct Tool calls, automatic retry, rollback,
Incident persistence, and mutating verification actions.

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
- Persisted approval and audit references, migration metadata, and retention
  metadata.

### Deliverables

- SQLite database through SQLAlchemy and Alembic migrations.
- An initial documented Alembic migration and migration workflow.
- Append-only repository methods for create, get by ID, and bounded
  recent-history query; no update or delete business API.
- Append-only Approval audit events (`ISSUED`, `CONFIRMED`, `CONSUMED`,
  `REJECTED`, `INVALIDATED`, and `EXPIRED`) so effective approval state can be
  reconstructed without mutating history.
- Append-only execution events including `ATTEMPT_STARTED`, `DISPATCH_INTENT`,
  `TOOL_RESULT`, `VERIFICATION_RESULT`, and `INCIDENT_FINALIZED`.
- A unique execution-attempt key for idempotent explicit replay.
- Transaction boundaries that prevent partially written incidents.
- Redaction before persistence and explicit prohibited-field validation.

### Acceptance Criteria

- Memory has no dependency path to PolicyDecision or Approval validation.
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
- Phase 9 enables cross-process pre-dispatch resume only when the reconstructed
  approval is unexpired, unconsumed, and still matches the canonical plan.
  After consumption, only the bound execution-attempt ID may be recovered; it
  cannot start another attempt.
- Database writes occur only in the Memory/Storage boundary.
- Every schema change requires migration, documentation, and tests.
- Historical context is bounded and treated as untrusted facts, not authority.
- If otherwise verified work cannot be persisted, the transaction rolls back,
  Runtime enters `FAILED`, and a structured storage error records that the
  remote effect was verified but the local evidence commit failed. There is no
  silent retry; an explicit replay uses the unique attempt key to prevent
  duplicates.

### Test Requirements

- Temporary SQLite tests cover initial migration, append, retrieval, bounded
  history, foreign-key relationships, transaction rollback, prohibited fields,
  redaction, and migration replay.
- Tests prove append-only behavior, unique-attempt handling, no update/delete
  business API, and the persistence-failure transition to `FAILED` with an
  explicit evidence-persistence disposition.
- Crash-recovery tests cover `ATTEMPT_STARTED` without dispatch,
  `DISPATCH_INTENT` without result, result without final Incident, and explicit
  replay without duplicate records.
- Tests cover approval-event reconstruction, cross-process expiry, consumed
  approval rejection, and pre-execution approval-storage failure.
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
