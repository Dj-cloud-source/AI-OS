# PHILOSOPHY.md

# The Philosophy of AIOps Agent Runtime

Version: 1.0

---

# Why This Project Exists

Most AI agents today are designed to answer questions.

This project is different.

Its purpose is not conversation.

Its purpose is controlled action.

The goal is to build an AI Runtime that can safely operate servers while always keeping humans in control.

---

# First Principle

## AI is an Advisor before it is an Operator.

The AI should first:

Observe.

Understand.

Explain.

Plan.

Only then should it operate.

Execution is the final step.

Never the first.

---

# Runtime, Not Chat

This project is intentionally built as a Runtime.

Not a chatbot.

Not an assistant.

Not a shell wrapper.

A Runtime manages:

State

Lifecycle

Planning

Approval

Execution

Verification

Memory

Every action belongs to a lifecycle.

Nothing happens "just because the model said so."

---

# Intelligence Is Not Authority

A better model should produce better plans.

It should NEVER gain more permissions.

Permissions come from Policy.

Not Intelligence.

Changing the model must never change the security boundary.

---

# Planning Is Cheap

Execution Is Expensive

Thinking has almost zero cost.

Server operations have real-world cost.

Therefore:

The Runtime should encourage planning.

The Runtime should slow down execution.

Every dangerous operation deserves another chance to be reviewed.

---

# Human Owns The Commit

The AI may generate a plan.

Only humans can approve the plan.

Approval is not trust.

Approval is explicit responsibility.

The Runtime records:

Who approved.

When.

What changed.

What was executed.

---

# Explain Before Execute

Every action must answer five questions.

Why?

What?

Impact?

Verification?

Rollback?

If an operation cannot explain itself,

it should not execute.

---

# Every Action Is Observable

Nothing should be invisible.

Every execution leaves evidence.

Every decision leaves history.

Every incident leaves memory.

Observability is a feature,

not a debugging tool.

---

# Every Tool Has A Boundary

Tools should be:

Small.

Deterministic.

Composable.

A Tool performs one operation.

Nothing more.

Tools never decide.

Tools never plan.

Tools simply expose capability.

---

# The Planner Should Never Touch Reality

The Planner reasons.

The Executor acts.

Keeping them separate prevents intelligence from becoming authority.

The Planner imagines.

The Executor performs.

---

# Policy Is Law

Policy does not negotiate.

Policy does not reason.

Policy does not improvise.

Policy exists to remain predictable.

The Runtime should trust Policy more than the LLM.

---

# Approval Is A Transaction

Approval is not:

"Yes"

Approval is:

"I reviewed THIS exact plan."

Every approval belongs to:

Plan Hash

Arguments

Steps

Expiration Time

Changing anything invalidates approval.

---

# Memory Is Experience

Memory is not conversation history.

Memory is operational experience.

Every incident should answer:

What happened?

Why?

How was it solved?

What should be done next time?

Repeated success becomes reusable knowledge.

---

# Skills Are Reviewed Experience

A Skill is not generated creativity.

A Skill is validated operational knowledge.

Workflow:

Incident

↓

Candidate Skill

↓

Testing

↓

Human Review

↓

Approved Skill

↓

Production

---

# Fail Safely

The Runtime should prefer:

Stop

rather than

Guess.

Unknown is better than wrong.

Rejected is better than dangerous.

Incomplete is better than destructive.

---

# Security Before Intelligence

The safest AI is not the smartest AI.

The safest AI is the most predictable AI.

A Runtime should never assume that a stronger model is a safer model.

---

# Local First

Models can change.

Providers can disappear.

APIs can become unavailable.

The Runtime should survive all of them.

Local execution is not only about privacy.

It is about ownership.

---

# Learn Without Becoming Dangerous

The Runtime should improve through:

Better Context

Better Skills

Better Policies

Better Verification

Not by silently changing its own behavior.

Self-improvement must remain observable.

---

# Simplicity Wins

Small modules.

Clear boundaries.

Explicit state.

Single responsibility.

Every unnecessary abstraction becomes future maintenance cost.

---

# Architecture Is A Contract

The architecture is not documentation.

It is a contract.

Every module has responsibilities.

Every layer has limits.

Breaking those limits is considered a bug.

---

# Human Always Has The Final Word

The purpose of this Runtime is not to replace SRE.

It is to amplify SRE.

The AI provides:

Observation.

Analysis.

Planning.

Execution assistance.

Learning.

The human provides:

Judgement.

Responsibility.

Authority.

Final decision.

That boundary should never disappear.

---

# The Runtime Motto

Observe.

Understand.

Plan.

Review.

Commit.

Execute.

Verify.

Learn.

Repeat.
