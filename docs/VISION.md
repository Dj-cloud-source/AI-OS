# AIOps Agent Runtime

> A Local-first AI SRE Agent powered by open-source LLMs.

---

# Vision

构建一个运行在本地电脑上的 AI 运维助手。

AI 不直接拥有服务器权限，而是在受控 Runtime 中完成：

- 服务器状态观察
- 故障诊断
- 执行计划生成
- 人工审批
- 自动执行
- 结果验证
- Incident Memory
- Skill Evolution

所有模型推理默认在本地完成。

不依赖第三方模型 API。

---

# Product Position

不是聊天机器人。

不是 SSH 自动脚本。

不是 AI Shell。

而是一个：

> AI Agent Runtime for Server Operations

---

# Design Principles

## 1.

Local First

模型可以替换。

数据属于用户。

默认离线。

---

## 2.

Everything Is A Tool

AI 不允许直接执行 Shell。

所有操作必须调用 Tool。

例如：

get_system_metrics()

restart_service()

docker_restart()

verify_http()

---

## 3.

Everything Is Observable

每一步必须可追踪。

记录：

- 为什么执行
- 执行什么
- 参数
- 输出
- 结果
- Token
- Runtime
- Incident

---

## 4.

Everything Needs Policy

AI 永远不能绕过权限。

权限来自：

Policy Engine

不是模型。

---

## 5.

Human In The Loop

危险操作：

必须审批。

审批对象不是一句话。

而是一份 Execution Plan。

---

# Risk Levels

L0

普通状态读取

Policy 评估并记录 `NOT_REQUIRED` 后可自动执行

例如：

CPU

Memory

Disk

Service Status

---

L1

敏感读取

需要策略判断

例如：

Config

Application Logs

Database Schema

---

L2

低风险修改

需要显式人工 Approval / Commit

例如：

Restart Service

Restart Container

Rollback

Deploy Known Version

---

L3

高风险操作

需要显式人工 Approval，

并在每个 L3 Tool invocation 前进行即时、不可重放的 Manual Confirmation

例如：

Delete

Firewall

Database Write

Permission Change

Secrets

L2 和 L3 的 Approval 均绑定：

- Approval ID
- 精确的 Plan Hash 和有序 Steps
- Tool ID、Version 和 Contract Hash
- 目标引用和具体 Arguments
- Verification 与 Rollback 要求
- Approver 和过期时间

计划或参数发生任何变化，

原 Approval 立即失效。

Risk Level 描述最低安全要求，

不代表操作已获准。

Policy 仍可拒绝任何操作。

上述例子只说明产品意图。实际 Risk Level 只来自精确 Tool Version 的受保护
Metadata；模型、Planner、Skill 和 Memory 都不能自行分类或降低风险。

当前 MVP 在一次性 L3 Manual Confirmation 协议实现并测试前，拒绝所有 L3
执行。

---

# Primary Runtime Flow

Observe

↓

Collect Context

↓

Diagnosis

↓

Planning

↓

Policy Check

↓

Approval Decision

`NOT_REQUIRED` 立即通过；Policy 要求时等待 Review / Commit

↓

Execute

↓

Verify

↓

COMPLETED

完成后的执行证据可以写入 Incident Memory。

Incident Memory 不是额外的 Runtime 状态。

在 Runtime terminal outcome 被脱敏并固化为 Incident 后，

系统可以选择创建独立的异步 Evolution Job：

```
Finalized Incident

from COMPLETED or FAILED

↓

Optional Evolution Job

↓

Candidate Skill
```

Skill Evolution 不属于主任务状态机。

不创建 Evolution Job，

不会影响主任务完成。

Evolution Job 失败，

也不能改变已经产生的 COMPLETED 结果。

对 `FAILED` 任务生成的 Evolution Job 同样不能改写原失败结果。

---

# Approval Model

Prepare

↓

Review

↓

Commit

↓

Execute

L2 Approval / Commit 绑定精确计划、参数和过期时间。

L3 在此基础上，

还需要每个 L3 Tool invocation 前的即时 Manual Confirmation。

任何计划发生变化：

重新审批。

---

# Incident Memory

保存：

问题

证据

执行步骤

结果

恢复方法

经验

合格、脱敏且有来源追踪的 Incident 模式：

可以作为生成 Candidate Skill 的输入。

重复或成功本身都不足以证明候选有效。

Candidate Skill：

必须经过：

测试

Skill Review Approval

显式人工激活

之后才能成为 `ACTIVE`。

---

# Scope (MVP)

支持：

Linux

Docker

Systemd

SSH

HTTP

Network

---

自动执行：

系统状态

容器状态

健康检查

网络检查

日志读取（脱敏）

---

审批执行：

Restart Service

Restart Container

Deploy

Rollback

---

禁止：

自由 Shell

对受管生产数据库或业务数据库写入

删除受管生产目标或业务资源

权限修改

Secrets

上述限制不禁止经过架构定义的本地控制面元数据写入，

例如：

- Incident Metadata
- Skill Metadata
- Audit Metadata

用户可以显式发起本地 Incident 或 Evolution 记录删除。

本地元数据写入和删除必须：

- 限定在本地控制面
- 使用最小必要范围
- 记录审计结果
- 默认 Fail Closed
- 不包含 Secret

本地元数据能力不得被用于连接、

写入或删除任何受管生产数据库与业务资源。

---

# Architecture

Python

Typer CLI

SQLite

Pydantic

SQLAlchemy

AsyncSSH

Rich

Ollama Compatible API

OpenAI Compatible API

---

# Future

Web UI

Multi User

RBAC

Workflow

Kubernetes

Cloud

Plugin System

Remote Agent

Distributed Runtime

---

# Goal

不是替代 SRE。

而是成为：

SRE 的第二大脑。

负责：

观察

分析

规划

记录

学习

人类负责最终决策。
