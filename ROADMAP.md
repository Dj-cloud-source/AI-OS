# AIOps Agent Runtime Roadmap

Status: Phases 0–1 baselines implemented; approval-gate conformance remediation
and Phases 2–11 planned

This roadmap defines the implementation order for the local-first AIOps Agent
Runtime. It is subordinate to `docs/VISION.md`, `docs/PHILOSOPHY.md`,
`docs/ARCHITECTURE.md`, `docs/STATE_MACHINE.md`, `docs/TOOL_SPEC.md`, and
`AGENTS.md`.

Phases 0–1 contain only the approved local Mock Runtime and its fail-closed
lifecycle. Phases 2–11 remain planned; no SSH, model, database, container, or
real server capability is enabled by the current implementation.

## Fixed architecture decisions

- The Python project uses Python 3.12+ and the `src/ai_server` layout.
- `WAITING_FOR_APPROVAL` is the canonical approval state name.
- Every Policy-allowed Plan passes through `WAITING_FOR_APPROVAL`; an audited
  `NOT_REQUIRED` decision exits it immediately without human input.
- Risk levels come only from immutable Tool Metadata.
- Only Executor may invoke a Tool. Context Builder and Verifier consume
  structured evidence and never invoke Tools directly.
- Approval binds the complete plan hash, ordered steps, Tool versions,
  arguments, verification criteria, approver, and expiration.
- L2 requires explicit plan approval. Every L3 Tool invocation additionally
  requires a single-use confirmation bound to the same Plan, exact Step and
  Arguments immediately before dispatch.
- A restart cannot be truthfully undone. Failed restart verification stops
  execution and requires manual intervention; it never triggers an automatic
  rollback.
- The authoritative execution flow is
  `Runtime → Policy → Approval Decision → Executor → Tool Gateway → Tool
  → Runtime → Verifier`.
- Verification reads are declared as typed steps in the approved plan. Their
  Tool versions, arguments, order, and expected conditions are included in the
  plan hash; Executor obtains the evidence and Verifier only evaluates it.
- Phase 0–7 Context uses Task data, static local configuration, and supplied
  evidence only. Before Phase 8 remote context is enabled, Runtime adds
  versioned Context Profiles and deterministic ObservationRequests. Each
  remote collection runs as a linked non-recursive `OBSERVATION` Task through
  the full Runtime lifecycle; Context Builder never selects or executes it.
- Diagnosis is Planner explanation recorded as reason and evidence linkage, not
  a separate authority-bearing component. Review and Commit are Approval events
  represented by `WAITING_FOR_APPROVAL`.
- Planner may select a Tool and arguments, but any resolved step risk is a
  read-only value derived by Runtime from Tool Metadata. Plan risk is the
  highest resolved step risk.

## Cross-phase failure rules

- Only Runtime changes task state.
- Denial, rejection, or a definite failure before any Tool succeeds ends in
  `FAILED`.
- A definite failure after one or more earlier steps succeeded also ends in
  `FAILED`; the structured outcome records prior successes and confirmed
  partial effects.
- Once a mutating Tool has been dispatched, timeout, unknown outcome, or failed
  verification ends in `FAILED`; the structured outcome marks the effect
  unknown and human intervention required.
- `PARTIAL_SUCCESS`, `ROLLBACK`, and `MANUAL_INTERVENTION_REQUIRED` are reserved
  and unreachable through Phase 11. Making one reachable requires a prior
  governance revision.
- Rollback is a separate governed Task and Plan, never an implicit transition.
- `COMPLETED` is reachable only after all required verification succeeds.

## Phase order and gates

Phases are implemented in numeric order. A phase may start only when every
Acceptance Criteria and Test Requirements item from the previous phase passes.
Real server access begins only in Phase 8. Mutating server operations begin only
in Phase 10.

## Phase 0 — Foundation

Status: Baseline implemented (2026-07-25); approval-gate conformance pending

### Goal

Create an installable, local-only Python foundation and a minimal mock vertical
slice.

### Inputs

The six governing documents, the fixed decisions above, and Python 3.12+.

### Outputs

A typed package, two safe CLI commands, core data models, a complete Runtime
state enum, one L0 Mock Tool, and a deterministic happy-path Runtime.

### Deliverables

`pyproject.toml`, the `src/ai_server` package skeleton, `ai-server version`,
`ai-server doctor`, Task, ExecutionPlan, ExecutionStep, ToolResult,
`get_system_status`, and tests.

### Acceptance Criteria

The governed runnable flow is
`RECEIVED → CONTEXT_BUILDING → PLANNING → POLICY_CHECK → WAITING_FOR_APPROVAL
→ EXECUTING → VERIFYING → COMPLETED`. For L0, `WAITING_FOR_APPROVAL` records
`NOT_REQUIRED` and exits immediately without human input.
All public APIs have type hints and docstrings. No code performs network,
server, shell, container, database-write, or model operations.

### Test Requirements

Unit and integration tests cover the CLI, models, state definitions and valid
transitions, Mock Tool, L0 Policy decision, and exact Runtime state history.
`pytest`, `ruff check`, `ruff format --check`, and strict `mypy` pass.

### Out of Scope

Real approval behavior, persistence, LLMs, SSH, Docker, arbitrary Shell,
server mutation, Web UI, multi-user, multi-agent, and cloud features.

## Phase 1 — Runtime

Status: Baseline implemented (2026-07-25); approval-gate conformance pending

### Goal

Make task lifecycle and component orchestration explicit, deterministic, and
fail-closed.

### Inputs

The Phase 0 models, state enum, Mock Tool, and vertical slice.

### Outputs

A Runtime Engine with validated transitions, immutable state history, pure
context construction, structured outcomes, and explicit failures.

### Deliverables

The Runtime orchestration service, transition rules, Runtime Context model,
structured lifecycle events, and domain exceptions.

### Acceptance Criteria

Runtime is the only application entry point. Planner cannot execute, Executor
cannot plan, Policy cannot call an LLM, and Context Builder cannot invoke a
Tool. Invalid transitions and unknown work fail closed. Unrecoverable errors
enter `FAILED`. Every allowed Plan enters `WAITING_FOR_APPROVAL`; L0 records
`NOT_REQUIRED` and passes through immediately, while human-approval-required
work cannot resume before Phase 4. `PARTIAL_SUCCESS`, `ROLLBACK`, and
`MANUAL_INTERVENTION_REQUIRED` remain reserved and reject undefined
transitions.

### Test Requirements

Table-driven tests cover every allowed and rejected transition, terminal-state
immutability, exact event ordering, failure propagation, and component boundary
violations. The L0 history includes
`POLICY_CHECK → WAITING_FOR_APPROVAL → EXECUTING`, and direct bypass is
rejected.

### Out of Scope

Real remote evidence, scheduling, workflows, persistence, approval storage,
and concurrent execution.

## Phase 2 — Tool Protocol

### Goal

Define the single typed and auditable boundary for operational capabilities.

### Inputs

Phase 1 Runtime contracts, core models, and the L0 Mock Tool.

### Outputs

Validated Tool Metadata, arguments, structured results, structured errors, and
a deterministic Tool Gateway.

### Deliverables

The minimal Tool protocol, Tool Metadata schema, immutable RiskLevel,
ToolResult envelope, error codes, timeout metadata, and explicit registration.

### Acceptance Criteria

Arguments are validated before dispatch. Unknown Tools are rejected. Planner
cannot supply or override risk. A Tool never returns a top-level free string,
plans, makes policy decisions, or exposes a raw command interface. Tool Gateway
only registers, resolves, and validates Tools; it does not perform Policy,
Approval, or planning.

### Test Requirements

Tests cover valid and invalid arguments, schema serialization, immutable
metadata, unknown Tools, structured failures, redaction boundaries, and
deterministic lookup.

### Out of Scope

SSH, Docker, remote execution, dynamic plugins, arbitrary Shell, and mutating
Tools.

## Phase 3 — Policy Engine

### Goal

Implement deterministic permission, allowlist, risk, and approval-requirement
decisions.

### Inputs

An ExecutionPlan, Tool Metadata catalog, target identity, and local operator
context.

### Outputs

A structured, explainable PolicyDecision for every plan and step.

### Deliverables

The Policy Engine, allowlist rules, L0–L3 decision matrix, denial reasons, and
fail-closed defaults.

### Acceptance Criteria

Policy never calls an LLM. L0 is automatic, L1 is policy-controlled, L2
requires approval, and L3 requires approval plus a per-invocation Manual
Confirmation. Missing metadata, an unknown Tool, a missing L1 rule, or a
disallowed target is denied. Plan risk is the highest resolved step risk.

### Test Requirements

An exhaustive risk matrix covers allow, deny, approval-required, metadata
tampering, unknown Tools, disallowed targets, and deterministic repeatability.

### Out of Scope

Approval records, Tool execution, dynamic policy languages, RBAC, multi-user,
and model-based risk classification.

## Phase 4 — Approval

### Goal

Implement exact-plan approval that expires and cannot authorize a changed plan.

### Inputs

A PolicyDecision, canonical ExecutionPlan, local approver identity, and UTC
clock.

### Outputs

An ApprovalRecord and a structured validation result.

### Deliverables

Canonical plan serialization, SHA-256 plan hashing, approval creation,
validation, one-time consumption, expiration, invalidation, per-Step L3
confirmation, and an in-process CLI Review/Commit path.

### Acceptance Criteria

Approval binds the ordered steps, Tool names and versions, exact arguments,
target, verification criteria, approver, and expiration. Any execution-relevant
change invalidates approval. Expired, consumed, rejected, or mismatched approval
is rejected. Phase 4 Review/Commit occurs in one CLI process; cross-process
resume is deferred until Phase 9. Consumption binds approval to one execution
attempt and cannot authorize a retry or second attempt.

### Test Requirements

Tests cover hash determinism, step order, argument mutation, Tool version
changes, target changes, verification changes, expiration, L2 approval, and
single-use per-Step L3 confirmation.

### Out of Scope

Database persistence, Web approval, multi-user approval, dual control, RBAC,
and remote identity providers.

## Phase 5 — Executor

### Goal

Make Executor the only component capable of invoking an approved Tool.

### Inputs

An ExecutionPlan, PolicyDecision, required ApprovalRecord, and Tool Gateway.

### Outputs

Ordered ToolResults and structured execution events.

### Deliverables

Sequential dispatch, pre-execution policy and approval revalidation, timeout
handling, stop-on-failure behavior, and in-memory audit evidence.

### Acceptance Criteria

Executor never plans, changes arguments, bypasses Policy, or silently retries a
dangerous operation. L2/L3 steps require valid approval. Context and
verification evidence requests also pass through Policy and Executor. Every
definite failure enters `FAILED`; structured outcome facts distinguish an
early failure from failure after prior success. Full execution success enters
`VERIFYING`, never directly `COMPLETED`.

### Test Requirements

Fake Tool tests cover order, timeouts, missing approval, plan tampering, unknown
Tools, mid-plan failure, stop-on-failure, and absence of silent retries.

### Out of Scope

Real transports, SSH, parallel execution, automatic rollback, persistence, and
remote server mutation.

## Phase 6 — Verifier

### Goal

Determine whether execution achieved the stated goal using structured evidence.

### Inputs

ExecutionPlan verification criteria, ToolResults, and verification evidence
from typed verification steps already included in the approved plan and
obtained through Executor.

### Outputs

A structured VerificationResult with evidence and failure reasons.

### Deliverables

Pure deterministic verifiers, evidence requirements, missing-evidence handling,
and Runtime integration.

### Acceptance Criteria

Verifier never invokes a Tool, plans, changes permissions, or executes recovery.
Missing, stale, malformed, or contradictory evidence cannot produce success.
A failed verification stops Runtime and enters `FAILED`. If a mutating
operation was dispatched and cannot be verified, the structured outcome marks
the remote effect unknown and requires human intervention.

### Test Requirements

Tests cover pass, fail, missing evidence, malformed evidence, contradictory
evidence, multiple steps, and confirmation that Verifier has no Tool Gateway
dependency.

### Out of Scope

LLM verification, direct Tool calls, Incident persistence, automatic recovery,
and rollback execution.

## Phase 7 — Local Model Adapter

### Goal

Allow a replaceable local model to produce draft plans without gaining
authority or execution capability.

### Inputs

Runtime Context, the permitted Tool catalog, and local endpoint configuration.

### Outputs

A Pydantic-validated ExecutionPlan whose risk data is deterministically
enriched from Tool Metadata.

### Deliverables

An Ollama/OpenAI-compatible local HTTP adapter, structured response parsing,
timeouts, and Planner integration.

### Acceptance Criteria

Only loopback endpoints are accepted by default; redirects and environment
proxies are disabled. The model cannot execute Tools, change Policy, approve
plans, or define risk. The model schema contains no risk, Policy, or Approval
fields; their presence is rejected rather than ignored. Invalid JSON, excessive
responses, unknown Tools, and metadata conflicts are rejected. Context is
redacted before model input.

### Test Requirements

Fake HTTP tests cover valid plans, malformed responses, timeouts, unknown Tools,
risk forgery, unavailable endpoints, and compatible-provider replacement.

### Out of Scope

Third-party cloud APIs, API keys, model training, autonomous execution, remote
models by default, and Internet search.

## Phase 8 — SSH Read Only

### Goal

Collect remote Linux evidence through narrowly scoped, policy-controlled,
read-only Tools.

### Inputs

Executor, Policy, Tool Protocol, target configuration, credentials supplied
through an external SSH agent, and trusted SSH host keys.

### Outputs

Structured, redacted system, service, network, and log evidence.

### Deliverables

An AsyncSSH transport hidden behind typed read-only Tools, strict operation
allowlists, normative Context Profile/Registry schemas, deterministic
ObservationRequests, linked non-recursive `OBSERVATION` Tasks, host-key
verification, timeouts, output limits, and redaction.

### Acceptance Criteria

Targets are selected only by allowlisted `target_id` bound to host, port, user,
and host-key fingerprint. Strict known-host verification is mandatory and TOFU
is forbidden. Runtime never reads or stores private keys or passwords. No raw
SSH, command, argv, or Shell API is public; each Tool owns a fixed read-only
operation with typed parameters. Only allowlisted L0/L1 reads execute. Secrets
never enter ToolResults, logs, or Memory. Each context read runs through a
linked child Task's full Policy/Approval/Executor/Verifier lifecycle; L1 can
pause for approval and observation recursion is rejected. No remote state
changes are possible.

### Test Requirements

Fake SSH transport or local test-server tests cover success, host-key failure,
authentication failure, timeout, allowlist rejection, output limits, redaction,
and L0/L1 Policy behavior. Real-server tests are opt-in only.

### Out of Scope

Restart, file writes, package changes, Docker mutation, arbitrary Shell,
credential storage, and production-server tests by default.

## Phase 9 — Incident Memory

### Goal

Persist redacted operational facts and outcomes without affecting authority.

### Inputs

Task, Plan, Approval, Execution, Verification, and lifecycle events.

### Outputs

IncidentRecords and read-only historical context.

### Deliverables

SQLite and SQLAlchemy schema, Alembic migration, append-only create/read
repository, append-only Approval audit events, unique execution-attempt key,
transaction boundary, retention metadata, and redaction enforcement.

### Acceptance Criteria

Memory never participates in permission or approval decisions. No secret is
stored. Incident relationships are complete and schema changes always use a
documented, tested migration. Task, plan snapshot/hash, approval/audit
references, execution intent, result, and verification are appended as ordered,
locally atomic events; the plan never claims atomicity with remote side effects.
If verified work cannot be persisted, Runtime enters `FAILED`, reports a
structured storage error that distinguishes the verified remote effect from
the failed local evidence commit, and does not silently retry. Cross-process
pre-dispatch resume requires an unexpired, unconsumed approval; a consumed
approval may resume only its bound execution attempt.

### Test Requirements

Temporary-database tests cover migration, insert, query, relationships,
transaction failure, redaction, retention metadata, and historical context
retrieval.

### Out of Scope

Automatic Skill enablement, cloud sync, authorization, vector databases,
multi-user storage, and remote databases.

## Phase 10 — Restart Service

### Goal

Safely restart one allowlisted systemd service under an exact L2 approval.

### Inputs

Phases 2–6, 8, and 9; an approved L2 plan; SSH transport; target-specific
service allowlist; and pre-operation service state.

### Outputs

A structured restart ToolResult, independent verification evidence, and
Incident record.

### Deliverables

A versioned, non-idempotent `restart_service` Tool, strict service-name
validation, pre-state capture, a separate read-only service-status Tool, and
recovery guidance. Before implementation, TOOL_SPEC must be reconciled with the
confirmed non-idempotent and no-automatic-rollback semantics.

### Acceptance Criteria

The exact plan is approved immediately before execution. Only allowlisted
service names whose fresh pre-state is active are accepted. Verification uses
fresh, predeclared evidence through Executor and requires a changed systemd
InvocationID or equivalent start identity. A pre-dispatch rejection makes no
remote change. After dispatch, failure, timeout, unknown outcome, or failed
verification enters `FAILED` with a structured unknown-effect and
human-intervention disposition; the restart is never retried and no automatic
rollback is claimed or attempted.

### Test Requirements

Fake transport tests cover success, approval absence/expiry/tampering, invalid
or injected service names, timeout, command failure, verification failure,
pre-state capture, and recovery guidance. Real-server tests are opt-in only.

### Out of Scope

Arbitrary `systemctl`, stop/start controls, bulk restart, deployment, deletion,
permission changes, automatic rollback, and default production tests.

## Phase 11 — Restart Container

### Goal

Safely restart one allowlisted Docker container under an exact L2 approval.

### Inputs

Phases 2–6 and 8–10; an approved L2 plan; controlled SSH transport; target
container name and approved immutable container ID; target-specific allowlist;
and pre-operation state.

### Outputs

A structured restart ToolResult, fresh status and health evidence, and Incident
record.

### Deliverables

A typed `restart_container` Tool, strict container-identity validation,
pre-state capture, read-only status and health Tools, and recovery guidance.

### Acceptance Criteria

No Docker API or raw Shell surface is exposed. Only one allowlisted container
is addressed by a fixed, versioned transport operation. Approval binds the
target, name, resolved immutable container ID, Tool version, arguments, and
verification. Identity is resolved again before dispatch; a mismatch fails
closed and requires a new plan. Success requires the same container ID,
`running`, a newer `StartedAt`, and `healthy` when a health check is required.
After dispatch, failure, timeout, unknown identity/outcome, or failed
verification requires manual intervention; restart is not retried and automatic
rollback is not attempted.

### Test Requirements

Fake transport tests cover success, name injection, unknown containers,
approval absence/expiry/tampering, timeout, unhealthy results, pre-state
capture, stop-on-failure, and recovery guidance. Real-server tests are opt-in.

### Out of Scope

Docker API, Compose, bulk operations, image deployment, deletion, Kubernetes,
automatic rollback, and default production tests.
