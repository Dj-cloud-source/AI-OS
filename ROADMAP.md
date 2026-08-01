# AIOps Agent Runtime Roadmap

Status: Phases 0–6 implemented; Phases 7–11 planned

This roadmap defines the implementation order for the local-first AIOps Agent
Runtime. It is subordinate to `docs/VISION.md`, `docs/PHILOSOPHY.md`,
`docs/ARCHITECTURE.md`, `docs/STATE_MACHINE.md`, `docs/TOOL_SPEC.md`, and
`AGENTS.md`.

Phases 0–3 contain only the approved local Mock Runtime and its fail-closed
lifecycle. Phase 2 is local, artifact-driven, Mock-only, and Phase 3 adds only
the reviewed deterministic Policy boundary. Both have passed their release
gates. No SSH, model, database, container, shell, network, or real server
capability is enabled by the current implementation.

Approval Gate conformance means that every Policy-allowed Plan passes through
`WAITING_FOR_APPROVAL`, `NOT_REQUIRED` is recorded, human-approval-required
work pauses without dispatch, and direct bypass is rejected. Phase 4 added only
in-process approval issuance, validation, consumption protocols, and L3
confirmation contracts. Phase 5 connects those process-local authorizations to
governed Executor dispatch. Persistent authorization and cross-process
resumption remain unavailable until Phase 9.

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
- Contract-required extra verification reads are declared as typed steps in
  the approved plan. Their Tool versions, arguments, order, and expected
  conditions are included in the plan hash; Executor obtains the evidence and
  Verifier only evaluates it.
- Runtime component injection is a trusted in-process composition/test seam,
  not a model-, Skill-, config-, or plugin-selected authority boundary. The
  supported CLI constructs the built-in Executor/Gateway and Verifier.
- Ordinary SHA-256 content hashes provide deterministic binding, not
  authentication. RuntimeOutcome is not an authorization or cross-process
  resume token; Phase 9 must reconstruct authority from owner journals and
  revalidate persisted verification semantics before recovery.
- Phase 6 uses ExecutionPlan v2 and Approval Snapshot v2. Every Plan has at
  least one ordered, structured, mandatory Plan-level verification criterion;
  its complete content and order are Hash-bound. v1 authorization cannot be
  reused for v2.
- An extra `VERIFY` Step exists only when the executed Tool Contract requires
  that exact read-only, non-L3 Tool. Every required verification reference is
  criterion-bound; partial coverage is invalid. A Contract-permitted read-only
  self-evidence result is not called twice; current
  `get_system_status@1.0.0` uses this path. Phase 6 permits at most one mutating
  source Step per Plan, and every required reference for it needs a meaningful
  postcondition rather than only provenance equality.
- An ordered Plan has an `OBSERVE`/`ACTION` prefix followed by a `VERIFY`
  suffix. Runtime asks Executor to dispatch the prefix in `EXECUTING` and the
  suffix in `VERIFYING`; Verifier still receives evidence only. An L3 Tool is
  valid only as an `ACTION` Step.
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
- Phase 3 Policy configuration is a package-resident, versioned, strict JSON
  artifact. Its RFC 8785 canonical SHA-256 hash is bound by a separate human
  review record and validated once at startup; hot reload and policy DSLs are
  not permitted.
- L1 is fail-closed: an exact missing rule is `DENY`; an exact matching rule
  may resolve to `NOT_REQUIRED` or `HUMAN_PLAN_APPROVAL`.
- Phase 4 denied every resolved L3 Step while its confirmation protocol was
  isolated. Phase 5 connects a single-use confirmation to the exact invocation
  boundary. The confirmation Challenge Hash covers the complete strict model
  except its self-referential Hash, including Authorization, Approval, Plan,
  Approval Record/Expiration, Step, exact Tool identity and integrity Hashes,
  Arguments, Target, execution attempt, and invocation; it is not a Runtime
  state. Removing the temporary blanket denial does not register
  or authorize a Tool: the default Registry remains L0-only, and an L3 Tool
  still needs reviewed artifacts, an exact Policy allow decision, Plan
  Approval, and per-invocation confirmation.
- Plan Approval consumption is linearized when Executor opens the exact
  attempt; only then may Runtime enter `EXECUTING`. The Approval is burned even
  if execution stops before dispatch. For each L3 invocation, Confirmation
  consumption is additionally adjacent to dispatch of the already-bound
  ToolCall in the same Executor call stack. Neither boundary claims a
  transaction with a remote side effect.

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
- ExecutionReport records per-Step dispatch certainty and effect certainty.
  A failure known to precede handler entry records no dispatch and no effect;
  a post-dispatch result cannot claim a confirmed mutating effect before
  independent Verification.
- If no trustworthy final ExecutionReport exists and attempt closure cannot be
  confirmed, Runtime records hash-bound `ExecutionUncertainty` and enters
  `FAILED`. Before a dispatch-capable call it may prove `NOT_DISPATCHED/NONE`;
  afterward it must use `UNKNOWN/UNKNOWN` and require human intervention. It
  never fabricates Step, Invocation, or ToolResult facts.
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

Status: Implemented (2026-07-25); Approval Gate conformance completed
(2026-07-26)

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

Status: Implemented (2026-07-25); Approval Gate conformance completed
(2026-07-26)

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

Status: Implemented (2026-07-29); only the reviewed local
`get_system_status@1.0.0` Mock artifact is registered

### Goal

Define the single typed, artifact-driven, and auditable boundary for
operational capabilities without enabling any real system access.

### Inputs

The completed Phase 1 Runtime and Approval Gate contracts, core models, the L0
`get_system_status` Mock Tool, and the Tool requirements in
`docs/TOOL_SPEC.md`.

### Outputs

Five versioned local JSON Schemas; immutable Contract, Registry Record,
Implementation Bundle, ToolCall, ToolResult, and ToolError models; an
artifact-driven Registry; a deterministic Tool Gateway; and sanitized
Mock-replay evidence.

### Deliverables

- JSON Schema Draft 2020-12 artifacts for Tool Contract, Tool Result, Replay
  Fixture, Registry Record, and Implementation Bundle.
- A package-resident reviewed artifact set for each exact Tool version:
  immutable Contract, separate Registry Record, implementation manifest,
  dependency-lock evidence, and sanitized fixtures.
- RFC 8785 canonical JSON plus SHA-256 Contract, Implementation, Arguments, and
  Fixture hashing with explicit hash-field exclusions.
- An explicit startup Registry that derives the authoritative immutable
  Tool Metadata from validated artifacts, verifies installed-file and
  dependency-lock digests, rejects duplicates, and freezes before resolution.
- A Gateway callable only by Executor that resolves exact versions, validates
  ToolCall identity and hashes, validates input before dispatch and the complete
  Gateway-owned ToolResult after return, enforces target scope, timeout,
  redaction, and retained-size limits, and maps failures to stable structured
  errors.
- Migration of `get_system_status` to a typed payload-only Mock handler. The
  Gateway, not the Mock Tool, owns trusted invocation and result-envelope
  fields.
- Sanitized local replay that reads fixtures or Mock results and never invokes
  a production Tool implementation.
- A repository-local `uv.lock` and local-only lock, source-distribution, wheel,
  package-resource, and clean-install build gates.

### Acceptance Criteria

- Registration fails closed unless all five normative Schemas and every
  required package artifact validate and all exact identity, status, hash,
  installed-file, dependency-lock, fixture, ABI, and model bindings agree.
- Contract Hash and Implementation Hash are recomputed from the schema-validated
  raw JSON artifacts using UTF-8 RFC 8785 canonical JSON and SHA-256; Registry
  status remains outside the immutable Contract.
- RiskLevel and other authoritative Metadata are derived from the exact
  registered Contract. Planner, caller arguments, Tool code, replay data, and
  Runtime text cannot supply or override them.
- The frozen Registry resolves only an exact registered `(tool_id, version)`;
  unknown, disabled, duplicate, unreviewed, malformed, or hash-drifted Tools
  are unavailable.
- The Gateway accepts only a strict hash-bound ToolCall, materializes no hidden
  post-approval arguments, validates arguments with both typed models and the
  registered Schema, and rejects target expansion before dispatch.
- The handler returns only a typed payload. The Gateway creates and validates
  the complete structured ToolResult envelope against the global Result Schema
  and exact Contract output Schema. No Tool returns a top-level free string.
- Only Executor may invoke the Gateway. The Registry and Gateway do not plan,
  decide Policy or Approval, infer risk, retry, or expose handler, transport,
  callback, or raw command interfaces.
- Replay uses only sanitized package fixtures, recorded Tool Results, or Mock
  Tools with production connections disabled; it verifies exact identity,
  hashes, schemas, sequence, expected outcome, and redaction evidence.
- The checked-in local dependency lock is current. Local source and wheel
  builds contain all five Schemas and required Tool artifacts, and a clean
  local installation passes package-resource, import, CLI, and Mock Runtime
  smoke gates; the source tree passes type, lint, format, and test gates.
- Every acceptance path proves absence of SSH, LLM/model, shell, Docker,
  Kubernetes, network, database, remote target, and other real I/O.

### Test Requirements

Tests cover all five meta-schemas, nested Contract input/output Schemas, RFC
8785 vectors and exclusions, immutable artifact-derived Metadata, exact frozen
lookup, duplicate and unknown identities, installed-file and lock drift,
ToolCall and target binding, strict input and output validation, stable errors,
timeouts, redaction and size boundaries, deterministic Mock output, sanitized
offline replay, and wheel-installed package resources. `pytest`, Ruff, strict
mypy, the local dependency-lock check, and clean local build/install checks
must pass.

### Out of Scope

SSH, LLMs or model adapters, Docker, Kubernetes, HTTP, database access,
filesystem or process mutation, network or remote execution, dynamic plugins,
arbitrary Shell, automatic retry, production registration, and mutating Tools.

## Phase 3 — Policy Engine

Status: Implemented (2026-07-29); the active reviewed Profile grants only
`local-user → local-mock → get_system_status@1.0.0`

### Goal

Implement deterministic permission, allowlist, risk, and approval-requirement
decisions.

### Inputs

An ExecutionPlan, a frozen Tool Registry, target identity, local operator
context, and a package-resident reviewed Policy Profile.

### Outputs

A structured, explainable PolicyDecision for every plan and step.

### Deliverables

The Policy Engine, exact capability rules, a versioned strict-JSON Policy
Profile, a separate review record, the L0–L3 decision matrix, stable denial
reasons, and fail-closed defaults. The profile is hashed using SHA-256 over its
UTF-8 RFC 8785 canonical JSON representation. Its review record binds the
exact profile identity, version, content hash, review status, reviewer, and UTC
review timestamp. The single-user MVP accepts only the local reviewer identity
`local-owner`.

### Acceptance Criteria

Policy never calls an LLM, invokes a Tool, mutates the Registry, or reads Tool
risk from a Plan. It may use only the frozen Tool Registry's read-only
metadata view and the shared canonical-hashing integrity helpers. L0 is
automatic only when an exact capability rule allows it. L1 has no implicit
allow: a missing exact rule is denied, while a matching rule explicitly chooses
`NOT_REQUIRED` or `HUMAN_PLAN_APPROVAL`. L2 requires human plan approval.
During Phase 3 every resolved L3 Step is denied. A structurally valid L3 Step
whose identity, integrity, and target scope pass uses
`l3_confirmation_unavailable`, while its decision still reports
`HUMAN_PLAN_APPROVAL` and `PER_INVOCATION`; earlier validation failures retain
their specific stable reason codes. Missing or malformed
metadata, an unknown Tool, an unreviewed or hash-mismatched Policy Profile, an
unknown policy field, or a disallowed operator, target, or Tool is denied or
prevents Runtime startup as appropriate. Plan risk is the highest resolved
step risk.

The Policy Profile is loaded and validated once at startup. It has no
environment override, wildcard rule, executable expression, user-defined code,
dynamic policy language, or hot-update path. A changed profile has a new hash
and requires a new independent review record before Runtime can use it.

### Test Requirements

An exhaustive risk matrix covers L0 allow, L1 default deny, both explicit L1
approval modes, L2 approval, the Phase 3 L3 hard denial, metadata and profile
tampering, unknown Tools, disallowed operators and targets, unreviewed profiles,
Registry mutation attempts, forbidden dependency imports, and deterministic
repeatability.

### Out of Scope

Approval issuance or persistence, L3 confirmation, Tool execution, hot policy
reload, dynamic policy languages, user-authored policy code, RBAC, multi-user,
and model-based risk classification.

## Phase 4 — Approval

Status: Implemented (process-local authorization only; no human-approved
dispatch)

### Goal

Implement exact-plan approval that expires and cannot authorize a changed plan.

### Inputs

A PolicyDecision requiring `HUMAN_PLAN_APPROVAL`, canonical ExecutionPlan,
frozen Registry Metadata, fixed `local-user` operator and `local-owner`
approver, reviewed TTL constraints, and UTC clock.

### Outputs

An ApprovalRecord and a structured validation result.

### Deliverables

A strict `PlanApprovalSnapshot`, canonical SHA-256 plan hashing, approval
creation, validation, one-time consumption, expiration, invalidation, per-Step
L3 confirmation protocol, and an in-process CLI Review/Commit path. The
reviewed Policy Profile fixes a 300-second Review session, a 300-second Plan
Approval, and a 30-second L3 confirmation ceiling. The local operator remains
`local-user`; only the fixed local control-plane identity `local-owner` may
Commit or Confirm.

### Acceptance Criteria

Approval binds the ordered steps, Tool names and versions, exact arguments,
target, verification criteria, approver, and expiration. Any execution-relevant
change invalidates approval. Expired, consumed, rejected, or mismatched approval
is rejected. Phase 4 Review/Commit occurs in one CLI process and leaves the Task
in `WAITING_FOR_APPROVAL`; Phase 5 connects process-local linearized validation
and consumption to Executor dispatch. Cross-process resume is deferred until
Phase 9.
Consumption binds approval to one execution attempt and cannot authorize a
retry or second attempt. The default Registry remains L0-only and no synthetic
L2 capability is registered for demonstration.

### Test Requirements

Tests cover hash determinism, step order, argument mutation, Tool version
changes, target changes, verification changes, expiration, L2 approval, and
single-use per-Step L3 confirmation.

### Out of Scope

Executor dispatch or `WAITING_FOR_APPROVAL → EXECUTING` resumption for a
human-approved Plan, database or file persistence, cross-process resume, Web
approval, multi-user approval, dual control, RBAC, remote identity providers,
and any registered L2/L3 capability.

## Phase 5 — Executor

Status: Implemented (2026-08-01)

### Goal

Make Executor the only component capable of invoking an approved Tool.

### Inputs

An ExecutionPlan, its PolicyDecision, an Approval ID referencing an Approval
Engine-owned process-local ApprovalRecord when Policy requires one,
authoritative frozen Tool Metadata, the deterministic Tool Gateway, and an
injectable monotonic timer. A caller-supplied record is never authority.

### Outputs

Ordered ToolResults, structured execution events, an ExecutionReport that
records execution-attempt identity, per-Step dispatch/effect certainty, failure
position, elapsed plan time, and the next-state fact for Runtime, plus bounded
ExecutionUncertainty when trustworthy attempt closure evidence is unavailable.

### Deliverables

Sequential dispatch in exact approved order; immediate Policy and applicable
Approval revalidation for the current invocation; process-local one-time
Approval consumption bound to attempt creation before Runtime enters
`EXECUTING`; L3 challenge generation, single-use Confirmation consumption, and
adjacent dispatch for every L3 invocation; plan elapsed-time recording;
stop-on-failure with no automatic retry; a Gateway-internal dispatch receipt;
and sanitized in-memory audit evidence. Runtime gains same-process governed
resumption of an exact paused Outcome. Interactive CLI Commit immediately
resumes that exact Outcome in the same process; approval issuance APIs do not
implicitly dispatch.

The first consumed Approval for one exact Plan Hash blocks sibling Approvals
from starting a second attempt. Each attempt permits only one active Executor
call, including while an L3 Confirmation reader is running. Missing or invalid
abort reports are revalidated through the same Runtime boundary as ordinary
execution reports; failed closure is represented by ExecutionUncertainty, not
assumed safe.

The current synchronous Mock Gateway remains authoritative for each Tool's
registered timeout. It measures elapsed time after handler return and emits
`tool_timeout`. Executor records total plan elapsed time, treats
`tool_timeout` as terminal for the attempt, and never claims to interrupt,
cancel, or preempt the handler.

### Acceptance Criteria

Executor never plans, changes arguments, bypasses Policy, or silently retries
an operation. It validates every precondition that applies to the current
invocation before that invocation; a later L3 Confirmation is obtained only
when its exact Step is next. L2/L3 steps require valid approval, and each L3
invocation requires `CONFIRM <challenge-hash>` for the complete bound
challenge. Confirmation is not a Runtime state.

ExecutionPlan contains between 1 and 64 Steps. Oversized or bypass-constructed
Plans are rejected by Executor before any dispatch.

Only an `OBSERVE`/`ACTION` prefix may run in `EXECUTING`; only the trailing
`VERIFY` suffix may run in `VERIFYING`. Verification evidence Steps pass
through Policy and Executor. Context collection stays a pure consumer of
supplied evidence through Phase 7; linked observation Tasks are owned by Phase
8. Every definite failure enters `FAILED`; ExecutionReport distinguishes zero
dispatch, handler dispatch, effect pending Verification, and effect
uncertainty. Full action-prefix success enters `VERIFYING`, never directly
`COMPLETED`.

If an attempt cannot produce a trustworthy final report and safe abort also
cannot be confirmed, the stable failure is `execution_abort_uncertain`.
Runtime preserves only a hash-bound prior trusted awaiting-verification report,
when present. Once a dispatch-capable boundary has been called, uncertainty is
`UNKNOWN/UNKNOWN` with human intervention required. CLI and structured audit
output make this condition visible without inventing per-Step facts.

An L3 Tool may appear only in an `ACTION` Step; an L3 `OBSERVE` or `VERIFY`
Step fails structural validation before dispatch.

The bundled CLI is the only supported Phase 5 production Confirmation adapter.
It requires interactive input and output TTYs plus exact
`CONFIRM <challenge-hash>` input. Runtime/Executor reader injection is a
trusted process-local test seam, not proof of human provenance, and must not be
connected to a model, Skill, Tool, pipe, or environment value. The default
Registry has no L3 Tool. Before any production L3 Tool is registered, an
independently reviewed design must bind this seam to a verifiable interactive
source; otherwise production L3 remains unavailable.

The Gateway-internal dispatch receipt records whether the handler boundary was
entered. It does not change Tool Protocol v1, ToolResult v1, Contract Hashes, or
public Tool output.

Executor and Runtime independently revalidate every retained receipt/report
result against the exact frozen Registry Contract, including redaction,
declared errors, timeout, and evidence projection. Retained evidence is deeply
immutable. A later verification or abort report must monotonically extend the
last trusted cumulative report and cannot erase prior dispatch facts.

### Test Requirements

Recording Fake Tool tests cover exact order and arguments, role ordering,
Runtime state segmentation, Gateway-owned synchronous timeouts, plan elapsed
time, missing/expired/consumed Approval, Plan or Policy tampering, unknown
Tools, pre-dispatch and post-dispatch failures, mid-plan stop behavior, and
absence of automatic retries. Concurrency and injected boundary-failure tests
prove at-most-once process-local consumption, burned authorization after
consumption, and no false claim of durable crash recovery or remote
transactional atomicity. L3 tests
cover the complete challenge hash, wrong/expired/replayed confirmation,
multiple L3 Steps, reentrant reader rejection, and immediate
consumption-to-dispatch adjacency. Hostile boundary tests cover contradictory
receipts, invalid Gateway error claims, forged success and abort reports,
pre-dispatch abort double failure, post-action clock/abort double failure,
verification-close tampering, prior-report Hash retention, uncertainty audit
redaction, truncated cumulative reports, contract-invalid or secret-bearing
results, immutable retained evidence, and sibling Approval single consumption.
Protocol tests prove the internal receipt does not alter ToolResult v1.

### Out of Scope

Real transports, SSH, parallel execution, automatic rollback, persistence, and
remote server mutation.

## Phase 6 — Verifier

Status: Implemented (2026-08-01)

### Goal

Determine whether execution achieved the stated goal using structured evidence.

### Inputs

ExecutionPlan v2 mandatory verification criteria, ToolResults, any
Contract-required verification evidence already included in the approved Plan
and obtained through Executor, plus Runtime-owned evidence acceptance context.

### Outputs

A strict, immutable VerificationResult with identity bindings, ordered checks,
evidence references, stable failure reasons, effect closure, human-intervention
fact, and canonical content Hash.

### Deliverables

Pure deterministic verifiers, strict criteria and context models, conservative
freshness, evidence requirements, missing-evidence handling, effect closure,
and Runtime integration. The only v1 kinds are `equals`, `numeric_bounds`,
`expected_state`, and `health_status`; dynamic paths and executable evaluators
are forbidden. Runtime supplies only strictly rebuilt isolated Plan/result/
context copies to an injected Verifier, independently recomputes the verdict,
binds every check and report-result reference into RuntimeOutcome, and emits a
hash-only `verification_audit`. Terminal CLI Review displays each criterion's
complete exact definition.

### Acceptance Criteria

Verifier never invokes a Tool, plans, changes permissions, or requests,
initiates, or executes recovery. Every Plan has at least one ordered mandatory
criterion and every check is one-to-one and in order. Missing, stale,
malformed, contradictory, unplanned, reordered, or identity-invalid evidence
cannot produce success.

Runtime takes one trusted local UTC sample after validating the cumulative
report and uses that same sample for `evidence_accepted_at` and `evaluated_at`.
It sets `collection_duration_ms` to the larger of Executor's total report
duration and the Runtime-observed `EXECUTING`-entry-to-acceptance elapsed time
rounded up to milliseconds, capped at 3,600,000 ms. This local receipt fact is
not a remote event timestamp. Only an all-pass, hash-valid `PASSED` result
reaches `COMPLETED`. Runtime strictly rebuilds isolated inputs and the returned
result, then independently recomputes the verdict; content Hash alone cannot
make a forged pass trustworthy, and duplicate Invocation IDs are rejected.

Every `VERIFY` Step is Contract-declared, criterion-bound, read-only, and
non-L3, and every required verification reference is covered. Phase 6 accepts
at most one mutating source Step per Plan, and every mutating source Step must
use the `ACTION` role. Every required verifier for that
mutation must have a `numeric_bounds`, `expected_state`, or `health_status`
postcondition; a provenance-only `equals` criterion is insufficient.

RuntimeOutcome binds every check identity and every ordered evidence reference,
including each exact result Hash. `verification_audit` contains only IDs,
hashes, statuses, stable reasons, effect/human closure, and check/reference
metadata; it never contains raw evidence. A thrown or incomplete Verifier is
`VERIFIER_FAILED`; an invalid or recomputation-mismatched returned result is
`VERIFIER_RESULT_INVALID`.

Read-only work has effect `NONE`; pending mutation becomes `VERIFIED` only on
pass. Any failed or inconclusive pending mutation enters `FAILED` with effect
`UNKNOWN` and human intervention required.

### Test Requirements

Tests cover all four strict evaluators, pass, fail, type errors, missing/stale/
malformed/contradictory evidence, multiple criteria, Plan/Approval v2 Hash
binding, conservative freshness boundaries, self-evidence without duplicate
dispatch, complete required-reference coverage, meaningful independent
read-only verification for one mutation, rejection of multiple mutation Steps,
isolated injected inputs, exact Outcome/audit closure, distinct Verifier
exception versus invalid-result reasons, full terminal CLI criterion display,
hostile result closure, and confirmation that Verifier has no Tool Gateway or
recovery dependency.

### Out of Scope

LLM verification, direct Tool calls, Incident persistence, automatic recovery,
rollback execution, multiple mutating source Steps, and implicit
action-to-criterion/effect inference.

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
repositories, an Approval-owned AuthorizationJournal, a
Runtime/Executor-owned ExecutionJournal, Incident Memory audit projections, a
unique execution-attempt key for idempotent local event recovery only,
transaction boundaries, retention metadata, and redaction enforcement. These
logical owners may share one physical SQLite database but never use Incident
Memory as authorization input. ExecutionJournal includes explicit attempt
closure and closure-uncertainty evidence; missing closure is never inferred as
zero dispatch.

### Acceptance Criteria

Memory never participates in permission or approval decisions. Approval
reconstructs authority only from its own AuthorizationJournal; Memory receives
redacted, non-authoritative projections. No secret is stored. Incident
relationships are complete and schema changes always use a documented, tested
migration. Task, plan snapshot/hash, authorization events, execution intent,
result, and verification are appended as ordered, locally atomic events; the
plan never claims atomicity with remote side effects. If verified work cannot
be persisted, Runtime enters `FAILED`, reports a structured storage error that
distinguishes the verified remote effect from the failed local evidence
commit, and does not silently retry. Cross-process pre-dispatch resume requires
an unexpired, unconsumed approval plus current Plan, Policy, and Tool Metadata
revalidation. A consumed approval may resume only its exactly bound,
still-unexpired attempt and only when no mutating dispatch intent has an
unknown outcome; recovery never automatically redispatches a mutating Tool.
Persisted RuntimeOutcome/Incident projections and their ordinary content
Hashes are never authority or authenticity proof. Before recovery trusts a
verification fact, the owning journals and storage-integrity boundary must
detect coordinated rewrites and the Runtime must recompute all applicable
semantic bindings.

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
