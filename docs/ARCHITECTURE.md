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

Policy alone determines whether human approval is required. Runtime records the
Policy-produced `NOT_REQUIRED` lifecycle decision. The Approval Engine owns and
validates only human Review, Plan Approval, Confirmation, and authorization
ledger facts; it does not decide permissions. Every Policy-allowed task enters
`WAITING_FOR_APPROVAL`. A `NOT_REQUIRED` decision passes through immediately; a
task pauses there only when Policy requires a Human Execution Plan Approval.

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

`RuntimeEngine` 的 Python 组件注入点属于进程内 trusted composition/test seam，
不是供模型、Skill、环境变量、配置文件或第三方插件选择实现的扩展接口。被注入的
Executor 位于 TCB 内；普通结构校验与 SHA-256 绑定不能证明一个恶意 Executor 真的调用过
handler。当前受支持的 CLI 只构造内置 Runtime、冻结 Registry、内置 Executor/Gateway 和
内置 Verifier，不从外部输入选择这些组件。未来若允许可配置执行组件，必须先通过独立
RFC，引入 Runtime-owned dispatch journal 或等价来源证明，并补充隔离与供应链审查。

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

负责记录并验证 Policy 之后的人工 Review、Plan Approval、Confirmation 和消费事实。
Policy 产生的 `NOT_REQUIRED` 由 Runtime lifecycle audit 记录，不写入人工授权 ledger。

Approval Engine 不决定权限，也不能降低 Policy 输出的
`approval_requirement`。每个 Policy 允许的计划都留下以下确定性结果之一：

- `NOT_REQUIRED`：由 Policy 产生并由 Runtime 记录；
- `APPROVED`：由授权人工产生；
- `REJECTED`：由授权人工产生；
- `EXPIRED`：由过期校验产生。

每个 Policy 允许的 Plan 都进入 `WAITING_FOR_APPROVAL` 审批决策门。
`NOT_REQUIRED` 会被记录并立即通过；只有 Policy 要求人工审批时才暂停等待。
`Commit` 是创建 Execution Plan Approval 的人工 CLI 动作，不是第三种授权对象。

Phase 4 的 Approval Engine 只维护当前进程内的 Review Session、不可变授权记录和
追加式审计事件。固定 Operator 是 `local-user`，固定本地控制面 Approver 和 L3
Confirmer 是 `local-owner`。这只是单用户本地信任声明，不是密码学身份认证。
Review Session 和 Plan Approval 的最大有效期均为 300 秒，L3 Manual
Confirmation 的最大有效期为 30 秒；数值来自受审 Policy Profile，CLI、模型和
环境变量不能扩大。

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

Phase 4 已实现和隔离测试一次性 L3 Manual Confirmation 协议，但不连接 Executor，
因此当时生产 Policy 固定拒绝 L3。Phase 5 把确认消费与精确 invocation dispatch
接到同一个受控边界。默认 Registry 仍只有 L0 Mock Tool；解除临时的框架级 L3
拒绝不会注册、激活或授权任何 L3 Tool。

Phase 5 的消费语义是本进程安全边界，不是分布式事务：

- Approval Engine 在线程安全的 ledger 中线性化消费 Approval 或 Confirmation；
- Plan Approval 在创建精确 execution attempt 时消费，Runtime 只能在成功后进入
  `EXECUTING`；即使尚未 dispatch 就失败，该 Approval 也会烧毁；
- 对每个 L3 invocation，Executor 已经固定 ToolCall 的全部绑定内容，并在消费
  Confirmation 后于同一调用栈中紧邻执行 dispatch；
- Runtime 不得回滚消费或自动重试，系统也不声称本地消费与远端副作用具有事务
  原子性。
- 同一精确 Plan Hash 的多份 sibling Approval 也只能启动一个 attempt；首次消费后，
  其余 sibling Approval 不能再次 dispatch。重试必须生成新的 Plan identity 并重新审批。
- 同一 attempt 同时只能有一个活动 Executor 调用；Confirmation reader 不能重入执行、
  验证或关闭该 attempt。

每份 L3 Confirmation 由完整 Challenge Hash 驱动。Hash 覆盖除
`challenge_hash` 自身外的完整严格 `ManualConfirmationChallenge`，包括 Schema
Version、Authorization Hash、Approval ID、Approval Plan Hash、Approval Record Hash、
Approval Expiration、Execution Attempt ID、Invocation ID、Step Index/ID/Role、Tool
ID/Version、Contract Hash、Implementation Hash、Arguments Hash 和 Target。CLI 展示
完整绑定事实，只接受精确的 `CONFIRM <challenge-hash>`；Confirmation 仍是一次 invocation
的执行门，不是 Runtime state。Phase 5 唯一支持的生产应用适配器是交互式 CLI，
它要求输入和输出同时为 TTY。Runtime/Executor 的 reader 注入只是可信进程内测试
seam，不是人工来源证明，不得连接模型、Skill、Tool、pipe 或环境变量。默认
Registry 没有 L3 Tool；注册首个生产 L3 Tool 前，必须通过独立受审设计把该 seam
封装或替换为可验证来源的交互式 Confirmation port，否则生产 L3 保持不可用。

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

L0/L1 是只读风险类别，Contract 和 Registry registration 都必须拒绝
`mutates_remote_state=true` 的 L0/L1 Tool。L2/L3 可以是只读 Tool，但这不会降低
它们既定的审批要求。

L2 Approval/Commit 绑定精确的 Plan Hash、Arguments 和 Expiration。每份 L3
Manual Confirmation 的 Challenge Hash 覆盖除自引用 Hash 外的完整严格模型，包括
Authorization、Approval、Plan、Approval Record/Expiration、具体 Step、精确 Tool
identity 和 integrity hashes、Arguments、Target、Execution Attempt 和 Invocation；
签发的 Confirmation Record 另行绑定短 Expiration，并且只能消费一次。任何内容变化
都会使原授权失效。
Policy 可以提高限制，但模型、Skill 和历史经验不能降低 Tool Metadata 所规定的
风险或审批要求。

---

## Plan Integrity and Hash

Execution Plan Approval 绑定不可变 Plan Snapshot。Plan 使用 UTF-8 RFC 8785
canonical JSON 和 SHA-256 生成 Plan Hash。

当前简化 ExecutionPlan 不直接作为 Hash 输入。Phase 4 从严格 Plan、
PolicyDecision 和冻结 Registry Metadata 确定性构建
`PlanApprovalSnapshot`，补齐完整 TargetReference、明确顺序、Target Scope、
Side Effects、Arguments Hash、Registry Risk/Redaction/Verification/Rollback
元数据，以及当前明确为空的 Skill provenance 和 limitations。Phase 6 将
ExecutionPlan 和 `PlanApprovalSnapshot` 同步升级为 Schema v2，并把有序、结构化的
Plan-level `verification_criteria` 纳入快照；v1 快照不能授权 v2 Plan，也不得被静默
迁移或重新签名。该快照始终是唯一 Plan Hash 输入。
Approval Engine 私有内存保留精确、安全可审阅的快照；ApprovalRecord、普通日志和
审计事件只保存 Plan Hash 与有序 Arguments Hash commitment，不保存原始参数。
终端 Review 必须展示每个 criterion 的完整精确结构，包括 kind、evidence Step、
evaluator version、expected 值和 freshness 上限；不得只展示可与快照内容分离的摘要。

Hash 覆盖所有向审批人展示且可能影响行为的 Plan 内容，至少包括：

- Plan Schema Version、Task ID、Plan ID 和目标引用；
- 有序 Step 列表及 Step Role；
- 每个 Tool 的 ID、Version、Contract Hash 和 Implementation Hash；
- 每个 Step 的具体 Arguments 和目标范围；
- Expected Evidence、有序 `verification_criteria` 的完整 discriminated-union 内容和
  执行顺序；
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

ExecutionPlan 的角色顺序必须是零个或多个 `OBSERVE`/`ACTION` Step 组成的前缀，
随后是零个或多个 `VERIFY` Step 组成的后缀。出现 `VERIFY` 后不得再出现
`OBSERVE` 或 `ACTION`。Runtime 只能在 `EXECUTING` 请求 Executor 执行前缀，
并且只能在 `VERIFYING` 请求 Executor 执行后缀。L3 Tool 只能出现在 `ACTION`
Step；L3 `OBSERVE` 或 `VERIFY` 在 dispatch 前结构校验失败。

Executor 永远不能重新规划，也不能让调用者提供任意 cursor、跳过 Step、调整顺序
或替换 Arguments。它在 dispatch 前验证完整 Plan 结构，并为“当前 invocation”
验证全部适用前置条件：精确 Policy、仍有效的 Plan Approval，以及当该 Step 为 L3
时的即时 Confirmation。尚未轮到的 L3 Step 的短期 Confirmation 不是更早
invocation 的前置条件。

需要人工 Approval 时，Executor 创建 execution attempt 并消费 Approval。消费成功
后 Runtime 才能进入 `EXECUTING`。每个 L3 invocation 的固定流程是：

```text
Build exact ToolCall
→ Build and display complete bound Challenge
→ Human enters CONFIRM <challenge-hash>
→ Issue and consume the single-use Confirmation
→ Immediately dispatch the already-bound ToolCall
```

Executor 返回结构化 ExecutionReport，而不是自行改变 Runtime state。
ExecutionReport 至少绑定 Attempt、Task、Plan、Policy Decision、可选 Approval、
有序 Step 结果、总计划耗时、失败位置、每步 dispatch/effect certainty、是否需要
人工介入，以及仅为 `VERIFYING` 或 `FAILED` 的 next-state fact。Runtime 是唯一状态
owner；Executor 永远不能返回 `COMPLETED`。

如果 Executor 的最终报告不可信或缺失，并且 Runtime 也无法确认 attempt 已安全关闭，
Runtime 不得伪造 ExecutionReport、Step、Invocation 或 ToolResult。它改为生成
hash-bound `ExecutionUncertainty` 并进入 `FAILED`：

- 只绑定精确 `ExecutionAttemptAuthorization`；
- 可以通过 `prior_report_hash` 绑定最后一份已验证的
  `AWAITING_VERIFICATION_DISPATCH` 报告，并保留其中可信结果；
- 尚未调用任何 dispatch-capable Executor 边界时记录
  `NOT_DISPATCHED` / `NONE`，不要求人工介入；
- 已调用该边界但没有可信最终闭合证据时记录 `UNKNOWN` / `UNKNOWN`，并要求人工介入；
- 使用稳定原因 `execution_abort_uncertain`，单独写入
  `execution_uncertainty_audit`，不虚构 per-Step 审计字段。

`ExecutionUncertainty` 只描述 attempt closure 和可能未报告的 dispatch/effect，不能
授权重试或恢复 dispatch。只有可信 ExecutionReport 可提供具体 Step/Invocation 事实。

Runtime 提供 same-process `resume_approved` 控制面，只接受与暂停 Outcome 精确绑定的
Approval。交互式 CLI 在成功 `COMMIT <plan-hash>` 后立即恢复同一 Outcome；底层
Approval issuance API 只签发授权，不隐式执行。跨进程恢复仍属于 Phase 9。

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
Executor 在保留 receipt 前、Runtime 在保留任意成功或失败 ExecutionReport 前，
都会通过同一 frozen Registry 独立复核完整 ToolResult：精确 identity、Arguments、
Target、payload model、Contract output Schema、declared error、timeout、Secret 扫描、
Redaction 和 evidence projection。任何不一致都丢弃该结果并 fail closed。
ToolResult 的 retained evidence 容器深度不可变，序列化只返回新的普通 JSON 容器。

Phase 5 在 Gateway 内部增加由 Gateway 产生、进程内使用的 dispatch receipt，
记录 private handler 边界是否已经进入。该 receipt 只供 Executor 生成
ExecutionReport，不属于 Tool Protocol v1，不写入 ToolResult v1，不改变任何已
审核 Tool Contract、Schema 或 Contract Hash。Executor 仍须严格验证 receipt；
若无法获得可信 receipt，则使用保守的 `UNKNOWN` fallback，不能自行推断未派发。

ExecutionReport 使用 receipt 和权威 Side Effects Metadata 表达安全事实：

| Dispatch certainty | Effect certainty |
| --- | --- |
| handler 前失败：`NOT_DISPATCHED` | `NONE` |
| read-only handler 已进入：`HANDLER_DISPATCHED` | `NONE`，但结果仍可失败 |
| mutating handler 已进入并返回成功：`HANDLER_DISPATCHED` | `PENDING_VERIFICATION` |
| mutating handler 已进入但结果或 outcome 不确定：`HANDLER_DISPATCHED` | `UNKNOWN`，进入 `FAILED` 并要求人工介入 |
| 无可信 receipt，不能证明 handler 前失败：`UNKNOWN` | `UNKNOWN`，进入 `FAILED` 并要求人工介入 |

“handler 已进入”不等于副作用已发生或成功；只有独立 Verification 可以确认计划
目标。若无法可靠证明 handler 前失败，系统不得标记 `NOT_DISPATCHED`。
已接受的 `AWAITING_VERIFICATION_DISPATCH` ExecutionReport 是后续报告的不可回退
前缀。Verification 或 abort 的累计报告必须原样保留其 records 和 events 并增加
闭合进度；截断、改写或重放不能抹除先前已确认的 dispatch 事实。

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
实现 transport timeout 或自动 retry。

Phase 5 不改变这一 Tool Protocol 语义：当前同步 Mock 的单次 invocation timeout
仍以 Gateway 的 post-return elapsed check 为权威，超时返回 `tool_timeout`。
Executor 另外记录整个计划的 monotonic elapsed time，将 `tool_timeout` 视为本次
attempt 的终止失败，并且不自动 retry。计划耗时记录不是抢占、取消或 transport
timeout。真实 transport timeout/cancellation 必须在引入真实 transport 的后续阶段
单独设计和批准。

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

每个 Execution Plan 必须预先声明至少一个有序、结构化且
`mandatory=true` 的 Plan-level `verification_criteria`。Criterion 至少绑定唯一
`criterion_id`、精确 `evidence_step_id`、整数 `maximum_age_ms`（0–30000，默认
30000）、固定 `evaluator_version="1"` 和以下四种 `kind` 之一：

| `kind` | 确定性语义 |
| --- | --- |
| `equals` | `source=data|evidence`，`field=source|simulated|target|hostname`，对严格 `str|bool` expected 执行同类型相等；不强制转换 |
| `numeric_bounds` | `field=cpu_percent|memory_percent|disk_percent`，对有限数值执行声明的包含式 `minimum` 与 `maximum` 检查；`bool`、NaN 和 Infinity 失败 |
| `expected_state` | 以精确 `service_name` 选择唯一 service，并与 `expected_state=running|stopped` 精确匹配；缺失或重复名称失败 |
| `health_status` | 使用 `expected_status=healthy|unhealthy` 和 `maximum_utilization_percent`；healthy 仅指 service 非空且名称唯一、全部为 `running`，且三项 utilization 都不超过上限 |

四类 criterion 使用严格 discriminated union 承载各自期望值。Criterion 不接受
JSONPath、点路径、正则、表达式、回调、脚本或其他动态字段选择；字段提取只由代码中
针对精确 Tool ID、Version、Result Schema 和 evaluator version 的静态类型逻辑完成。
所有 field/source/state selector 都是上述封闭枚举，不是动态路径。未知 kind/version、
重复 criterion ID、未引用 Plan Step，或引用无对应成功结果的 Step 均 fail closed。
`health_status` 遇到重复 service 名时是 contradictory evidence 并直接失败，不能把它
解释为普通 `unhealthy`；在证据无矛盾时，`unhealthy` 是上述 healthy 谓词的否定。

Contract 的 `verification.evidence_fields` 是必须保留的最小 safe evidence 投影，
不是任意字段白名单：`source=evidence` 只能选择该列表中的字段；typed `data` evaluator
只能读取精确 registered output model 的固定字段，并继续受 output Schema 和 redaction
约束。当前 Mock 的默认 mandatory criteria 只使用其 Contract-required
`simulated`、`source` 和 `target`；`hostname` 不是该 Tool 的默认 mandatory criterion。

额外的 `VERIFY` Tool Step 只在被执行 Tool 的不可变 Contract 明确列出验证 Tool 时
存在，必须只读且非 L3，并且必须被至少一个 criterion 引用。每个被执行
Tool Contract 列出的 required verification reference 都必须在 Plan 中有精确的
`VERIFY` Step 和对应 criterion，不得只覆盖其中一部分。只读 Tool 的 Contract 若声明可由自身
结构化结果提供所需证据，则 criterion 可引用该 `OBSERVE`/`ACTION` Step 的结果；不得
为了验证而重复调用同一 Tool。当前只读 `get_system_status@1.0.0` 采用这种自证据模式，
因此没有额外 `VERIFY` 调用。任何 `mutates_remote_state=true` 的 Tool 都必须至少有一个
criterion 引用独立、只读、Contract-declared 的 `VERIFY` Step；mutating Action 自身的
返回值不能闭合其副作用。

所有 `mutates_remote_state=true` 的 source Step 都必须使用 `ACTION` role；不得用
`OBSERVE` 误标变更。Phase 6 暂时限制每个 Plan 至多一个 mutating source Step。该 Step 的每个 required
verification reference 都必须由至少一个有意义的后置条件覆盖，即
`numeric_bounds`、`expected_state` 或 `health_status`；仅证明来源或目标相等的
provenance `equals` 不能验证变更效果。支持多个 mutating source Step 之前，必须先引入
精确 action-to-criterion/effect 绑定并完成独立架构审阅；不得靠 criterion 共享隐式扩大闭合范围。

Policy 检查全部 Step。Plan v2 Hash 覆盖验证 Tool、具体 Arguments、Expected Evidence
和完整有序 criteria。Executor 经 Tool Gateway 执行 Contract 要求的验证步骤，Runtime
验证累计 ExecutionReport 后建立不可变 `VerificationContext`，再将证据交给 Verifier。
Context 的严格字段是 `context_schema_version`、`task_id`、`plan_id`、`plan_digest`、
`execution_attempt_id`、`execution_report_hash`、Runtime-owned
`evidence_accepted_at`、与之为同一 Runtime 可信时钟样本的 `evaluated_at`、Runtime 保守计算的
`collection_duration_ms` 和 `mutating_effect_pending`；schema version 固定为 `"1"`，
duration 是 0–3600000 的整数，非 null 时间必须是 exact timezone-aware UTC。Verifier
不接受调用方另行提供或 Tool 声称的采集时间。Schema 允许
`evidence_accepted_at=null` 仅为安全表示 Runtime 无法取得可信接收时间；它固定产生
`CLOCK_UNAVAILABLE` 且永远不能通过验证。

Runtime 在完成 report/result revalidation 后只取一次本地 UTC 时钟样本，并把该同一
样本同时用作 `evidence_accepted_at` 和 `evaluated_at`；它是本地接收事实，不是远端事件时间。
`collection_duration_ms` 是 Executor `total_duration_ms` 与从 Runtime 进入 `EXECUTING`
到接受 report 的 elapsed 毫秒向上取整值之较大者，上限为 3,600,000 ms：

```text
collection_duration_ms = min(
    max(
        ExecutionReport.total_duration_ms,
        ceil_ms(evidence_accepted_at - Runtime EXECUTING entered_at)
    ),
    3_600_000
)
```

为了不把整份 report 的最后接收时刻误当成所有证据的采样时刻，freshness 使用保守年龄：

```text
conservative_age_ms =
    evaluated_at - evidence_accepted_at + collection_duration_ms
```

Runtime 不信任更短的单方时长。时钟样本不可用第二次读取向前移动 evaluation time。
负时钟差、非 UTC 时间、负或溢出的 duration、
以及 `conservative_age_ms > maximum_age_ms` 都产生稳定失败；绝不因本地接收时间存在而声称
远端时钟可信。

Verifier 返回严格、不可变并带 canonical content Hash 的 `VerificationResult`。其字段
是 `result_schema_version`、`task_id`、`plan_id`、`plan_digest`、
`execution_attempt_id`、`execution_report_hash`、`evaluated_at`、
`status`、`checks`、`evidence_references`、`failure_reasons`、
`effect_disposition`、`human_intervention_required` 和 `content_hash`。
`result_schema_version` 固定为 `"1"`。
`VerificationStatus` 和 `VerificationCheckStatus` 都只有 `PASSED | FAILED`；
`VerificationEffectDisposition` 只有 `NONE | VERIFIED | UNKNOWN`：

- 每个 `VerificationCheckResult` 仅包含 `criterion_id`、`evidence_step_id`、
  `evaluator_version`、`status` 和可选 `failure_reason`；pass 不得带 reason，fail 必须带
  一个 reason，且不保留 raw observed value。
- 每个 `VerificationEvidenceReference` 仅包含 `step_index`、`step_id`、`invocation_id`、
  `tool_id`、`tool_version`、`contract_hash`、`implementation_hash`、
  `arguments_hash`、`target`、`result_hash` 和 `accepted_at`；它是 Hash-only provenance，
  不复制 raw evidence。

- `PASSED` 仅当每个 mandatory criterion 对正确 Target 的新鲜、完整、一致证据都通过；
  此时只读计划的 effect 是 `NONE`，先前 pending 的 mutating effect 才能成为
  `VERIFIED`。
- `FAILED` 必须至少包含一个稳定原因。缺失、malformed、stale、矛盾、未计划、重排、
  identity/hash 不匹配或无法判定的证据都属于失败，不能降级成警告。
- 若 `mutating_effect_pending=true` 且结果不是 `PASSED`，`effect_disposition` 必须为
  `UNKNOWN` 且 `human_intervention_required=true`；只读验证失败保持
  `effect_disposition=NONE`。

`VerificationFailureReason` 是以下封闭枚举；不得把异常文本或自由字符串写入结果：

| Reason | Meaning |
| --- | --- |
| `MALFORMED_PLAN` | Plan 或 criterion 不能严格重建 |
| `MALFORMED_EVIDENCE` | evidence 容器、结果或嵌套 payload 形状非法 |
| `PLAN_BINDING_MISMATCH` | Task、Plan、digest、Attempt 或 report 绑定不匹配 |
| `MISSING_EVIDENCE` | Plan-required result 缺失 |
| `EXTRA_EVIDENCE` | 出现未计划或未引用的 result |
| `EVIDENCE_ORDER_MISMATCH` | evidence 顺序不等于批准顺序 |
| `EVIDENCE_IDENTITY_MISMATCH` | Step、Tool、Contract、Arguments 或 result Hash 身份不匹配 |
| `TOOL_VERSION_MISMATCH` | evidence Tool Version 不匹配 |
| `TARGET_MISMATCH` | target correlation 失败 |
| `DUPLICATE_INVOCATION_ID` | 累计 report 中 Invocation ID 不唯一 |
| `UNSUCCESSFUL_TOOL_RESULT` | criterion 引用的 ToolResult 不是成功 envelope |
| `STALE_EVIDENCE` | conservative age 超过 criterion maximum |
| `CONTRADICTORY_EVIDENCE` | 同一结构化 subject 存在冲突，例如重复 service 名 |
| `CRITERION_EVIDENCE_MISSING` | 某 criterion 的精确 source/field/subject 不存在 |
| `CRITERION_MISMATCH` | 证据存在且有效，但不满足 expected condition |
| `VERIFIER_RESULT_INVALID` | 返回结果类型、绑定、顺序、Hash 或闭合语义非法 |
| `VERIFIER_FAILED` | Verifier 失败或抛出异常，细节被脱敏 |
| `CLOCK_UNAVAILABLE` | Runtime 无法取得可信单次 UTC evaluation time |

Result 的 content Hash 只证明序列化内容未漂移，不证明结论正确。Runtime 在调用可注入
Verifier 之前，会对 Plan、全部 ToolResults 和 VerificationContext 逐个做 exact-type
strict rebuild，并只把这些隔离副本交给 Verifier；Verifier 不获得 Runtime 保留的权威对象
引用。Runtime 另行严格重建返回对象，并在自身信任域内用同一组纯 evaluator 和
原始可信 `VerificationContext` 重新计算期望结果；返回结果必须与重算结果完整相等。
Runtime 还拒绝重复 Invocation ID，并对身份、criteria/check 顺序、evidence references、Hash
和闭合语义做第二次校验。可注入 Verifier 返回结构正确且 Hash 有效的伪造 `PASSED`
仍必须失败。只有合法 `PASSED` 可以进入 `COMPLETED`。

终态 RuntimeOutcome 不仅绑定 VerificationResult 及其 Hash，还精确绑定每个 check 的
criterion/evidence-Step/evaluator 身份，以及每个 report result 的有序 evidence reference、
Invocation、Tool/Contract/Implementation/Arguments/Target 身份和 result Hash；缺失、重排或
重签名的引用都失败。

这些普通 SHA-256 Hash 提供确定性内容绑定和局部篡改检测，不提供密钥认证，也不能阻止
攻击者协调重写内容并重算全部 Hash。`RuntimeOutcome` 是 Runtime 返回的数据对象，不是
Authorization、恢复令牌或跨进程真实性证明；Phase 0–8 不支持从外部反序列化 Outcome 后
据此恢复或执行。在线结论的权威来源是同一 Runtime 调用中的私有输入与独立重算。Phase 9
在接受任何持久化事实用于恢复前，必须从各 owner 的 journal 重建 authority、重新验证
Plan/Policy/Tool Metadata/verification 语义，并为本地记录提供能够检测协调改写的完整性边界。
Incident Memory 中的 Outcome 投影始终是非权威历史事实。

边界失败使用两个不可混淆的稳定原因：Verifier 调用抛出异常或未完成评估为
`VERIFIER_FAILED`；Verifier 返回的类型、严格结构、绑定、顺序、Hash 或闭合语义非法，
或与 Runtime 独立重算结果不相等，为 `VERIFIER_RESULT_INVALID`。两者都 fail closed，但审计不得
把无效结果误记为 Verifier 运行异常。

Runtime 为每个产生的 VerificationResult 写入结构化 `verification_audit`。审计只包含
Task/Plan/Attempt/report/result Hash 绑定、status、稳定 reasons、effect/human 闭合、每个 check
的身份和状态，以及 evidence reference 的 Step/Invocation/result Hash；它不记录 raw data、raw
evidence、observed value 或未脱敏参数。

若 Contract-required `VERIFY` evidence 在交给 Verifier 前获取失败，Runtime 不伪造
VerificationResult；它直接 fail closed。只读 work 的 effect 是 `NONE`；任何已 pending
mutation 的 outcome 则闭合为 `UNKNOWN` 并要求人工介入。Verifier 失败、抛出异常或无法
得出结论时，Runtime 不得报告成功，也不得由 Verifier 临时派发 Tool、重试、请求恢复、
发起恢复或执行恢复。Verifier 只能在结构化结果中报告“需要人工考虑恢复”。

---

## Layer 10

Memory

负责：

- Incident 事实；
- 不可变任务与执行历史；
- 脱敏证据；
- 实际使用的 Skill ID、Version 和 Content Hash；
- 用户反馈和 Verification 结果。

Memory 是已终态 Incident 历史事实的权威来源。Skill Registry 可以维护可重建的
使用指标投影，但 Skill Bundle、Lifecycle Status、Review Record 和 active pointer
只由 Skill Registry 管理。Memory 不是授权或执行恢复事实的权威来源。

Memory 永远不参与权限判断、审批、Skill 激活或 Tool 执行。

Phase 9 持久化授权时必须使用 Approval-owned `AuthorizationJournal`，持久化执行
恢复事实时必须使用 Runtime/Executor-owned `ExecutionJournal`。它们可以与 Incident
Memory 物理共用 SQLite，但逻辑 schema、repository、owner 和依赖方向必须隔离。
Approval 只能从 AuthorizationJournal 重建 authority；Incident Memory 只接收脱敏
审计投影，永远不能向 Approval、Policy 或 Executor 提供授权事实。唯一 attempt key
只用于本地事件恢复和去重，不授权再次派发 Tool。

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
进入 `EXECUTING`，不等待人工。Phase 4 可以在当前进程签发 L2/L3 Plan
Approval，但当时 Task 仍停留在 `WAITING_FOR_APPROVAL`。Phase 5 让 Executor
线性化消费有效 Approval；只有消费成功后，Runtime 才进入 `EXECUTING`。任何风险
等级都不能绕过 Policy 或审计。L3 的即时确认在每个具体 Tool 调用前执行，不属于
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

Approval → AuthorizationJournal

Runtime / Executor → ExecutionJournal
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
