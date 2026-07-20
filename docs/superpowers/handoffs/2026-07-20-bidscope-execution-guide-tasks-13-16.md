# BidScope 超详细实施执行手册（任务 13-16）

**用途：** 本文供能力较弱、上下文较短或容易越界的编码模型接管 BidScope 任务 13-16 的实现。  
**项目根仓库：** `C:\Users\29913\zcode_workspace\bidscope`  
**实现 worktree：** `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0`  
**实现分支：** `feat/bidscope-p0`  
**当前代码基线：** `3ff9fca fix: address batch-3 review findings (H1, M1, M2, M3, M4, L1-L3)`（含任务 9-12 全部实施、第三批审查修复、Alembic `run_events.timestamp` migration `a1b2c3d4e5f6`）  
**已通过审查：** 任务 1-12、三批审查、第三批审查修复 `3ff9fca`；下一批只执行任务 13-16  
**设计规格：** `docs/superpowers/specs/2026-07-18-bidscope-design.md`  
**详细计划：** `docs/superpowers/plans/2026-07-18-bidscope-implementation.md`  
**前一批次交接文档：** `docs/superpowers/handoffs/2026-07-19-bidscope-execution-guide-tasks-9-12.md`（含任务 9-12 所有细节、通用协议、Windows 排障、门禁格式）  
**本文版本：** 2026-07-20

---

## 0. 可以直接交给接管模型的启动提示词

将下面整段原样交给负责任务 13-16 的新模型。第一次只让它执行任务 13；任务 13 提交并自检后再继续任务 14。

```text
你正在接管 BidScope 的第四个实现批次，只执行任务 13-16。

1. 只在这个目录工作：
   C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
2. 当前分支必须是 feat/bidscope-p0；提交 3ff9fca 必须是 HEAD 的祖先。
3. 3ff9fca 之后在接管前只允许存在本交接文档的纯文档提交。若出现其他代码提交，停止并报告。
4. 任务 1-12 已通过多轮审查。禁止改写、回退或顺手重构任务 1-12。任务 9-12 含第三批审查修订件，均为已冻结代码。
5. 先阅读：
   - docs/superpowers/specs/2026-07-18-bidscope-design.md
   - docs/superpowers/plans/2026-07-18-bidscope-implementation.md
   - docs/superpowers/handoffs/2026-07-20-bidscope-execution-guide-tasks-13-16.md
6. 当前先执行任务 13。任务 13 独立提交后才执行任务 14；依次完成到任务 16。
7. 严格执行 TDD：先写测试并取得有效 RED，再写最小生产代码，最后取得 GREEN 和完整门禁。
8. 路径错误、依赖缺失、语法错误、数据库未启动都不是有效 RED。有效 RED 必须来自目标模块或行为尚未实现。
9. P0 是 snapshot-only。禁止访问、抓取或探测 CCGP、GGZY 或其他招投标网站。
10. 数据真实性边界不可改变：
    - raw_response：真实原始响应
    - curated_public_excerpt：人工核验公开摘录
    - synthetic_demo：明确合成数据
11. 合成数据只能使用 source=synthetic_demo、example.invalid URL、demo-* ID。
12. CCGP 只允许 www.ccgp.gov.cn/search.ccgp.gov.cn；GGZY 只允许 www.ggzy.gov.cn。
13. 每个任务必须独立提交，提交消息使用实施计划中的固定文本。
14. 每个任务结束前运行目标测试、Ruff、mypy、git diff --cached --check，并确认工作树干净。
15. 遇到规格冲突或必须访问外部网站才能继续时，停止并报告，不要猜测或绕过限制。
16. 禁止 git reset --hard、禁止改写 main、禁止 push、禁止发布外部服务。
17. 开始任务 13 前，先针对锁定版本核验依赖可用性：

uv run python -c "from docx import Document; print('python-docx OK')"
uv run python -c "from bidscope.domain.reports import Report, ReportItem, ReportClaim, ReportCitation; print('Report contracts OK')"
uv run python -c "from bidscope.delivery.objects import ObjectStore, LocalObjectStore; print('ObjectStore OK')"

第一条操作：核验 branch=feat/bidscope-p0、3ff9fca 是 HEAD 的祖先、3ff9fca..HEAD 只有交接文档、工作树干净，然后只开始任务 13 的 RED。
```

---

## 1. 项目目标与不能改变的决策

（继承自前一批次交接文档第 1 节，本节概要列出与任务 13-16 直接相关的条款。完整条款见 `docs/superpowers/handoffs/2026-07-19-bidscope-execution-guide-tasks-9-12.md` 第 1 节。）

BidScope 是一个证据优先的招投标情报 Agent。任务 13-16 完成其 DOCX 交付、API/SSE 暴露、订阅调度与前端工作台。

### 1.1 任务 13-16 各自目标

| 任务 | 目标 |
|---|---|
| 13 | DOCX 报告渲染与幂等存储（`delivery/docx.py`） |
| 14 | 运行/报告/SSE API 暴露（`api/*`，修改 `main.py`） |
| 15 | 订阅、PostgreSQL 顾问锁、收件箱事件（`subscriptions/*`） |
| 16 | React 工作台与主流程（`web/*`） |

### 1.2 不可改变的 P0 决策（节选与本批次相关者）

- P0 是 **snapshot-only**，不是实时爬虫。
- 在线报告与 DOCX 使用同一个类型化 `Report` 模型（`domain/reports.py`，已冻结）。
- DOCX 只从类型化 `Report` 渲染，**绝不重新 prompt 模型**。
- 所有当前时间通过 `Clock` 注入；业务代码不得直接 `datetime.now()`。
- 合成数据只能使用 `source=synthetic_demo`、`example.invalid` URL、`demo-*` ID。
- 防 prompt injection：导入文本只能放在 `UNTRUSTED_SOURCE_DATA` 区域。

---

## 2. 当前现场：任务 13-16 接管前必须知道

### 2.1 Git 结构与审查基线

```text
C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
branch: feat/bidscope-p0
code baseline: 3ff9fca fix: address batch-3 review findings (H1, M1, M2, M3, M4, L1-L3)
HEAD: code baseline + this handoff's documentation-only commit
status: clean
```

冻结边界（任务 1-12 全部冻结）。本批次会在以下**已允许位置**新增代码，其余位置一律不得改写：

- 任务 13 → 新建 `backend/src/bidscope/delivery/docx.py`、`backend/tests/unit/delivery/test_docx.py`、`backend/tests/integration/test_report_delivery.py`
- 任务 14 → 新建 `backend/src/bidscope/api/dependencies.py`、`api/routes/runs.py`、`api/routes/reports.py`、`api/routes/test_controls.py`、`backend/tests/integration/api/test_runs.py`、`backend/tests/integration/api/test_sse.py`；**可改**已有 `main.py`
- 任务 15 → 新建 `backend/src/bidscope/subscriptions/service.py`、`subscriptions/scheduler.py`、`api/routes/subscriptions.py`、`backend/tests/integration/test_subscriptions.py`、`backend/tests/integration/test_scheduler_lock.py`；**可改**已有 `persistence/repositories.py`
- 任务 16 → 新建 `package.json`、`package-lock.json`、`web/package.json`、`web/vite.config.ts`、`web/src/app/App.tsx`、`web/src/api/client.ts`、`web/src/features/workbench/*`、`web/src/styles/*`、`web/tests/workbench.test.tsx`

### 2.2 当前依赖、数据库与验证状态

关键锁定版本：

- python-docx `>=1.1.2,<2`（任务 13 必用）
- FastAPI `>=0.115,<1`、sse-starlette `>=2.1,<3`（任务 14 SSE 必用）
- React 19、TypeScript、Vite、TanStack Query、Vitest、Playwright（任务 16）
- APScheduler `>=3.10,<4`（任务 15 订阅调度必用）

两个数据库 URL 必须指向同一 host、port 和 test/e2e 数据库；仅 driver 可不同：

```text
BIDSCOPE_APP_MODE=test
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

任务 12 放行时的独立门禁（任务 13-16 必须保持绿色）：

```text
pytest:        191 passed, 1 skipped, 1 warning
Ruff:          passed
mypy:          45 source files passed
Alembic check: passed
git diff --check: passed
```

当前 Alembic 头：`a1b2c3d4e5f6` → 实为 `run_events.timestamp` migration。

### 2.3 已完成的领域契约（任务 13-16 直接依赖）

新建模块前必须熟知以下冻结契约：

| 契约 | 文件 | 任务 13-16 用途 |
|---|---|---|
| `Report`, `ReportItem`, `ReportClaim`, `ReportCitation` | `domain/reports.py` | 任务 13 DOCX 渲染输入；任务 14 报告 API |
| `ObjectStore`, `LocalObjectStore` | `delivery/objects.py` | 任务 13 DOCX 字节存储 |
| `QueryRun`, `RunEvent` | `persistence/models.py` | 任务 14 运行/事件 API；任务 15 订阅运行 |
| `SnapshotRepository` | `persistence/repositories.py` | 任务 15 可扩展示范订阅仓库方法 |
| `create_run`, `execute`, `mark_stale_runs_retryable` | `graph/executor.py` | 任务 14 API 调用执行器；任务 15 订阅触发 |
| `build_graph`, `GraphDeps`, `QueryWorkflow` | `graph/builder.py` | 任务 14/15 复用图编译 |
| `RunState`（含 `evidence_by_id`, `report`, `node_events`） | `graph/state.py` | 任务 14 SSE 事件源 |

禁止任何改写、回退或顺手重构上述契约。如需接口扩展，只能作当前任务不可避免的加法，并在审查包中逐项解释。

### 2.4 第三批审查修复摘要（3ff9fca，本批次必须保持）

| 编号 | 修复 | 位置 |
|---|---|---|
| H1 | `DeepSeekDuplicateModel.classify()` 实现为真实 LLM 端口（ChatOpenAI + with_structured_output + UNTRUSTED_SOURCE_DATA 包装 + 真实 ModelUsage） | `llm/deepseek.py` |
| M1 | 移除未使用 `Annotated` 导入 | `llm/types.py` |
| M2 | 新增 `SearchIntent.model_json_schema()` 嵌套类型测试 | `test_contracts.py` |
| M3 | 确认测试使用非空 `load_notice_views` fixture | `test_confirmation.py` |
| M4 | `RunEvent.timestamp` 列 + Alembic migration + 持久化 Clock 注入时间 | `models.py`, `executor.py` |
| L1-L3 | 澄清注释 + 尾随空白清理 | `nodes.py`, `deepseek.py`, handoff doc |

任务 13-16 不得回退上述行为。

---

## 3. 任务 13-16 的精确推进地图

本节补充能力较弱模型最容易遗漏的进入条件、关键断言、退出条件和禁区。**每一文件与代码仍以详细计划为准。**

---

### 任务 13：DOCX 报告渲染与幂等存储

**进入条件：** 任务 12 已独立提交，工作树干净，`python-docx` 可用。  
**目标:** 从类型化 `Report` 渲染 DOCX，幂等存储到 `ObjectStore`。

允许创建：

```text
backend/src/bidscope/delivery/docx.py
backend/tests/unit/delivery/test_docx.py
backend/tests/integration/test_report_delivery.py
```

#### 任务 13 RED

```bash
uv run pytest backend/tests/unit/delivery/test_docx.py backend/tests/integration/test_report_delivery.py -q
```

#### 任务 13 渲染与存储要求

- **只从类型化 `Report` 渲染**，绝不重新 prompt 模型（设计 §5.2）。
- 使用确定性标题顺序、条件与机会表格、编号证据引用、来源/版本附录。
- 文件名消毒为 `bidscope-{report_id}.docx`。
- 字节通过 `ObjectStore`（`LocalObjectStore`）存储。
- 幂等键由 report ID + renderer version 派生 — 同一 report 导出两次只得一条逻辑导出记录、一个对象键。
- 渲染的 DOCX 用 python-docx 重新打开后，必须包含：查询条件、每个 item 标题、未知字段标记、来源 URL、证据标签、完整性警告。

#### 任务 13 完整验证与提交

```bash
uv run pytest backend/tests/unit/delivery backend/tests/integration/test_report_delivery.py -q
uv run ruff check backend/src/bidscope/delivery backend/tests/unit/delivery backend/tests/integration/test_report_delivery.py
uv run mypy backend/src/bidscope/delivery
git diff --cached --check
```

固定提交：

```text
feat: render idempotent evidence reports
```

---

### 任务 14：运行/报告/SSE API 暴露

**进入条件：** 任务 13 已提交，工作树干净。  
**目标:** 暴露运行、报告、SSE 端点；`main.py` 接入 lifespan。

允许创建：

```text
backend/src/bidscope/api/dependencies.py
backend/src/bidscope/api/routes/runs.py
backend/src/bidscope/api/routes/reports.py
backend/src/bidscope/api/routes/test_controls.py
backend/tests/integration/api/test_runs.py
backend/tests/integration/api/test_sse.py
```

可改：`backend/src/bidscope/main.py`。

#### 任务 14 RED

```bash
uv run pytest backend/tests/integration/api/test_runs.py backend/tests/integration/api/test_sse.py -q
```

#### 任务 14 端点与行为边界

测试 `POST /api/runs`、`GET /api/runs/{id}`、`POST /api/runs/{id}/confirm`、`POST /api/runs/{id}/retry`、`GET /api/runs/{id}/events`、`GET /api/reports/{id}`、`GET /api/reports/{id}/docx`。

- `confirm` 仅当 run 处于 `awaiting_confirmation` 时成功，否则返回 HTTP 409。
- `retry` 仅当 run 处于 `retryable` 时成功，否则返回 HTTP 409。
- 创建 run 时先存 `pending` 状态，再调度执行器任务（调用 `graph/executor.py` 的 `execute`）。
- SSE 读取有序数据库事件（`RunEvent`，按 `seq`），发射 `id`、`event`、JSON `data`；每 15 秒心跳；尊重 `Last-Event-ID`；终端事件后结束。
- 公开 demo 模式始终注入 fake model 与 hash embeddings；real model 模式需服务端配置 + `X-Admin-Token`。
- `/api/test-controls/*` 仅当 `app_mode=test` 时注册；需独立测试 token；暴露有界控制（一次性节点失败、Batch 2 导入）。测试必须断言这些路由在 demo、development、production 模式下返回 404。

#### 任务 14 完整验证与提交

```bash
uv run pytest backend/tests/integration/api -q
uv run ruff check backend/src/bidscope/api backend/src/bidscope/main.py backend/tests/integration/api
uv run mypy backend/src/bidscope/api
git diff --cached --check
```

固定提交：

```text
feat: expose Agent runs and reports over API
```

---

### 任务 15：订阅、顾问锁与收件箱事件

**进入条件：** 任务 14 已提交，工作树干净，Postgres 已启动。  
**目标:** 增量订阅调度、PostgreSQL 顾问锁、收件箱事件。

允许创建：

```text
backend/src/bidscope/subscriptions/service.py
backend/src/bidscope/subscriptions/scheduler.py
backend/src/bidscope/api/routes/subscriptions.py
backend/tests/integration/test_subscriptions.py
backend/tests/integration/test_scheduler_lock.py
```

可改：`backend/src/bidscope/persistence/repositories.py`（最小必要扩展）。

#### 任务 15 RED

```bash
BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
```

#### 任务 15 订阅与锁要求

- 显式确认的 intent 创建、下次运行时间。
- 第二快照批次产生 `new_notice` 与 `material_change` 收件箱事件；不变项目不产生事件。
- 连续三次失败暂停订阅。
- 双 worker 锁测试：同 subscription/time bucket 两次并发触发，只得一次 query run、一组收件箱事件。
- APScheduler 一分钟 tick、数据库存储 schedule、IANA 时区、PostgreSQL 顾问锁由 subscription UUID + 调度时间派生。
- `subscription_seen_items` 仅在 report commit 后推进。
- 暴露 list/create/pause/resume 端点与 `uv run bidscope scheduler` CLI。

#### 任务 15 完整验证与提交

```bash
uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
uv run ruff check backend/src/bidscope/subscriptions backend/src/bidscope/api/routes/subscriptions.py backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py
uv run mypy backend/src/bidscope/subscriptions
git diff --cached --check
```

固定提交：

```text
feat: schedule incremental tender subscriptions
```

---

### 任务 16：React 工作台与主流程

**进入条件：** 任务 15 已提交，工作树干净。  
**目标:** React 工作台与主流程（`web/*`）。

允许创建：

```text
package.json
package-lock.json
web/package.json
web/vite.config.ts
web/src/app/App.tsx
web/src/api/client.ts
web/src/features/workbench/*
web/src/styles/*
web/tests/workbench.test.tsx
```

#### 任务 16 RED

```text
npm install
npm run test:web -- --run web/tests/workbench.test.tsx
```

#### 任务 16 工作台要求

- React、TypeScript、React Router、TanStack Query、lucide-react、Vest、Testing Library、MSW。
- 根脚本暴露 `npm run dev:web`、`npm run test:web`、`npm run build:web`、`npm run test:e2e`。
- 主工作台测试：mock create-run、SSE 事件、intent 确认、报告响应；断言用户可输入代表查询、审查可编辑芯片、批准、观察有序节点事件、打开证据、查看快照来源、调用 DOCX 下载。
- 确认的三列桌面布局 + 1050px 以下证据/痕迹抽屉。
- 固定图标按钮尺寸、lucide 工具提示、显式 loading/empty/partial/error/awaiting-confirmation 状态、可访问标签、无嵌套卡片。
- 报告明显区分 `raw_response`、`curated_public_excerpt`、`synthetic_demo`；合成记录使用持久“合成演示数据”标签，URL 显示为纯文本非可点击链接，加检索时间、hash 前缀、新鲜度。

#### 任务 16 完整验证与提交

```text
npm run test:web -- --run
npm run build:web
npm run lint --if-present

git add package.json package-lock.json web
git commit -m "feat: add BidScope evidence workbench"
```

完成任务 16 后**立即暂停**。不要执行任务 17。

---

## 4. 前批次通用协议与门禁格式（直接沿用）

以下沿用 `docs/superpowers/handoffs/2026-07-19-bidscope-execution-guide-tasks-9-12.md` 第 4–13 节，**全部适用**于任务 13-16：

- **第 4 节** 通用执行协议（RED → GREEN → REFACTOR → COMMIT）
- **第 5 节** 任务 17-20 推进地图（任务 13-16 同批次但不在本手册范围，不要提前实现）
- **第 6 节** Windows、Git Bash、uv、Docker 常见故障
- **第 7 节** 每次任务完成后的强制自审（规格符合性、代码质量、Git 审核）
- **第 8 节** 每轮必须返回的格式（Task N Result 模板）
- **第 9 节** 阻塞处理
- **第 10 节** 分支、提交与最终集成
- **第 11** 节 任务清单（任务 13-16 当前批次）
- **第 12 节** 接管后第一条实际操作
- **第 13 节** 审查包模板（**第四批审查包模板**用本节）

关键纪律重申：

- 第 7.1 节新增一条：**禁止提前实现任务 17**（evaluation），禁止添加任务 17-20 的调度、前端代码。
- 第 8 节格式、第 13 节审查包模板适用于本批次。

---

## 5. 对本批次（任务 13-16）特别容易遗漏的提醒

### 5.1 TDD RED 必须是有效 RED

任务 13-16 很多模块依赖 FastAPI/React/APScheduler 类型。提醒：

- `ImportError: No module named 'bidscope.delivery.docx'` / `bidscope.api` / `bidscope.subscriptions` 是**有效 RED**（目标模块不存在）。
- `ModuleNotFoundError: docx` / `sse_starlette` 是**无效 RED**（依赖缺失）。如果锁定依赖 import 失败，停止核验第 0 节预检命令，不要继续。
- 语法错误、路径拼写错误、拼错 import 都是无效 RED。

### 5.2 不得改写冻结契约，即使“顺手”

任务 13-16 大量调用 `domain/reports.py`、`delivery/objects.py`、`persistence/models.py`、`graph/executor.py` 等已冻结契约。常见诱惑：

- 为了让 DOCX 渲染更简单而给 `Report` 加字段 → **禁止**。
- 为了让 API 更省事而把 `RunEvent` 改成非 frozen → **禁止**。
- 为了让 `confirm_intent` 跳过 interrupt → **禁止**。

如需接口扩展，只能作当前任务不可避免的加法，并在审查包中逐项解释。

### 5.3 数据真实性边界（再提醒）

- 任务 13 的 DOCX 测试可继续用 `data/demo/batch-1`、`example.invalid`、`demo-` IDs、`source=synthetic_demo`。
- 任务 14 的 SSE 测试使用真实 Postgres（`bidscope_test`），**不得用生产库**。
- 任务 15 的订阅/锁测试使用 `bidscope_test` 数据库，**不得用生产库**。

### 5.4 线程安全与跨进程

LangGraph `thread_id = str(run_id)` 是必选约定。任务 14 创建 run 时必须用 `create_run` 返回的 `run_id` 作为 `thread_id`。

### 5.5 DOCX 幂等性

任务 13 的幂等键必须由 report ID + renderer version 派生。同一 report 导出两次只得一条逻辑导出记录、一个对象键。测试必须断言这一点。

---

## 6. 第四批审查包模板（任务 13-16 完成后使用）

完成任务 16 后立即暂停。把下面模板填完整后交给审查模型；不得进入任务 17。

```markdown
# BidScope 第四批审查请求

## Batch
Tasks: 13-16
Base SHA: 3ff9fca
Head SHA: <任务16提交SHA>
Branch: feat/bidscope-p0

## Commits
- Task 13: <SHA> feat: render idempotent evidence reports
- Task 14: <SHA> expose Agent runs and reports over API
- Task 15: <SHA> schedule incremental tender subscriptions
- Task 16: <SHA> add BidScope evidence workbench

## Task 13 RED/GREEN
- RED command:
- GREEN command:
- DOCX parity evidence (query conditions / item titles / unknown-field marker / source URL / evidence label / completeness warning):
- Idempotency evidence (one export record + one object key on second export):
- Network access performed: no

## Task 14 RED/GREEN
- RED commands:
- GREEN commands:
- Confirm 409 evidence (non-awaiting-confirmation):
- Retry 409 evidence (non-retryable):
- SSE heartbeat / Last-Event-ID / terminal-event evidence:
- Test-controls 404 in demo/development/production evidence:

## Task 15 RED/GREEN
- RED commands:
- GREEN commands:
- Subscription lifecycle evidence:
- Two-worker lock evidence (exactly one run + one inbox set):
- Failure-pause evidence (three consecutive failures):

## Task 16 RED/GREEN
- RED command:
- GREEN command:
- Workbench main-flow evidence:
- Synthetic-data labeling evidence (persistent label + non-clickable URL):
- Responsive layout evidence (three-column + drawer <1050px):

## Batch Verification
- Full pytest command/result:
- Ruff command/result:
- mypy command/result:
- Alembic check result:
- npm test:web / build:web result:
- git diff --check 3ff9fca..HEAD:
- git status:

## Data Truthfulness
- Synthetic URLs use example.invalid: yes
- Demo IDs use demo-*: yes
- DOCX tests use synthetic/demo data only: yes
- LLM/network calls in tasks 13-16: none

## Files Changed Outside Planned Scope
- None / list every file and reason

## Deviations
- None / explain each deviation and why it was necessary

## Known Risks
- None / list

## Stop Confirmation
- Task 17 files created: no
- Worktree clean: yes/no
```

---

*本文与 `docs/superpowers/handoffs/2026-07-19-bidscope-execution-guide-tasks-9-12.md`（任务 9-12）配套使用；前者通用条款、Windows 排障、通用审查包模板沿用，本文补充任务 13-16 进入条件、契约依赖、推进地图、第四批审查包模板。*
