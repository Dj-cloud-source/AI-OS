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

Every feature must belong to one Runtime stage.

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

Approval

↓

Execute

↓

Verify

↓

Memory

↓

Skill Evolution

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

The Runtime decides.

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
- risk evaluation
- approval requirement

Policy Engine must NOT call LLM.

---

Executor

Responsible for:

- invoking approved Tools
- recording execution
- collecting outputs

Executor never generates plans.

---

Verifier

Responsible for:

- checking expected result
- reporting success/failure
- requesting rollback

---

Memory

Responsible for:

- Incident Memory
- Skill Memory

Memory never decides execution.

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

Input

↓

Validation

↓

Execution

↓

Structured Output

↓

Verification

Example:

```python
RestartServiceTool

Input:
    service_name

Output:

success

stdout

stderr

duration

verification
```

Never return plain strings.

---

# Planning Rules

Planner produces only:

ExecutionPlan

ExecutionStep

Reason

Risk

Verification

Rollback

Planner never produces shell scripts.

---

# Risk Levels

L0

Automatic

L1

Policy Controlled

L2

Approval Required

L3

Manual Confirmation Required

Risk level belongs to Tool metadata.

Never let the LLM classify risk dynamically.

---

# Logging

Every execution records:

Task ID

Plan ID

Approval ID

User

Target

Tool

Arguments

Result

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

Repeated successful incidents may become Candidate Skills.

---

# Skill Rules

Skills are generated automatically.

Skills are NOT enabled automatically.

Workflow:

Candidate Skill

↓

Tests

↓

Human Review

↓

Enabled

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

1. Read PROJECT.md

2. Read ARCHITECTURE.md

3. Read AGENT.md

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
