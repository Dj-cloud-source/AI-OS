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

自动执行

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

需要 Commit

例如：

Restart Service

Restart Container

Rollback

Deploy Known Version

---

L3

高风险操作

必须审批

例如：

Delete

Firewall

Database Write

Permission Change

Secrets

---

# Runtime Flow

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

Review

↓

Commit

↓

Execute

↓

Verify

↓

Incident Memory

↓

Skill Evolution

---

# Approval Model

Prepare

↓

Review

↓

Commit

↓

Execute

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

重复成功：

生成 Candidate Skill。

Candidate Skill：

必须经过：

测试

人工审批

才能升级。

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

数据库写

删除

权限修改

Secrets

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