# BidScope P0 修复交接文档

**日期**：2026-07-25  
**分支**：`feat/bidscope-final-17-20`  
**工作树**：`C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20`  
**状态**：Task 1–3 完成并通过双审，Task 4–8 待执行。

---

## 1. 项目概览

BidScope — 证据优先的招标智能 Agent。P0 阶段只做快照导入和已审核 fixture 数据，不抓取外部网站。

## 2. 已完成任务

### Task 1：API 与安全边界

**关键决策**：
- `development` / `production` 模式强制 `X-Admin-Token`；`demo` / `test` 免校验。
- `POST /api/runs` 支持 `Idempotency-Key`：同 key 同请求重放 → 200，同 key 不同请求 → 409，空 key → 422。
- 快照来源 URL 限制：HTTPS only，精确域名白名单，禁止 userinfo（含空 userinfo），禁止非默认端口。

**主要提交**：
```
69e31a0 fix: enforce P0 API and provenance boundaries
e2b5310 fix: require admin token outside demo modes
17671f1 fix: reject empty snapshot URL userinfo
6ab0ccd fix: serialize Task 1 run transitions
```

### Task 2：报告持久化与 DOCX

**关键决策**：
- 在线报告先于 DOCX 持久化（`Report` / `ReportItem` / `ReportClaim` / `ReportCitation`）。
- DOCX 写入失败不回滚在线报告；提供独立重试路由 `POST /api/reports/{run_id}/docx/retry`。
- 对象先写入再更新 `docx_object_key`；缺失对象时重新渲染写入相同确定性 key。
- 迁移使用 `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` 防止并发窗口。

**主要提交**：
```
468bef6 fix: persist evidence-backed reports before DOCX export
2d3bf6b fix: complete report delivery persistence
db48233 fix: lock report migration preflight
```

### Task 3：持久化 Checkpoint 运行时（含加固）

**架构要点**：

1. **生命周期**
   - FastAPI lifespan 持有 `AsyncPostgresSaver`、编译后的 `QueryWorkflow`、`RunService`。
   - `checkpoints setup` 仅 CLI 显式调用，不在 lifespan 里隐式执行。
   - 关闭时先 drain tracked tasks，再 dispose saver/engine。

2. **执行所有权**
   - 每次 run 分配 `execution_token`（`gen_random_uuid()`）。
   - 执行前获取 run-scoped PostgreSQL advisory lock（`pg_try_advisory_lock`）。
   - 锁立即 commit，不保持 idle-in-transaction。
   - 所有事件写入、状态更新、heartbeat、force-fresh 删除均校验 token。
   - 低层 `_assert_execution_token` / `_append_events` 拒绝 `execution_token=None` 调用 `running` / `retryable` 行。

3. **Heartbeat**
   - `config.py`：`run_heartbeat_seconds=30`，`stale_run_after_seconds=300`，Pydantic validator 保证前者小于后者。
   - 独立 heartbeat 协程按 `run_heartbeat_seconds` 刷新 `QueryRun.updated_at`，仅当 token 匹配。

4. **陈旧恢复**
   - 启动时将 `updated_at < stale_before` 的 `pending` / `running` 行翻为 `retryable`。
   - 对 `running` 行先探测 advisory lock，持锁者不回收。
   - `pending` 行不探测 lock（本无图所有者）。

5. **重试与取消**
   - retry 保持相同 `run_id` / `run_key` / `checkpoint_thread_id`。
   - 有未完成 checkpoint 时用 `Command(resume={"action": "retry"})` 恢复。
   - 终态 checkpoint 用 `force_fresh=True` 同线程重新执行。
   - 取消补偿绑定 execution token，只能修复自己持有的行。
   - Advisory unlock 失败时 invalidate/close 连接，不放回池。

6. **事件对账**
   - `RunState.event_seq_offset` 标记每次尝试的 relational 事件基数。
   - retry 保留旧 `run_events`，仅追加新事件于 `event_seq_offset` 之后。
   - 终态 checkpoint 返回前须证明 checkpoint 事件与 relational 事件精确对应。

7. **Windows 兼容**
   - CLI / API / scheduler 启动前应用 `_selector_event_loop`（psycopg async 需要 SelectorEventLoop）。

**测试覆盖**：
- `test_graph_persistence.py`
- `test_run_recovery.py`（含 heartbeat、stale lock probe、token fence、取消修复）
- `test_idempotency.py`（含 tokenless fence 回归）
- `test_runtime_recovery.py`（API 生命周期）
- `test_sse.py` / `test_runs.py`
- `test_run_cancellation.py` / `test_cli.py`

**主要提交**：
```
2745c18 fix: run API workflows with durable checkpoints
c22d920 fix: harden durable runtime lifecycle
9dccbea fix: finalize Task 3 cancellation lifecycle
885a00e fix: complete Task 3 recovery semantics
711dd86 fix: fence Task3 execution ownership
534f556 fix: fence Task3 run ownership
664d2ec fix: harden Task 3 execution recovery
0ba61a6 fix: reject tokenless executor writes on retryable cleared-token runs
```

**双审结论**：
- Spec review：通过。发现 `execution_token=None` 缺口并已在 `0ba61a6` 修复。
- Quality review：通过。变更范围精准，测试覆盖充分，ruff + mypy 通过。
- 验收门禁：`88 passed, 0 failures`。

---

## 3. 设计 / 计划文档

| 文件 | 用途 |
|------|------|
| `docs/superpowers/specs/2026-07-23-bidscope-p0-remediation-design.md` | P0 修复设计 |
| `docs/superpowers/plans/2026-07-23-bidscope-p0-remediation.md` | 分步实施计划 |
| `docs/superpowers/specs/2026-07-18-bidscope-design.md` | 原始设计 |
| `docs/superpowers/plans/2026-07-18-bidscope-implementation.md` | 原始实施计划 |

---

## 4. 待执行任务

### Task 4：订阅执行链

**目标**：替换 `_dummy_graph`，让订阅触发真实图执行和报告持久化。

**关键行为**：
- 订阅创建仅接受已完成且已确认的 scheduled run（`POST /api/subscriptions` body 为 `{"run_id": "…"}`）。
- 触发时用 idempotent scheduled run key 执行真实图，要求在线报告存在。
- 游标 / inbox 仅在报告提交后推进。
- 业务字段变更检测替代纯内容哈希。

**涉及文件**：
- `backend/src/bidscope/subscriptions/service.py`
- `backend/src/bidscope/subscriptions/scheduler.py`
- `backend/src/bidscope/api/routes/subscriptions.py`
- `backend/src/bidscope/api/dependencies.py`
- `backend/tests/integration/test_subscriptions.py`
- `backend/tests/integration/test_scheduler_lock.py`
- `backend/tests/integration/api/test_runs.py`

### Task 5：对象存储与容器

**目标**：Settings 驱动的 Local/S3 工厂，MinIO 初始化，Dockerfile 含迁移，标准 `bidscope api serve`。

**涉及文件**：
- `docker/`
- `Dockerfile` / `compose.yml`
- `backend/src/bidscope/delivery/objects.py`
- `backend/src/bidscope/config.py`

### Task 6：前端 Workbench

**目标**：运行状态、SSE 事件流、解析后的 intent、报告和证据溯源 UI。

**涉及文件**：`frontend/`

### Task 7：E2E 与 CI

**目标**：独立 Playwright setup → 迁移 → checkpoint → 导入 → 6 条非条件 E2E 流程 → CI 运行。

**涉及文件**：
- `frontend/e2e/`
- `.github/workflows/` 或 CI 配置

### Task 8：完整 P0 验证

**目标**：干净基础设施启动 → 迁移 → checkpoint → 导入 → 后端/前端/Docker/E2E 全部通过。

---

## 5. 环境变量与测试命令

### 本地 PostgreSQL 测试环境

```bash
export BIDSCOPE_APP_MODE=test
export BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test'
export BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test'
```

### 常用命令

```bash
# Checkpoint 表初始化（首次或迁移后必须执行）
uv run bidscope checkpoints setup

# 运行 Task 3 聚焦门禁
uv run pytest \
  backend/tests/integration/test_graph_persistence.py \
  backend/tests/integration/test_run_recovery.py \
  backend/tests/integration/test_idempotency.py \
  backend/tests/integration/api/test_runtime_recovery.py \
  backend/tests/integration/api/test_sse.py \
  backend/tests/integration/api/test_runs.py \
  backend/tests/unit/api/test_run_cancellation.py \
  backend/tests/unit/test_cli.py -q

# 静态检查
uv run ruff check backend/src/bidscope/ backend/tests/
uv run mypy --strict backend/src/bidscope/graph/executor.py backend/src/bidscope/api/dependencies.py backend/src/bidscope/config.py

# 启动 API
uv run bidscope api serve --host 0.0.0.0 --port 8000
```

---

## 6. 关键约束与注意事项

1. **不重写历史 migration**：新增 migration 追加到 `migrations/versions/`，不改已有文件。
2. **不丢弃未提交修改**：Task 3 加固期间保留了中断 agent 的 dirty 修改，通过审查后提交。
3. **快照 URL 安全**：`SnapshotManifest.source_urls` 在 Pydantic 验证前做 raw string 检查，拒绝 `https://@host` / `https://:@host` / 非默认端口。
4. **Task 3 token fence**：`executor.py` 低层函数不允许 `execution_token=None` 操作 `running` / `retryable` 行。新增功能若走 executor 直接路径须注意。
5. **`postgresql+psycopg` DSN**：checkpoint URL 和 sync migration URL 必须用 `+psycopg` 驱动，不能裸 `postgresql://`。
6. **mypy strict 范围**：当前仅对核心模块（executor / dependencies / config）开启 strict。

---

## 7. 风险区域

| 风险 | 等级 | 说明 |
|------|------|------|
| LangGraph 内部 checkpoint 写入 | 低 | `astream` 内的 checkpoint write 未被 token fence 覆盖；当前通过 advisory lock 隔离。若未来 LangGraph 升级改变 write 时序，需重新评估。 |
| 网络断开后 lock 残留 | 低 | PostgreSQL 在连接断开时自动释放 advisory lock；但旧 worker 恢复后可能继续写入直到 heartbeat 失败。已通过 token+lock 双重 fence 缓解。 |
| 前端 SSR/构建 | 未知 | Task 6 前未验证前端与后端 token/auth 集成。 |
| E2E Playwright 环境 | 未知 | 需确认 CI runner 有 Docker/PostgreSQL 能力。 |
