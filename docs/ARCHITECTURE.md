# AIOps Agent Runtime Architecture

Version: MVP v1

---

# Overview

```
User
  │
  ▼
Typer CLI
  │
  ▼
Runtime Engine
  │
  ├── Context Builder
  ├── Skill Retriever ── read-only ──> Skill Registry
  │
  ▼
Planner
  │
  ▼
Policy Engine
  │
  ▼
Approval Decision
  │  NOT_REQUIRED, or valid Human Execution Approval
  │
  ▼
Executor
  │
  ▼
Tool Gateway
  │
  ▼
Registered Tool / Target
  │
  ▼
Structured Tool Result
  │
  ▼
Runtime Evidence Router
  │
  ▼
Verifier
  │
  ▼
Runtime State / Outcome
```

Runtime records the finalized Incident in Memory. After the production task has
reached a terminal outcome, an optional asynchronous Evolution Job may read a
sanitized Incident snapshot. Evolution is not part of the production execution
chain.

Policy alone determines whether human approval is required. The Approval Engine
records and validates that decision; it does not decide permissions. Every
Policy-allowed task enters `WAITING_FOR_APPROVAL`. A `NOT_REQUIRED` decision
passes through immediately; a task pauses there only when Policy requires a
Human Execution Plan Approval.

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

负责组合 Runtime 已提供的结构化证据：

系统信息

Docker

日志

配置

历史 Incident

Memory

最终生成：

Runtime Context

供 Planner 使用。

Context Builder 不能执行任何操作、调用 Tool、选择 Target 或创建子任务。
Runtime 是 Context 收集编排的唯一 owner。

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

Policy 从每个精确 Tool Version 的受保护 Metadata 读取 Risk Level。

多步骤 Plan 的 Effective Risk 至少等于所有 Step 风险的最高值。Policy 可以根据
目标范围、组合副作用和本地规则提高限制，但不能降低 Tool Metadata 的风险。

Policy 独自输出：

- `ALLOW` 和 `approval_requirement`
- `DENY` 和稳定原因

Policy 永远不能调用 LLM。

Policy 必须完全可预测。

---

## Layer 6

Approval Engine

负责记录并验证 Policy 之后的 Approval Decision。

Approval Engine 不决定权限，也不能降低 Policy 输出的
`approval_requirement`。每个 Policy 允许的计划都留下以下确定性结果之一：

- `NOT_REQUIRED`：由 Policy 产生；
- `APPROVED`：由授权人工产生；
- `REJECTED`：由授权人工产生；
- `EXPIRED`：由过期校验产生。

每个 Policy 允许的 Plan 都进入 `WAITING_FOR_APPROVAL` 审批决策门。
`NOT_REQUIRED` 会被记录并立即通过；只有 Policy 要求人工审批时才暂停等待。
`Commit` 是创建 Execution Plan Approval 的人工 CLI 动作，不是第三种授权对象。

L2：

- 需要显式人工 Approval/Commit。

L3：

- 需要显式人工 Approval。
- 每个 L3 Tool invocation 在 dispatch 前还需要一次独立、即时、不可重放的
  Manual Confirmation。

L2 和 L3 的授权都绑定：

- Plan Hash
- Steps
- Arguments
- User
- Expiration

Plan、Step 或 Arguments 发生任何变化，或者授权过期，都必须重新审批。
模型、Skill、Memory 和历史成功记录都不能降低审批要求。

当前 MVP 在一次性 L3 Manual Confirmation 协议实现并测试前，必须拒绝 L3
执行。

Execution Plan Approval 只授权一份精确的生产执行计划。它与 Skill Review
Approval 是不同的审批对象，两者不能互相替代。

---

## Canonical Risk and Approval Terms

Risk Level 属于 Tool Metadata，不是模型、Planner、Memory 或 Skill 的推理
结果。

| Risk Level | Canonical requirement |
|---|---|
| L0 | 普通只读操作；Policy 可以输出可审计的 `NOT_REQUIRED` |
| L1 | 敏感只读操作；由确定性 Policy 决定 `NOT_REQUIRED` 或要求人工审批 |
| L2 | 显式人工 Approval/Commit |
| L3 | 显式人工 Approval，并在每个 L3 Tool invocation 前立即进行 Manual Confirmation |

L2 Approval/Commit 绑定精确的 Plan Hash、Arguments 和 Expiration。每份 L3
Manual Confirmation 还绑定 Approval ID、具体 Step、Arguments、Execution
Attempt 和短 Expiration，并且只能消费一次。任何内容变化都会使原授权失效。
Policy 可以提高限制，但模型、Skill 和历史经验不能降低 Tool Metadata 所规定的
风险或审批要求。

---

## Plan Integrity and Hash

Execution Plan Approval 绑定不可变 Plan Snapshot。Plan 使用 UTF-8 RFC 8785
canonical JSON 和 SHA-256 生成 Plan Hash。

Hash 覆盖所有向审批人展示且可能影响行为的 Plan 内容，至少包括：

- Plan Schema Version、Task ID、Plan ID 和目标引用；
- 有序 Step 列表及 Step Role；
- 每个 Tool 的 ID、Version、Contract Hash 和 Implementation Hash；
- 每个 Step 的具体 Arguments 和目标范围；
- Expected Evidence、Verification 条件和执行顺序；
- Rollback 指导或明确的 `not_available`；
- 实际使用的 Skill ID、Version 和 Content Hash；
- 解释、限制和已声明副作用。

Hash 不包含 Plan Hash 字段本身，也不包含 Approval ID、Approver、Review
Timestamp 或 Expiration；这些字段由 Approval Record 单独绑定到 Plan Hash。

Tool Contract、Implementation Hash、Step、Arguments、Target、Verification、
Rollback 或 Skill provenance 任一发生漂移，旧 Plan 和 Approval 均失效。Runtime
不得静默重算 Hash 后继续执行。

---

## Layer 7

Executor

唯一允许真正执行 Tool 的地方。

Executor：

读取：

Approved Plan

↓

调用：

Tool Gateway

↓

Registered Tool

↓

收集 Structured Tool Result

↓

返回 Runtime

Runtime 在 `EXECUTING` 中协调操作步骤，在 `VERIFYING` 中让 Executor 执行
Plan 已包含的验证步骤，并将全部结构化结果交给 Verifier。

Executor 永远不能重新规划。

---

## Layer 8

Tool Gateway

Tool Gateway 是所有 Tool invocation 的唯一边界，只允许 Executor 调用。
Phase 2 只启用本地、确定性、无外部 I/O 的
`get_system_status@1.0.0` Mock Tool。Restart、SSH、Docker、HTTP、数据库、Shell、
网络和其他真实系统能力尚未进入此边界。

Phase 2 调用路径是：

```text
Approved immutable Plan
        ↓
Executor validates Plan and builds ToolCall
        ↓
Executor derives TargetReference and Arguments Hash
        ↓
Tool Gateway validates ToolCall
        ↓
Frozen Tool Registry exact resolution
        ↓
Contract / Implementation / Arguments / Target checks
        ↓
Private typed payload handler, at most once
        ↓
Payload validation and Redaction boundary
        ↓
Gateway-owned complete ToolResult envelope
        ↓
Global Result Schema + exact Contract output Schema
        ↓
Executor identity revalidation
        ↓
Runtime
```

Tool Gateway 负责：

- 严格重建不可变 ToolCall；
- 按精确 Tool ID 和 Version 从已冻结 Registry 解析；
- 校验不可变 Contract Hash 和 Implementation Hash；
- 以 Pydantic 和登记的 JSON Schema 双重校验具体 Arguments；
- 重算 RFC 8785 canonical Arguments Hash；
- 校验 Executor 提供的结构化 Target Reference 和单目标范围；
- 最多调用一次私有 typed handler；
- 校验 handler 只返回登记的 typed `data` payload；
- 按固定顺序执行 Secret 扫描、Redaction、payload size 和 evidence projection；
- 创建带 Invocation、Plan Step、Contract Hash、Arguments Hash、Target、duration、
  data、evidence 和 structured error 的完整 ToolResult；
- 先后用全局 Result Schema 和精确 Contract output Schema 校验完整 envelope；
- 拒绝未登记、未冻结、已禁用、Hash 漂移、Schema 不匹配或 scope 扩大的 Tool。

Tool Gateway 不负责 Policy、Approval、Plan 修改、Risk 推断、Target 发现、retry 或
Verification 判断。它不公开 raw handler、transport、callback 或 command interface。

### Tool Result and Failure Boundary

Tool handler 只接收 typed arguments，并只返回 typed payload。Invocation、Step、
Hash、Target、success/error、evidence 和 timing 均由 Gateway 创建，Tool 不能伪造。
Contract `output_schema` 校验完整 ToolResult envelope，Tool-specific payload 位于
`data`。

在可信的精确 Tool identity 建立之前，Gateway 不得使用未经验证的数据伪造
ToolResult：

| Boundary | Result |
| --- | --- |
| Gateway configuration invalid | sanitized explicit exception, zero dispatch |
| ToolCall malformed or untrusted | sanitized explicit exception, zero dispatch |
| exact Tool resolution fails | sanitized explicit exception, zero dispatch |
| Contract or Implementation Hash mismatch | sanitized explicit exception, zero dispatch |
| identity/hash 已可信后的 Arguments、Target、clock、handler、timeout、output 或 Redaction 失败 | declared structured ToolResult, at most one dispatch |

Executor 把前四类 exception 转换为显式 Runtime domain failure。exception 和
ToolResult 都不能包含 raw exception、stack trace、Secret 或未脱敏输入。

### Target Ownership

Executor 是 ToolCall 中 `TargetReference` 的 owner。它从已经验证、Policy 检查并
在需要时审批的不可变 Plan 确定性创建 Target；Planner、Tool 和 Gateway 都不能创建、
发现或扩大 Target。

Phase 2 的单目标 Mock 映射要求：

- `resource_type` 等于 Contract `target_scope.resource_type`；
- Contract selector 是 input Schema 中必填的字符串；
- `target_id`、`resource_id` 和 selector 值完全一致；
- `maximum_targets` 是 `1`；
- Gateway 不执行 DNS、文件、网络或远程资源解析。

### Redaction and Synchronous Timeout

Phase 2 Redaction field list 只表示 top-level payload properties。Gateway 依次执行
typed payload validation、JSON serialization、禁止 output field 检查、递归 Secret/
executable marker 扫描、RFC 8785 payload byte-size 检查、top-level safe evidence
projection、完整 envelope 构造、全局及 Contract Schema 校验。任何失败都会丢弃
unsafe payload 并返回无 `data` 的 sanitized structured failure。

Phase 2 Gateway 同步调用本地 Mock handler，并使用 monotonic nanosecond clock
在 handler 返回后检查 elapsed time。它不能中断、取消、隔离或 kill handler，也不
实现 transport timeout 或自动 retry。真实 timeout/cancellation 必须在未来 Executor
设计中另行批准。

### Tool Registry

Tool Registry 是 Tool Contract、Implementation Hash、Risk Metadata 和
Registry Status 的权威目录，由 Tool Gateway 使用并向 Policy 提供只读查询。

Registry 的 package artifact loader 必须同时加载并验证：

```text
src/ai_server/schemas/tool/tool-contract-v1.json
src/ai_server/schemas/tool/tool-result-v1.json
src/ai_server/schemas/tool/tool-replay-fixture-v1.json
src/ai_server/schemas/tool/tool-registry-record-v1.json
src/ai_server/schemas/tool/tool-implementation-bundle-v1.json
```

每个精确 Tool Version 的 package artifact set 包含 immutable Contract、独立
Registry Record、Implementation Bundle、`uv-tool-lock-v1` dependency lock 和
sanitized fixtures。Artifact loader：

- 从 schema-validated raw JSON 以 UTF-8 RFC 8785 + SHA-256 重算裸 64 hex
  Contract 和 Implementation Hash；
- 对 fixture 移除 root `content_hash` 后重算裸 64 hex Hash；
- 对 installed file bytes 和 dependency-lock bytes 校验
  `sha256:<64-lowercase-hex>`；
- 只接受 runtime ABI
  `python-source-v1.requires-python-ge-3.12`；
- 校验 manifest 中精确 `handler_entry_point`、`input_model_entry_point` 和
  `output_model_entry_point`；
- 证明这些 entry point 的 source modules 位于审核 manifest 并且 installed bytes
  未漂移；
- 校验 replay 的 identity、arguments/result hash、Target、sequence、redaction、
  Verification 和 global/exact Schemas；
- 失败时不产生部分 Registry entry。

Registry 只在 startup 接受显式 ToolDefinition。代码中绑定的 handler/input/output
对象的 qualified entry point 必须与 reviewed manifest 完全相同；Registry 还必须从
installed package 解析 reviewed entry point，并校验 model 和 handler function 的
精确对象 identity。bound method 的 `__self__` 必须是 reviewed owner class 的精确
实例类型；伪造 `__module__` 或 `__qualname__` 不能获得注册能力。ToolDefinition
不能提供 Metadata 或 Risk。Registry 从验证后的 Contract 生成 read-only Metadata。

Startup registration 完成后 Registry 必须 freeze。Freeze 前禁止 resolution 和
Metadata snapshot；freeze 后禁止新增或替换 entry。Policy 只读取 immutable
Metadata snapshot。Gateway 使用 private exact resolution；raw handler 不通过 public
Registry API 暴露。duplicate、unknown、non-registered 或 drifted identity 全部
fail closed。

模型、Planner、Skill、Memory、Evolution 和 Tool implementation 都不能写入 Risk
Level、Metadata 或 Registry Status。不可变 Tool Contract 与 Registry Record
分离。

Phase 2 没有 mutable registry database 或 Tool approval CLI，只接受 repository
reviewed、package-resident、`reviewer: local-owner` 的 bootstrap Registry Record。
该 record 仅使本地 deterministic Mock capability 可发现，不替代 Execution
Approval，也不能授权真实服务器或 mutating capability。

---

## Layer 9

Verifier

Verifier 只评估 Runtime 提供的结构化证据，不直接调用 Tool。

Execution Plan 必须预先包含执行步骤和验证步骤。Policy 检查全部步骤，Plan Hash
覆盖验证 Tool、具体 Arguments 和 Expected Evidence。Executor 经 Tool Gateway
执行验证步骤，Runtime 将其结构化结果交给 Verifier。

Verifier 可检查：

- 登记 Tool 返回的服务状态；
- Port 检查结果；
- HTTP Endpoint 检查结果；
- 结构化 Health Check；
- 证据是否完整、一致并满足 Plan 条件。

Verifier 失败或无法得出结论时，Runtime 不得报告成功，也不得由 Verifier 临时派发
新的 Tool 调用。

---

## Layer 10

Memory

负责：

- Incident 事实；
- 不可变任务与执行历史；
- 脱敏证据；
- 实际使用的 Skill ID、Version 和 Content Hash；
- 用户反馈和 Verification 结果。

Memory 是任务事实的权威来源。Skill Registry 可以维护可重建的使用指标投影，但
Skill Bundle、Lifecycle Status、Review Record 和 active pointer 只由 Skill
Registry 管理。

Memory 永远不参与权限判断、审批、Skill 激活或 Tool 执行。

### Incident Evidence Contract

进入 Memory 或 Evolution 的 Incident Snapshot 至少记录：

- Incident ID、Task ID、Runtime terminal outcome 和时间范围；
- Service、Symptom、Environment Fingerprint 和 Error Signature；
- Root Cause Classification，以及 unknown/inconclusive 标记；
- Plan Hash、Tool Invocation/Result 引用和 Verification Result；
- 实际使用的 Skill ID、Version 和 Hash；
- Recovery/Rollback Result 和用户反馈；
- Source Hash、Redaction Policy Version、Redaction Status 和 Data Quality。

Snapshot 使用字段 allowlist，禁止 Secret、未脱敏环境变量、生产凭据和无界原始
日志。`COMPLETED` 与 `FAILED` 任务在 Incident Finalization 后都可以提供 Evolution
证据；活跃任务不能。

---

# Post-task Learning Components

以下组件不属于生产任务主状态机，也不位于 Executor 的生产调用路径中。

## Evolution Engine

Evolution Engine 负责：

- 在 Runtime terminal outcome 固化后读取经过脱敏的 Incident Evidence
- 分析重复模式
- 生成 Candidate Skill
- 协调静态验证和离线评测
- 将候选提交给人工 Skill Review

Evolution Engine 不能：

- 修改 Runtime、Policy Engine、Tool 实现或 Risk Level
- 调用 Executor、Tool Gateway 或生产目标
- 改变已经终态化的 Runtime 结果
- 自动激活 Candidate Skill

Evolution 是可选、异步、失败隔离的独立生命周期。生产任务不依赖
Evolution 才能终态化，Evolution 的任何失败都不能反向影响生产任务。

## Skill Registry

Skill Registry 是 Skill 版本、状态和完整性记录的权威来源。它负责：

- 存储不可变 Skill 版本
- 保存 Content Hash
- 保存评测和人工审核记录
- 管理 `CANDIDATE`、`APPROVED`、`ACTIVE`、`DEPRECATED` 等状态
- 提供受状态约束的 Skill 检索
- 原子激活、禁用和回滚
- 从 Memory/Audit 的不可变使用事实重建指标投影

Skill Registry 不执行 Tool，不持有生产凭据，不决定 Policy，也不授予生产
执行权限。默认运行时检索只能返回已经人工审核并处于 `ACTIVE` 状态的 Skill。

Candidate Generator 生成可解析内容并由系统计算 Hash 后，必须先将不可变
`CANDIDATE` 注册到 Skill Registry。后续 Validation、Evaluation、Review 和
Activation 事件由 Registry 记录在该具体版本上；Evolution Controller 只记录
Evolution Job 状态。

## Skill Retriever

Skill Retriever 是 Runtime 调用的只读组件。它：

- 从 Skill Registry 读取 `ACTIVE` Skill；
- 执行结构化兼容性过滤和可审计排序；
- 返回精确 Skill ID、Version、Content Hash 和排序理由；
- 不选择权限，不生成 Plan，不执行 Tool。

Runtime 将 Ranked Skill Matches 交给 Planner。Planner 不能直接查询或修改 Skill
Registry。

## Skill Review Approval

Skill Review Approval 只允许一份经过验证和评测的具体 Skill 版本进入激活
流程。它至少绑定：

- Candidate ID
- Skill ID
- Version
- Content Hash
- Evaluation Result Hash
- Reviewer
- Timestamp
- Expiration

Skill 内容、版本、Hash 或评测结果发生变化后，旧审核立即失效。Skill Review
Approval 不等于 Execution Plan Approval，不能授权 Tool 调用，也不能绕过
Policy、Approval Engine、Executor 或 Verifier。

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

每个 Policy 允许的 Plan 使用以下主路径：

```text
POLICY_CHECK

↓

WAITING_FOR_APPROVAL

↓

EXECUTING
```

`WAITING_FOR_APPROVAL` 是唯一正式名称，表示统一审批决策门。Policy 输出
`NOT_REQUIRED` 时，Runtime 进入该状态、记录可审计的 Approval Decision，并立即
进入 `EXECUTING`，不等待人工。L2/L3 必须停留并等待相应授权；任何风险等级都不能
绕过 Policy 或审计。L3 的即时确认在每个具体 Tool 调用前执行，不属于
`WAITING_FOR_APPROVAL → EXECUTING` 的状态迁移条件。

当前终态：

```
COMPLETED

FAILED
```

以下是为后续阶段保留、当前不可达的 RuntimeState 枚举：

```text
PARTIAL_SUCCESS

ROLLBACK

MANUAL_INTERVENTION_REQUIRED
```

具体迁移以 `STATE_MACHINE.md` 为唯一权威。

Evolution Job 使用独立状态机。它不是 Runtime State，不能追加在任何 Runtime
terminal outcome 后成为生产任务状态。

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

Approval Decision

`NOT_REQUIRED` or valid Human Execution Approval

↓

Executor

↓

Tool Gateway

↓

Registered Tool / Target

↓

Structured Tool Result

↓

Runtime Evidence Router

↓

Verifier

↓

Runtime Outcome

↓

Incident Memory
```

---

# Tool Flow

```
Planner

↓

Execution Plan

↓

Policy

↓

Approval Decision

`NOT_REQUIRED` or valid Human Execution Approval

↓

Executor

↓

Tool Gateway

↓

Registered Tool

↓

Target

↓

Structured Tool Result

↓

Runtime Evidence Router

↓

Verifier

↓

Runtime Outcome
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

Context Builder 不调用 Tool。Runtime 先使用 Task、受信 Target 配置和脱敏 Memory
构造最小 `BootstrapContext`；Skill Retriever 只能基于这些结构化字段检索 Context
Skill。匹配不足时不使用 Context Skill，禁止用模型猜测 Profile。

Context Skill 只能建议预登记 Context Profile 的精确 ID、Version、Content Hash、证据
类型和预算，不能提供命令、地址、凭据或自由参数。Context Profile Registry 是 Profile
内容、状态和 Hash 的权威来源。Profile 必须绑定 allowlisted read-only Tool 的精确
Version、受信 Target 参数映射、预算和停止条件；模型和 Skill 不能创建或修改 Profile。

需要远程观察数据时，Runtime 从 Profile 创建一个 linked `OBSERVATION` Task，而不是在
父任务的 `CONTEXT_BUILDING` 内隐藏执行。Observation Task 使用受信 Target Reference，
完整经过主 Runtime 状态机、Policy、Approval Decision、Executor、Tool Gateway 和
Verifier。父任务保持 `CONTEXT_BUILDING`，只消费子任务终态后的结构化结果。
Observation Task 禁止递归创建新的 Observation Task。

---

# Memory Flow

```
Execution

↓

Verification

↓

Finalized Incident

from `COMPLETED` or `FAILED`

-. optional asynchronous trigger .->

Sanitized Incident Evidence

↓

Evolution Job

↓

Candidate Draft

↓

Strict Parse / System IDs / Content Hash

↓

Skill Registry `CANDIDATE`

↓

Validation and Offline Evaluation

↓

Skill Review Approval

↓

Atomic Registry Activation
```

This is a post-task learning data flow, not an extension of the Runtime state
machine. Memory supplies facts; it does not decide whether a Candidate is safe,
approved, or active.

---

# Module Responsibilities

## Runtime

生命周期管理。

---

## Context Profile Registry

管理不可变、版本化的 Context Profile、Content Hash 和 Registry Status；只向 Runtime
提供精确只读解析，不执行 Tool、不选择权限。正式 Schema 和 Registry Record 不存在
前，不得创建远程 Observation Task。

---

## Planner

思考。

---

## Policy

决策。

---

## Approval

记录并校验 Policy Decision，以及 Policy 要求时绑定精确 Execution Plan 的人工授权。

---

## Executor

执行。

---

## Verifier

确认。

---

## Memory

保存 Incident 事实、历史和脱敏证据；不授权、不执行。

---

## Evolution

从已终态化、脱敏并固化的 Incident 生成、验证和评测 Candidate Skill；不执行生产操作。

---

## Skill Registry

管理不可变 Skill 版本、审核记录、状态和激活指针；不执行 Tool，不参与
权限判断。

---

## Skill Retriever

从 Registry 读取并排序 `ACTIVE` Skill；只返回精确版本和理由，不规划、不授权。

---

## Tool Gateway

只接受 Executor 的 ToolCall；从 frozen artifact-driven Tool Registry 精确解析，
校验 Hash、Arguments 和 Target，最多调用一次 private typed payload handler，并
创建及验证完整 ToolResult envelope；不规划、不审批、不推断 Risk、不 retry。

---

## Tool

提供一个小型 typed bounded capability；不规划、不授权。Phase 2 唯一 Tool 是
无外部 I/O 的 deterministic Mock。真实世界连接只能由后续阶段按 Policy、
Approval、Executor 和 Tool Gateway 边界另行引入。

---

# Dependency Rules

允许：

```
Planner

↓

Policy

↓

Approval Decision

↓

Executor

↓

Tool Gateway

↓

Registered Tool / Target

↓

Structured Tool Result

↓

Runtime Evidence Router

↓

Verifier

↓

Runtime Outcome
```

Runtime support and post-task learning dependencies are limited to:

```
Runtime → Memory

Evolution Engine → Memory

Evolution Engine → Skill Registry

Runtime → Skill Retriever

Skill Retriever → Skill Registry

Runtime → Context Profile Registry (read-only)

Policy → Tool Registry (read-only)

Tool Gateway → Tool Registry
```

Runtime records finalized Incidents in Memory. Evolution Engine can only read
sanitized evidence from terminal, finalized Incidents. Evolution Engine can read
existing Skill versions and submit Candidate versions to Skill Registry, but it
cannot activate content without a valid Skill Review Approval. Planner receives
eligible reviewed Skill Matches only through Runtime and Skill Retriever;
retrieval never conveys authority.

Policy reads authoritative Risk Metadata from the exact Tool Registry version.
Tool Gateway resolves the same Contract and Implementation Hash. Neither
component may mutate Registry metadata during a task.

禁止：

Tool → Planner

Planner → Tool Gateway

Policy → Tool Gateway

Approval → Tool Gateway

Context Builder → Tool Gateway

Memory → Tool Gateway

Executor → Tool Registry

Memory → Executor

Memory → Policy

Memory → Approval

Planner → SSH

Planner → SQLite

Policy → LLM

Verifier → Tool Gateway

Verifier → Planner

Evolution Engine → Executor

Evolution Engine → Tool Gateway

Evolution Engine → Policy mutation

Evolution Engine → Runtime state mutation

Skill Registry → Executor

Skill Registry → Tool Gateway

Skill Registry → Policy

Context Profile Registry → Executor

Context Profile Registry → Tool Gateway

Planner → Skill Registry

Planner → Skill Retriever

保持单向依赖。

---

# Design Rules

Runtime 是唯一入口。

Planner 不执行。

Executor 不思考。

Policy 不推理。

Memory 不决策。

Verifier 不调用 Tool。

Tool 不规划。

Evolution 不执行生产操作，不修改权限。

Skill Registry 不执行 Tool，不授予执行权限。

Skill Retriever 不规划、不授权。

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
