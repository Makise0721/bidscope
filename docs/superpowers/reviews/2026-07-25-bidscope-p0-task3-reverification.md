# BidScope P0 Task 3 独立复核记录

**复核日期**：2026-07-25  
**复核基线**：`864643c029481f4ba645a09722219045e1f4cf7e` (`feat/bidscope-final-17-20`)  
**工作树**：`C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20`  
**范围**：Task 3 持久化 checkpoint 运行时，以及它对 P0 后续任务的边界影响。  
**方法**：独立阅读交接、计划、规格和源码；独立运行测试收集、测试、Ruff、mypy、diff 检查；不修改实现代码。

## Findings

### [P1] 后台 heartbeat 失权后不会把运行行立即收敛为 `retryable`

- **位置**：`backend/src/bidscope/api/dependencies.py:504-519, 562-563, 614-620`
- **影响**：heartbeat 协程遇到数据库错误或 ownership loss 时只设置本地 `heartbeat_failure` 后退出。后续图执行发现失权时返回 `{"status": "retryable"}`，但该返回不会更新 `QueryRun.status`；`finally` 随后释放 advisory lock。数据库行可能继续保持 `running`，直到下一次启动的 stale recovery 才被回收。
- **证据/复现路径**：`heartbeat()` 在 `dependencies.py:516-519` 只记录异常；`_execute_run()` 在 `dependencies.py:562-563` 直接返回本地结果；正常状态落库只发生在 `dependencies.py:604-612`。现有 heartbeat 测试只验证 `updated_at` 递增，未验证 heartbeat 失败后的状态收敛。
- **为什么现有测试未发现**：`backend/tests/integration/test_run_recovery.py:344-390` 只覆盖成功刷新；`backend/tests/unit/api/test_run_cancellation.py:310-360` 只验证初始 heartbeat 失败时锁释放，不覆盖运行中 heartbeat 失败。
- **修复要求**：heartbeat 失权后应通过 execution-token 条件更新把行置为 `retryable` 并记录有界错误；若数据库暂时不可写，必须保留明确的恢复路径并验证 stale recovery 能接管，不能只依赖内存返回值。
- **所需回归测试**：模拟运行中 heartbeat 更新返回零行或抛出数据库错误，等待执行器结束，断言 `QueryRun.status`、`execution_token`、错误字段和 advisory-lock 后续可重试状态。

### [P1] 订阅 scheduler 仍绕过 Task 3 的 ownership-fenced RunService 路径

- **位置**：`backend/src/bidscope/subscriptions/service.py:485-493`；`backend/src/bidscope/graph/executor.py:443-475`
- **影响**：订阅执行仍直接创建 `QueryRun`，调用 `_dummy_graph()` 和低层 `execute()`，未经过 `RunService._start_run()`、execution token、run-scoped advisory lock、heartbeat 和最终状态同步。低层 `execute()` 对 tokenless `pending` 行仍允许执行。因此当前不能声称 API、scheduler、E2E 共用同一 durable execution path。
- **证据/复现路径**：`_run_locked()` 直接调用 `create_run(...); execute(_dummy_graph(), ...)`，调用未传 `execution_token`。交接文档已把替换 `_dummy_graph` 列为 Task 4 未完成目标，这个缺口是当前 P0 集成边界的真实反证。
- **为什么现有测试未发现**：Task 3 聚焦测试主要覆盖 API `RunService`，没有把订阅触发接到真实 `RunService`；当前订阅路径正是后续 Task 4 的待办实现。
- **修复要求**：Task 4 必须注入并调用同一 `RunService`/报告持久化路径；订阅触发产生幂等 scheduled-run key，并由受 token fence 保护的执行生命周期完成。
- **所需回归测试**：订阅触发后断言真实 `QueryRun` 的 token、checkpoint thread、状态和在线报告存在；模拟旧 worker/重复 trigger，断言不能追加事件或推进 seen cursor。

### [P2] Windows snapshot import 入口没有应用 SelectorEventLoop policy

- **位置**：`backend/src/bidscope/cli.py:73-76, 147-155`
- **影响**：`api serve`、`scheduler run`、`scheduler start` 调用了 `configure_windows_selector_event_loop_policy()`，但 `snapshots import` 直接执行 `asyncio.run(_run_import(bundle))`。该路径使用 async SQLAlchemy/asyncpg，在 Windows 默认 Proactor loop 下没有与代码库其他入口一致的兼容保证。
- **证据/复现路径**：`cli.py:154` 直接调用 `asyncio.run`；同文件只有 API/scheduler 命令在启动前调用 selector policy。当前单元测试只验证 policy 函数在不同平台的行为，没有真实执行 Windows snapshot import。
- **修复要求**：将 selector policy 应用到 snapshot import（以及任何使用 asyncpg 的 CLI 入口），或统一封装所有 async CLI 入口，并为入口增加 Windows-specific smoke test。
- **所需回归测试**：在 Windows policy 为 Proactor 的初始状态下调用 snapshot import 命令，确认实际数据库连接和导入完成；至少验证命令在 selector policy 被设置后才创建 async engine。

### [P2] 交接中的双审结论没有独立、可追溯的审查证据

- **位置**：`docs/superpowers/handoffs/2026-07-25-bidscope-p0-handoff.md:106-109`
- **影响**：文档声称“Spec review 通过”“Quality review 通过”“88 passed, 0 failures”，但没有审查人、审查时间、审查基线、审查范围、命令 stdout、pytest collection 或 CI artifact。无法从原交接记录证明结论是在最终 `0ba61a6` 修复之后得到的，也无法证明两个 review 是独立完成的。
- **证据/复现路径**：Git 历史中这些结论只出现在交接提交 `864643c`；没有独立 spec-review/quality-review 文档或测试日志。旧交接 `docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md:186-196` 的 `88 passed, 1 skipped, 1 warning` 属于旧任务 1-4 基线，不能替代当前 Task 3 证据。
- **修复要求**：保留审查基线 commit、范围、审查结果、实际命令及完整摘要；review 后续修复必须有 re-review 记录。交接状态应把“当前重新验证结果”和“历史声明”分开。
- **所需回归证据**：提交或归档当前 HEAD 对应的 `--collect-only`、pytest、Ruff、mypy 输出，记录环境变量、数据库/checkpoint setup 和 warnings；两个审查结论分别引用同一不可变基线。

## 已确认的强项

- `AsyncPostgresSaver` 由 FastAPI lifespan 持有；checkpoint `setup()` 只由显式 CLI 调用。
- API run claim、retry/confirm claim、事件追加、checkpoint mutation 和取消补偿均有 execution-token fence。
- stale `pending`/`running` 回收、running 行 advisory-lock probe、retry resume/force-fresh、`event_seq_offset` 对账均有实现和针对性测试。
- Task 3 聚焦测试包含真实 PostgreSQL checkpoint 跨实例 resume，以及 token、取消、事件历史和 reconciliation 回归。

## 独立验证

基于当前 HEAD `864643c`，使用交接文档中的 test-mode 数据库配置：

```text
BIDSCOPE_APP_MODE=test
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

- 聚焦 8 个 Task 3 文件：`88 tests collected`。
- 聚焦门禁：`88 passed, 2 warnings in 20.82s`。
- Task 3 相关源码和聚焦测试文件 Ruff：通过。
- 交接声明的 3 文件 strict mypy：通过。
- CI 同口径 `uv run mypy backend/src/bidscope`：`66 source files`，通过。
- CI 同口径 `uv run ruff check backend scripts`：失败，3 个已有 `I001` 导入排序错误，位置为 `backend/tests/integration/test_failure_recovery.py:16`、`backend/tests/unit/graph/test_confirmation.py:9`、`backend/tests/unit/graph/test_routing.py:14`。
- `git diff --check`：通过。
- 复核开始时工作树无业务代码修改；本复核新增本文件作为审查记录。

## 结论

Task 3 的**当前聚焦后端门禁已被重新验证为 88 passed**，实现和测试覆盖也足以说明其核心 API runtime 路径具备较强支撑。但这不等于原交接中的“双审通过”已经被独立证明：审查包和历史日志缺失，且 heartbeat 失权收敛与 Windows CLI 入口存在待修复风险。

因此当前状态应记录为：

> **Task 3：局部后端门禁通过，独立审查证据已补建；整体状态暂不视为无条件通过。P0 完整验收未完成。**

Task 4 可以进入实现，但在 Task 4 结束前不得把当前订阅路径描述为 durable report execution path，也不得把 P0 设计的完整 verification gate 标记为通过。

## 后续门禁

1. 修复或正式接受 P1 heartbeat 失权状态收敛，并补充运行中 heartbeat failure 回归测试。
2. Task 4 替换 `_dummy_graph`，接入真实 `RunService` 和报告持久化，再执行订阅/锁/游标事务门禁。
3. 修复 CI 的 `postgresql+psycopg://` checkpoint DSN，并清理 CI Ruff 的 3 个导入错误；不要用缩小检查范围替代修复。
4. 补充独立 review metadata、命令日志和当前 HEAD 绑定的 artifacts。
5. 最终 P0 仍需执行规格中要求的 Docker、MinIO、Web build、security、Playwright 和 clean-environment verification。
