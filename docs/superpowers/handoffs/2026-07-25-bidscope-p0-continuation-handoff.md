# BidScope P0 继续推进交接文档

**日期**：2026-07-25  
**分支**：`feat/bidscope-final-17-20`  
**工作树**：`C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20`  
**基线 HEAD**：`864643c029481f4ba645a09722219045e1f4cf7e` (`docs: hand off BidScope P0 remediation status`)  
**当前状态**：实现已暂停；没有提交本轮修改；等待下一个模型继续复核和推进。

---

## 1. 本轮目标与停止原因

本轮接收了 `2026-07-25-bidscope-p0-handoff.md`，重点核验 Task 3 双审结论，并按用户要求开始派代理修复已确认的问题。

用户随后要求停止并交接给另一个模型，因此：

- 不再继续修改业务代码。
- 不提交、不推送、不回滚当前工作树改动。
- 保留所有当前未提交修改，供下一个模型先审阅后继续。
- 已新增本交接文档。
- 原有独立复核文档也保留：
  `docs/superpowers/reviews/2026-07-25-bidscope-p0-task3-reverification.md`

**重要**：当前工作树不是干净状态。下一个模型必须先读取 `git diff`，不要直接假设当前改动已经可合并。

---

## 2. 已阅读的设计与计划文档

本轮已经完整阅读：

1. `docs/superpowers/handoffs/2026-07-25-bidscope-p0-handoff.md`
2. `docs/superpowers/plans/2026-07-23-bidscope-p0-remediation.md`
3. `docs/superpowers/specs/2026-07-23-bidscope-p0-remediation-design.md`
4. `docs/superpowers/plans/2026-07-18-bidscope-implementation.md`
5. `docs/superpowers/specs/2026-07-18-bidscope-design.md`
6. `docs/superpowers/specs/2026-07-20-bidscope-scheduler-and-test-isolation-design.md`
7. `docs/superpowers/reviews/2026-07-19-tasks-5-8-review-handoff.md`

Task 4 的设计要求集中在：

- `docs/superpowers/plans/2026-07-23-bidscope-p0-remediation.md:505-623`
- `docs/superpowers/specs/2026-07-23-bidscope-p0-remediation-design.md:59-80`

---

## 3. 当前工作树状态

截至交接时，`git status --short --untracked-files=all` 显示：

```text
 M .github/workflows/ci.yml
 M backend/src/bidscope/api/dependencies.py
 M backend/tests/integration/test_failure_recovery.py
 M backend/tests/integration/test_run_recovery.py
 M backend/tests/unit/api/test_run_cancellation.py
 M backend/tests/unit/graph/test_confirmation.py
 M backend/tests/unit/graph/test_routing.py
?? docs/superpowers/reviews/2026-07-25-bidscope-p0-task3-reverification.md
?? docs/superpowers/handoffs/2026-07-25-bidscope-p0-continuation-handoff.md
```

上述状态中：

- 任务1的 4 个 tracked 文件改动先于任务2存在，应保留。
- 任务2改动集中在 `dependencies.py`、`test_run_recovery.py` 和 `test_run_cancellation.py`。
- 两个 `docs/superpowers/...` 文件是审查/交接文档，不是业务代码。
- 当前没有新 commit。最近的 commit 仍是 `864643c`。

当前累计 diff 约为：

```text
.github/workflows/ci.yml                         任务1
backend/src/bidscope/api/dependencies.py         任务2
backend/tests/integration/test_failure_recovery.py任务1
backend/tests/integration/test_run_recovery.py   任务2
backend/tests/unit/api/test_run_cancellation.py  任务2
backend/tests/unit/graph/test_confirmation.py    任务1
backend/tests/unit/graph/test_routing.py         任务1
```

不要把本轮所有 diff 一次性提交。建议先拆分、复核、测试，再按任务边界提交。

---

## 4. Task 1：Ruff 与 CI checkpoint DSN

### 4.1 已完成的改动

修改文件：

- `backend/tests/integration/test_failure_recovery.py`
- `backend/tests/unit/graph/test_confirmation.py`
- `backend/tests/unit/graph/test_routing.py`
- `.github/workflows/ci.yml`

内容：

1. 三个测试文件仅移动 `from graph_fakes import FakeReportPersistence` 的导入位置，修复 Ruff `I001`。
2. `.github/workflows/ci.yml:95`：

```yaml
BIDSCOPE_CHECKPOINT_DATABASE_URL: postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

原值是裸 `postgresql://`，与项目 psycopg v3 配置不一致。

没有缩小 CI Ruff 的检查范围；CI 仍是：

```yaml
uv run ruff check backend scripts
```

### 4.2 审查与验证状态

任务1已由两个只读代理完成：

- 规格审查：PASS。
- 代码质量审查：tracked diff PASS。

代理报告的验证结果：

```text
uv run ruff check --no-cache backend scripts
All checks passed!

uv run mypy backend/src/bidscope
Success: no issues found in 66 source files

git diff --check
exit 0
```

这些结果是在任务1改动完成后由实现代理执行的；下一个模型提交前仍应重新运行。

质量审查唯一指出的范围卫生问题是：审查文档为未跟踪文件，不属于任务1四文件 diff。该文档是本轮之前主动保留的审计材料，不要误删：

```text
docs/superpowers/reviews/2026-07-25-bidscope-p0-task3-reverification.md
```

### 4.3 建议下一步

任务1可以作为独立机械提交候选：

```text
chore: fix Ruff import ordering and checkpoint DSN
```

但用户没有要求本轮提交，所以先不要提交，除非下一个模型明确决定提交。

---

## 5. Task 2：heartbeat ownership loss 修复（当前待复核）

### 5.1 原始缺陷

原始实现位于 `backend/src/bidscope/api/dependencies.py` 的 `RunService._execute_run()`：

- 后台 heartbeat 在数据库更新失败或 `rowcount == 0` 时只设置本地 `heartbeat_failure`。
- 图执行发现 `RunOwnershipLostError` 时直接返回 `{"status": "retryable"}`。
- 该返回不一定更新 `QueryRun.status`。
- advisory lock 随后释放，数据库行可能继续保持 `running`，直到下一次启动的 stale recovery 才被回收。

因此旧 worker 的状态收敛和重试可见性不可靠。

### 5.2 当前已做的生产代码改动

文件：

```text
backend/src/bidscope/api/dependencies.py
```

当前实现增加了以下局部闭包/状态：

- `heartbeat_repair_attempted`
- `heartbeat_repair_task`
- `repair_ownership_loss(error)`
- `await_heartbeat_repair()`
- `ownership_loss_result(error)`

当前意图是：

1. heartbeat 失败后创建独立 repair task。
2. 使用 `asyncio.shield()`，避免 `_execute_run` 的 heartbeat cleanup 直接打断 repair。
3. 如果收到取消，使用 `_drain_task_preserving_cancellation()` 后再传播 `CancelledError`。
4. repair 调用：

```python
_update_status(
    run_id,
    "retryable",
    error=serializable_error,
    expected_status="running",
    execution_token=execution_token,
)
```

5. `_update_status` 返回 `False` 或抛普通异常时，不伪造状态，而是在返回结果中记录有界 details：

```json
{
  "repair_applied": false,
  "repair_error": "...",
  "recovery_path": "stale_run_recovery"
}
```

6. 初始 `ensure_active()` 的 ownership/OperationalError 也尝试进入同一 token-fenced repair 路径。
7. completed 或新 owner 的行不会被旧 token 覆盖。
8. 不修改 `backend/src/bidscope/graph/executor.py` 的 tokenless fence。

关键位置约为：

- `dependencies.py:469-539`：repair helper 和 ownership-loss result。
- `dependencies.py:573-613`：后台 heartbeat 失败和初始 heartbeat 失败。
- `dependencies.py:647-698`：graph 失权、正常结果和最终 cleanup。
- `_update_status` 的既有实现约在 `dependencies.py:885-957`，仍然是 status + execution token 的 CAS 边界。

### 5.3 当前测试改动

生产/测试修改文件：

- `backend/src/bidscope/api/dependencies.py`
- `backend/tests/integration/test_run_recovery.py`
- `backend/tests/unit/api/test_run_cancellation.py`

`test_run_recovery.py` 当前新增了较大一组测试，覆盖：

- heartbeat ownership loss 不覆盖 `completed`。
- heartbeat ownership loss 不覆盖新 owner token。
- SQLAlchemy/DBAPI 风格 `OperationalError`。
- 原 owner 仍在时 heartbeat `rowcount=0` 的 repair。
- repair 返回 `False` 时的可观测 stale recovery details。
- repair 自身抛 `OperationalError` 时的 stale recovery details。
- 初始 heartbeat OperationalError 不应继续执行 graph。
- advisory lock 可复用/释放。
- repair task 不被 heartbeat cleanup 取消打断。

`test_run_cancellation.py` 中现有初始 heartbeat 测试的断言已扩展为检查返回结果中的 repair details。

### 5.4 TDD 与验证状态

实现代理报告：

- 旧实现下新增边界测试 RED：
  - repair 失败时返回了错误的 `completed` 结果。
  - 初始 OperationalError 外溢。
  - 原 inline repair 被 heartbeat task cleanup 打断；此前两个参数均 RED。
- 修复后 heartbeat 边界测试：`5 passed`。
- 最新 Task 3 聚焦门禁：`95 passed, 2 warnings`。
- scoped Ruff：通过。
- strict mypy（三个核心文件）：通过。
- `git diff --check`：通过。

**重要验证声明**：以上最新 `95 passed` 是实现代理报告，主代理在停止前没有独立重新运行这轮最终代码。下一个模型必须把它视为“待验证”，不能直接当成最终门禁证据。

### 5.5 Task 2 当前审查结论

Task 2 曾经历两轮规格审查：

- 第一轮发现 repair task 被 heartbeat cleanup 打断的竞态，代理随后用独立 task + shield/drain 修复。
- 第二轮又发现 repair 自身失败和初始 heartbeat OperationalError 缺少明确路径/测试，代理随后补强。
- 补强后实现代理报告最终 GREEN，但补强后的代码尚未由主代理做最终独立规格审查和代码质量审查。

因此当前 Task 2 状态应写为：

> **实现代理报告完成，待主代理独立复跑、规格复审、质量复审。暂不提交。**

### 5.6 下一个模型必须重点检查

1. `repair_ownership_loss()` 对 `_update_status=False` 和普通异常的 details 是否边界清楚。
2. 初始 `ensure_active()` 异常是否正确创建 repair task，且没有无 token 写入。
3. `ownership_loss_result()` 在 repair task 为 `None`、已完成、异常和取消时的行为。
4. `finally` 中 heartbeat task cancel 是否可能产生未消费异常。
5. repair task 是否可能泄漏，或在父任务取消时正确 drain。
6. test doubles 是否真正只拦截 heartbeat update，而没有误拦截 `_update_status` 的其他 SQL。
7. 新增 471 行集成测试是否过度复杂、是否存在 flaky timing/竞态断言。
8. `test_run_cancellation.py` 改动是否只改变与新契约相关的断言。
9. 运行完整 Task 3 聚焦命令，当前预计 collection 从原来的 88 增加到 95。
10. 运行相关全包 mypy/Ruff，不只运行 scoped 命令。

---

## 6. Task 3：Windows snapshot import（尚未开始）

当前尚未修改：

- `backend/src/bidscope/cli.py`
- `backend/tests/unit/test_cli.py`

已确认缺陷：

- `api serve`、`scheduler run`、`scheduler start` 会调用：

```python
configure_windows_selector_event_loop_policy()
```

- `snapshots import` 在 `cli.py:147-155` 直接：

```python
record = asyncio.run(_run_import(bundle))
```

- 该入口使用 async SQLAlchemy/asyncpg，但没有在 `asyncio.run` 前设置 SelectorEventLoop policy。

建议实现：

1. 先写 command-path 测试，记录 `configure_windows_selector_event_loop_policy()` 和 `_run_import()` 的调用顺序。
2. 在 `asyncio.run(_run_import(bundle))` 前调用已有 helper。
3. 不把 policy 调用放进 coroutine 内。
4. 不引入 `executor.py` 的私有 `_run_async` helper。
5. 不改变同步的 `snapshots inspect`。

建议验证：

```bash
uv run pytest backend/tests/unit/test_cli.py -q
uv run pytest ...Task3 focused files... -q
uv run ruff check --no-cache backend scripts
uv run mypy backend/src/bidscope
```

---

## 7. Task 4：订阅真实执行链（尚未开始）

当前仍保留旧实现，不能绕过 Task 3 token fence 适配。

### 7.1 当前缺陷位置

`backend/src/bidscope/subscriptions/service.py:329-339`

- `SubscriptionService` 只注入 `session_factory`/失败开关，没有 RunService。

`service.py:485-495`

- 直接 `create_run()`。
- 调用 `_dummy_graph()`。
- 调用低层 tokenless `execute()`。

`service.py:665-676`

- `_dummy_graph()` 是 no-op graph，不产生报告。

`service.py:497-517`

- 旧流程直接 retrieve/diff/advance seen/commit，没有 report existence gate。

`service.py:578-625`

- material change 仍主要比较 content hash，没有调用已有 `detect_material_changes()`。

`backend/src/bidscope/api/routes/subscriptions.py:29-39,68-79`

- 仍接受任意 `intent`、`cron_expression`、`timezone`。
- 目标契约应改为：

```json
{"run_id": "..."}
```

`backend/src/bidscope/subscriptions/scheduler.py:164-211`

- scheduler 自建 engine/session。
- 创建裸 `SubscriptionService`。
- 没有 lifecycle-owned `AsyncPostgresSaver`、compiled graph、ReportPersistence、RunService。

### 7.2 Task 4 设计目标

必须满足：

1. 订阅只允许来自 completed、已确认、带 schedule 的 QueryRun。
2. 缺 run 返回 404；未完成/无 schedule 返回 409。
3. 保存 run 中规范化后的 intent，不允许 API body 覆盖 cron/timezone。
4. 每次 trigger 使用 subscription ID + UTC minute bucket 的确定性 scheduled run key。
5. 使用真实 RunService/graph/report persistence。
6. scheduled intent 在 graph 的 `confirm_intent` 会 interrupt；bridge 必须自动复用已确认意图并 approve/resume。
7. `RunService.execute_run()` 返回 retryable 时不能只依赖异常捕获，必须检查返回 status。
8. 只有在线报告持久化确认后，才计算 delta、写 inbox、更新 seen、推进 next-run。
9. material change 必须调用已有 `detect_material_changes()`，格式化变化不能触发事件。
10. 保留 PostgreSQL advisory lock、due filtering、success/skipped/failure next-run 规则和资源释放。
11. 不修改 `executor.py` tokenless fence。
12. 不新增 migration，除非先证明现有 schema 无法表达需求并重新确认范围。

### 7.3 Schema 注意事项

`Subscription` 没有 user_request/source run 专用列，见 `backend/src/bidscope/persistence/models.py:287-298`。

Task 4 计划没有 migration，因此要么：

- 在 `normalized_intent` 中使用明确命名空间的内部字段保存 source run/user request；
- 要么先报告需要 migration 的阻塞，不要擅自新增 schema。

`ReportItem.notice_version_id` 应作为报告当前版本集合的权威来源。

旧 `SubscriptionSeenItem` 只保存 source notice + previous content hash，构造 material diff 时需要加载旧 NoticeVersion、当前 ReportItem 对应 NoticeVersion 和 NoticeEvidence，形成完整 `NoticeView`。

### 7.4 Task 4 必须新增/改写测试

计划中要求但当前不存在的 fixture/helper 包括：

- `seed_completed_run`
- `FailingReportRunService`
- `_create_active_subscription`
- `_append_latest_version`
- `_create_subscription_with_seen_version`
- `count_seen_items`
- `count_inbox_events`

现有旧测试仍锁定 dummy 语义，需要重写而不是机械保绿：

- `backend/tests/integration/test_subscriptions.py:103-120`
- `backend/tests/integration/test_subscriptions.py:268-294`
- `backend/tests/integration/test_scheduler_lock.py:805-920`
- `backend/tests/integration/test_scheduler_lock.py:921-959`

Task 3 与 Task 4 共用：

```text
backend/tests/integration/api/test_runs.py
```

修改该文件时必须同时保留 Task 3 run/retry/API 断言。

E2E 之后也要同步修改：

```text
e2e/specs/subscription-batch.spec.ts
```

目前它仍发送任意 intent/cron/timezone，Task 4 完成后会失效；虽不在当前 Task 4 文件清单中，但属于 Task 7 后续工作。

---

## 8. 已知 CI/P0 门禁问题

Task 1 已修正：

- CI checkpoint DSN。
- 3 个 Ruff I001。

但以下仍未完成：

1. CI 没有独立 security job。
2. CI 没有 Playwright/E2E job。
3. CI web job 只有 unit test，没有 production build。
4. Docker job 只执行 `docker run --rm bidscope:ci --help`，没有 migration/checkpoint/API health smoke。
5. `backend/tests/integration/test_failure_recovery.py` 中仍有标记为 RED/gap 的测试面；完整 integration 不能仅凭 Task 3 focused 通过就宣称全绿。
6. `.env.example` 和 `docs/deployment.md` 仍有陈旧裸 `postgresql://`，其中 `docs/deployment.md` 属于 Task 5 文档范围，不要在 Task 4 混改。
7. P0 clean-environment、MinIO、Docker、Web build、security、Playwright 尚未完成。

---

## 9. 推荐下一个模型的执行顺序

### 第一步：只读确认当前状态

```bash
cd /c/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-final-17-20

git status --short --untracked-files=all
git diff --stat
git diff -- backend/src/bidscope/api/dependencies.py
git diff -- backend/tests/integration/test_run_recovery.py
git diff -- backend/tests/unit/api/test_run_cancellation.py
```

不要先运行 `git reset`、`git checkout` 或清理 untracked 文件。

### 第二步：复核 Task 2

先运行新增 heartbeat 测试，之后运行完整聚焦门禁：

```bash
export BIDSCOPE_APP_MODE=test
export BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test'
export BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test'

uv run pytest backend/tests/integration/test_run_recovery.py backend/tests/unit/api/test_run_cancellation.py -q

uv run pytest \
  backend/tests/integration/test_graph_persistence.py \
  backend/tests/integration/test_run_recovery.py \
  backend/tests/integration/test_idempotency.py \
  backend/tests/integration/api/test_runtime_recovery.py \
  backend/tests/integration/api/test_sse.py \
  backend/tests/integration/api/test_runs.py \
  backend/tests/unit/api/test_run_cancellation.py \
  backend/tests/unit/test_cli.py -q
```

当前实现代理报告预期为 `95 passed, 2 warnings`，但必须由下一个模型独立确认。

然后运行：

```bash
uv run ruff check --no-cache backend scripts
uv run mypy backend/src/bidscope
uv run mypy --strict backend/src/bidscope/graph/executor.py backend/src/bidscope/api/dependencies.py backend/src/bidscope/config.py
uv run git diff --check
```

如果 Task 2 规格/质量审查仍发现问题，先修复并重新测试，不要进入 Task 3/4。

### 第三步：Task 3 Windows CLI

按第6节 TDD 实现并审查。

### 第四步：Task 4 订阅链

按第7节设计实现；不要用测试 mock 掩盖 `_dummy_graph`，不要放宽 token fence。

### 第五步：最终分层验证

至少分开记录：

- Task 3 聚焦门禁。
- Task 4 订阅/scheduler-lock/API 门禁。
- unit/contract。
- 全 integration（包括现有 RED/gap 状态）。
- Ruff、mypy、diff。
- 尚未完成的 Docker/Web/security/E2E/P0 clean gate。

---

## 10. 提交建议

本轮没有提交任何 commit。建议未来按以下边界提交：

1. `chore: fix Ruff import ordering and checkpoint DSN`
   - 任务1四个文件。
2. `fix: fence heartbeat ownership loss recovery`
   - `dependencies.py`、heartbeat/cancellation recovery tests。
3. `fix: make snapshot import Windows-safe`
   - `cli.py`、CLI tests。
4. `fix: deliver subscriptions through persisted reports`
   - Task 4 service/scheduler/routes/dependencies 和相关测试。

不要把未跟踪 review/handoff 文档和业务代码混入行为提交，除非明确需要归档审计材料。

---

## 11. 当前最重要的结论

- Task 1 的四文件改动已完成，tracked diff 规格/质量审查通过。
- Task 2 的最新实现代理报告为 `95 passed`，但主代理没有独立复跑，且最新规格/质量复审尚未通过，因此 Task 2 不能标记为完成。
- Task 3 尚未开始。
- Task 4 尚未开始；订阅仍使用 `_dummy_graph` 和 tokenless low-level execute，不能宣称 durable report path。
- 原交接文档中的“双审通过”不能替代当前 HEAD 的独立审查证据。
- 绝不能把当前工作树标记为 P0 完成。
