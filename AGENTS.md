# AGENTS.md

> Development Rules for AIOps Agent Runtime

This document defines how AI coding agents (Codex, Claude Code, Gemini CLI, etc.) should contribute to this repository.

The goal is to keep the project maintainable, secure, and architecture-first.

---

# Philosophy

This is **NOT** an AI chatbot.

This is **NOT** an SSH automation script.

This project is an **AI Agent Runtime** for Server Operations.

Everything should reinforce:

- Safety
- Observability
- Reproducibility
- Explicit Control

---

# Core Principles

## 1. Runtime First

Never implement isolated features.

Every production feature must belong to one Runtime responsibility.

The main task lifecycle is:

Observe

↓

Collect Context

↓

Diagnosis

↓

Planning

↓

Policy

↓

Human Approval / Commit when Policy requires it

↓

Execute

↓

Verify

↓

COMPLETED

Incident Memory may be recorded after task completion without extending or
reopening the main task lifecycle.

Skill Evolution is an optional asynchronous job created only after a terminal
Runtime outcome is finalized as a sanitized Incident.
It has its own lifecycle and is never appended to the main task state machine.
An Evolution failure must not change or invalidate the completed production
task.

A finalized `FAILED` task may also provide sanitized negative Incident evidence
to an Evolution Job. Evolution never reads an active task or changes a terminal
outcome.

---

## 2. Tool First

The AI must NEVER execute arbitrary shell commands directly.

Everything must be wrapped as a Tool.

Good:

```python
restart_service("nginx")
```

Bad:

```python
ssh.exec("systemctl restart nginx")
```

---

## 3. Policy First

Permission decisions are NEVER made by the LLM.

Only Policy Engine decides.

The model may suggest.

The Runtime enforces the deterministic decision.

Approval Engine records and validates Policy's result. It does not decide
permissions. Only an authorized human can produce `APPROVED`; deterministic
Policy may produce `NOT_REQUIRED`.

---

## 4. Explainability

Every execution plan must explain:

- Why
- What
- Impact
- Verification
- Rollback

Never generate execution steps without explanations.

---

## 5. Human Control

Risky operations always stop at

WAITING_FOR_APPROVAL

No exception.

---

# Development Rules

## Never

Never:

- bypass Policy Engine
- bypass Approval
- bypass Verification
- bypass Tool Layer
- execute raw shell from Planner
- hardcode credentials
- store secrets in repository
- ignore failed verification
- silently retry dangerous operations

---

## Always

Always:

- use Pydantic models
- write type hints
- write docstrings
- keep modules small
- log important operations
- return structured data
- use async APIs whenever practical

---

# Architecture Boundaries

Planner

Responsible for:

- reasoning
- task decomposition
- execution plan

Planner CANNOT:

- execute
- access SSH
- modify database

---

Tool Layer

Responsible for:

- server interaction

Tools must be:

- deterministic
- idempotent whenever possible
- individually testable

---

Policy Engine

Responsible for:

- permission checking
- resolving and enforcing authoritative Tool Metadata risk
- approval requirement

Policy Engine must NOT call LLM.

---

Executor

Responsible for:

- invoking approved Tools
- recording execution
- collecting outputs

Executor never generates plans.

Only Executor may invoke a Tool, always through Tool Gateway.

---

Verifier

Responsible for:

- checking expected result
- reporting success/failure
- requesting rollback

Verifier consumes Runtime-provided structured results. It never invokes Tool.

---

Memory

Responsible for:

- Incident Memory
- immutable Skill usage facts and exact version references

Memory never stores authoritative Skill content, Lifecycle Status, Review
Record, or active pointer; those belong to Skill Registry. Memory never decides
execution, approval, or activation.

---

# Code Style

Use

Python 3.12+

Prefer

Pydantic

SQLAlchemy

Typer

Rich

AsyncSSH

Avoid unnecessary dependencies.

---

# Repository Structure

Do not move directories without approval.

Expected structure:

```
agent/
approval/
cli/
memory/
policy/
runtime/
storage/
tools/
tests/
docs/
```

---

# Database Rules

Every schema change requires:

- migration
- documentation
- tests

Never change production schema silently.

---

# Tool Design

Every Tool should have:

- stable Tool ID and version
- validated input schema
- structured output schema
- authoritative risk metadata
- declared side effects and target scope
- redaction rules
- structured error codes
- verification and rollback metadata
- sanitized recorded or mock replay fixtures

The design requirements for the future machine-verifiable contract are defined
in `docs/TOOL_SPEC.md`. No Tool may be registered until its normative Contract,
Result, Fixture, and Registry Record schemas exist.

Never return plain strings or accept arbitrary commands.

---

# Planning Rules

Planner produces only:

ExecutionPlan

ExecutionStep

Reason

Exact Tool identity, Version, Contract Hash, and Implementation Hash

Verification

Rollback

Planner never produces shell scripts.

Planner does not place Tool risk in an ExecutionPlan. Policy resolves risk from
the exact frozen Registry Metadata after planning. Planner must never infer,
copy, lower, or override a Tool risk level.

---

# Risk Levels

L0

Policy may record `NOT_REQUIRED` and allow automatic execution

L1

Policy Controlled

L2

Explicit Human Approval / Commit Required

L3

Explicit Human Approval / Commit Required

plus

Immediate Manual Confirmation Required

Risk level belongs to Tool metadata.

Never let the LLM classify risk dynamically.

Until Phase 5 atomically connects the Phase 4 per-invocation, one-time L3
confirmation protocol to the exact Tool dispatch boundary and tests that
connection, Policy must deny every L3 execution.

For a multi-step Plan, effective risk is at least the maximum authoritative
Tool risk among its Steps. Policy may raise restrictions but never lower them.

Approval binds the exact:

- Plan Hash
- concrete Arguments
- Expiration

Any change to approved content invalidates the approval. An expired approval
must never authorize execution.

Plan Hash canonicalization and coverage are defined in `docs/ARCHITECTURE.md`.

Phase 4 Approval is process-local only. Review Session and Plan Approval TTLs
are capped at 300 seconds and L3 Manual Confirmation at 30 seconds by a
reviewed Policy Profile. Operator is `local-user`; only the fixed local control
identity `local-owner` may Commit or Confirm. Phase 4 does not resume a
human-approved Plan or dispatch a Tool.

---

# Logging

Every execution records:

Task ID

Plan ID

Approval ID

User

Target

Tool

Arguments (redacted)

Result (redacted)

Duration

Verification

Logs should be structured JSON.

---

# Testing

Every new Tool requires:

Unit Test

Mock Test

Policy Test

No Tool should be merged without tests.

---

# Security

Never:

read private keys

read passwords

dump secrets

print tokens

If a Tool can expose secrets,

default behavior must redact them.

---

# Memory Rules

Memory stores facts.

Memory never stores secrets.

Incident Memory contains:

Problem

Evidence

Plan

Execution

Verification

Lessons Learned

Qualified, sanitized Incident patterns may become inputs to `CANDIDATE`
generation. Repetition or success alone is not sufficient.

---

# Skill Rules

Automation may generate only a `CANDIDATE` Skill.

Automation and models must never review, approve, or activate a Skill.

Workflow:

`CANDIDATE`

↓

Validation and Tests

↓

Human Review

↓

`APPROVED`

↓

Explicit Human Activation

↓

`ACTIVE`

Only `ACTIVE` Skills may participate in default production retrieval.
Changing Candidate content invalidates prior review and approval.

---

# Commit Rules

Small commits.

Single responsibility.

Examples:

feat(runtime): add task state machine

feat(policy): implement risk evaluation

fix(tool): docker restart verification

Avoid:

misc update

fix bugs

update files

---

# AI Coding Rules

Before implementing:

1. Read `docs/VISION.md`.

2. Read `docs/PHILOSOPHY.md`.

3. Read `docs/ARCHITECTURE.md`.

4. Read `docs/STATE_MACHINE.md`.

5. Read `docs/TOOL_SPEC.md`.

6. Read `AGENTS.md`.

7. When planning or implementing a project phase, also read `ROADMAP.md` and
   `docs/IMPLEMENTATION_PLAN.md`.

Treat these governance documents as constraints. If they conflict, report the
conflict and wait for a human decision instead of choosing silently.

Never guess architecture.

Never invent modules.

If architecture is unclear,

stop and ask.

---

# MVP Priority

Priority 1

Runtime

Policy

Approval

Tools

Execution

Verification

Priority 2

Incident Memory

CLI UX

Logging

Priority 3

Skill Evolution

Workflow

Plugin

Web UI

Priority 4

Kubernetes

Distributed Runtime

Multi User

---

# Definition of Done

A feature is complete only if it has:

✓ implementation

✓ tests

✓ documentation

✓ logging

✓ verification

✓ error handling

✓ rollback consideration

If any item is missing,

the feature is NOT complete.

---

# Final Rule

When making design decisions:

Safety

>

Correctness

>

Observability

>

Convenience

>

Autonomy

The AI should be powerful,

but always predictable and reviewable.
