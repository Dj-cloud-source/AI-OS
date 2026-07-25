# ARCHITECTURE.md

# AIOps Agent Runtime Architecture

Version: MVP v1

---

# Overview

```
                   User

                     │

               Typer CLI

                     │

              Runtime Engine

                     │

        ┌────────────┼────────────┐

        │            │            │

    Context       Planner      Memory

        │            │            │

        └────────────┼────────────┘

                     │

              Policy Engine

                     │

            Approval Engine

                     │

               Task Executor

                     │

              Verification

                     │

               Tool Gateway

                     │

      SSH / Docker / HTTP / Systemd

                     │

             Remote Linux Server
```

---

# Runtime Layers

## Layer 1

CLI

负责：

- 用户输入

- 输出执行计划

- Commit

- Explain

- Reject

CLI 不负责：

任何业务逻辑。

---

## Layer 2

Runtime Engine

整个系统入口。

负责：

Task 生命周期

Session

State Machine

Scheduler

Future Workflow

Runtime 是整个系统的大脑。

---

## Layer 3

Context Builder

负责收集：

系统信息

Docker

日志

配置

历史 Incident

Memory

最终生成：

Runtime Context

供 Planner 使用。

Context Builder

不能执行任何操作。

---

## Layer 4

Planner

输入：

Runtime Context

输出：

Execution Plan

Planner 负责：

推理

拆解任务

生成步骤

Planner 不允许：

SSH

数据库

文件

网络修改

所有执行必须经过 Executor。

---

## Layer 5

Policy Engine

整个系统最重要。

职责：

检查：

权限

风险

审批

白名单

Tool 是否允许。

Policy 永远不能调用 LLM。

Policy 必须完全可预测。

---

## Layer 6

Approval Engine

如果：

Risk >= L2

停止。

等待：

Commit。

Approval 保存：

Plan Hash

Step

Arguments

User

Expire Time

Plan 修改：

必须重新审批。

---

## Layer 7

Executor

唯一允许真正执行 Tool 的地方。

Executor：

读取：

Approved Plan

↓

调用：

Tool

↓

收集输出

↓

交给 Verifier

Executor 永远不能重新规划。

---

## Layer 8

Verifier

验证：

是否达到目标。

例如：

Restart Nginx

验证：

systemctl status

Port

HTTP

Health Check

Verifier 失败：

Runtime 停止。

---

## Layer 9

Memory

负责：

Incident

Skill

History

Memory 永远不参与权限判断。

---

## Layer 10

Tool Gateway

所有服务器能力都必须封装。

例如：

RestartServiceTool

ReadLogsTool

DockerTool

HTTPTool

NetworkTool

禁止：

Planner 直接 SSH。

---

# Runtime State

```
RECEIVED

↓

CONTEXT_BUILDING

↓

PLANNING

↓

POLICY_CHECK

↓

WAITING_FOR_APPROVAL

↓

EXECUTING

↓

VERIFYING

↓

COMPLETED
```

异常：

```
FAILED

PARTIAL_SUCCESS

ROLLBACK

MANUAL_INTERVENTION_REQUIRED
```

---

# Execution Flow

```
User

↓

Runtime

↓

Context Builder

↓

Planner

↓

Policy

↓

Approval

↓

Executor

↓

Verifier

↓

Memory
```

---

# Tool Flow

```
Planner

↓

Execution Plan

↓

Executor

↓

Tool

↓

Linux

↓

Result

↓

Verifier
```

Planner 永远不知道 Tool 如何实现。

Tool 永远不知道 Planner 为什么调用自己。

---

# Context Flow

```
Logs

Docker

Metrics

Network

Config

Incident

↓

Context Builder

↓

Runtime Context

↓

Planner
```

---

# Memory Flow

```
Execution

↓

Verification

↓

Incident

↓

Repeated Success

↓

Candidate Skill

↓

Tests

↓

Human Review

↓

Skill
```

---

# Module Responsibilities

## Runtime

生命周期管理。

---

## Planner

思考。

---

## Policy

决策。

---

## Approval

授权。

---

## Executor

执行。

---

## Verifier

确认。

---

## Memory

学习。

---

## Tool

连接真实世界。

---

# Dependency Rules

允许：

Planner

↓

Policy

↓

Executor

↓

Tool

禁止：

Tool → Planner

Memory → Executor

Planner → SSH

Planner → SQLite

Policy → LLM

Verifier → Planner

保持单向依赖。

---

# Design Rules

Runtime 是唯一入口。

Planner 不执行。

Executor 不思考。

Policy 不推理。

Memory 不决策。

Tool 不规划。

每层只负责一件事。

---

# Future Extensions

MVP

↓

Web UI

↓

Workflow

↓

Plugin System

↓

Multi-Agent

↓

Kubernetes

↓

Distributed Runtime

↓

Multi-Tenant

↓

Cloud Control Plane
