# BidScope 超详细实施执行手册（任务 9-12）

**用途：** 本文供能力较弱、上下文较短或容易越界的编码模型接管 BidScope 任务 9-12 的实现。  
**项目根仓库：** `C:\Users\29913\zcode_workspace\bidscope`  
**实现 worktree：** `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0`  
**实现分支：** `feat/bidscope-p0`  
**当前代码基线：** `c4442a8 fix: address batch-2 review findings (M1-M6, L1-L4)`（含任务 5-8 全部实施、第二批审查修复、Alembic 检索索引 migration）  
**已通过审查：** 任务 1-8、三批审查、第二批审查修复 `c4442a8`；下一批只执行任务 9-12  
**设计规格：** `docs/superpowers/specs/2026-07-18-bidscope-design.md`  
**详细计划：** `docs/superpowers/plans/2026-07-18-bidscope-implementation.md`  
**前一批次交接文档：** `docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md`（含任务 1-8 所有细节、通用协议、Windows 排障、门禁格式）  
**本文版本：** 2026-07-19

---

## 0. 可以直接交给接管模型的启动提示词

将下面整段原样交给负责任务 9-12 的新模型。第一次只让它执行任务 9；任务 9 提交并自检后再继续任务 10。

```text
你正在接管 BidScope 的第三个实现批次，只执行任务 9-12。

1. 只在这个目录工作：
   C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
2. 当前分支必须是 feat/bidscope-p0；提交 c4442a8 必须是 HEAD 的祖先。
3. c4442a8 之后在接管前只允许存在本交接文档的纯文档提交。若出现其他代码提交，停止并报告。
4. 任务 1-8 已通过多轮审查。禁止改写、回退或顺手重构任务 1-8。任务 5-8 含第二批审查修订件，均为已冻结代码。
5. 先阅读：
   - docs/superpowers/specs/2026-07-18-bidscope-design.md
   - docs/superpowers/plans/2026-07-18-bidscope-implementation.md
   - docs/superpowers/handoffs/2026-07-19-bidscope-execution-guide-tasks-9-12.md
6. 当前先执行任务 9。任务 9 独立提交后才执行任务 10；依次完成到任务 12。
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
17. 开始任务 9 前，先针对锁定版本核验依赖可用性：

uv run python -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; print('AsyncPostgresSaver OK')"
uv run python -c "from langgraph.types import Command, interrupt; print('Command/interrupt OK')"
uv run python -c "from langgraph.checkpoint.memory import InMemorySaver; print('InMemorySaver OK')"
uv run python -c "from langchain_openai import ChatOpenAI; print('ChatOpenAI OK')"
uv run python -c "from openai import AsyncOpenAI; print('AsyncOpenAI OK')"

第一条操作：核验 branch=feat/bidscope-p0、c4442a8 是 HEAD 祖先、c4442a8..HEAD 只有交接文档、工作树干净，然后只开始任务 9 的 RED。
```

---

## 1. 项目目标与不能改变的决策

（继承自前一批次交接文档第 1 节，本节概要列出与任务 9-12 直接相关的条款。完整条款见 `docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md` 第 1 节。）

BidScope 是一个证据优先的招投标情报 Agent。任务 9-12 完成其核心 Agent“大脑”与可恢复执行层。

### 1.1 任务 9-12 各自目标

| 任务 | 目标 |
|---|---|
| 9 | Fake 与 DeepSeek 模型端口（`IntentModel` / `DuplicateModel` / `ReportModel`） |
| 10 | 前六节点到人工确认的 LangGraph（`candidates_resolved` 结束） |
| 11 | 证据抽取、报告生成、事实校验（后四节点） |
| 12 | PostgreSQL Checkpoint 与运行恢复 |

### 1.2 不可改变的 P0 决策（节选与本批次相关者）

- P0 是 **snapshot-only**，不是实时爬虫。
- P0 使用一个有界 LangGraph，不为了简历标签拆成多个 Agent。
- **LLM 只负责**语义解析、模糊去重和有证据摘要。日期/预算/过滤/精确去重/引用完整性/调度/幂等**由确定性代码负责**（任务 8 已完成，任务 9 不得实现或调用 LLM 处理这些事）。
- LangGraph checkpoint 走 **PostgreSQL**（`langgraph-checkpoint-postgres`），`thread_id = str(run_id)`。
- **DeepSeek 只在显式配置、服务端授权后使用**；公开 Demo 默认使用 deterministic fake model 和 hash embeddings。
- 所有当前时间通过 `Clock` 注入；业务代码不得直接 `datetime.now()`。
- 在线报告与 DOCX 使用同一个类型化 `Report` 模型。
- 防 prompt injection：导入文本只能放在 `UNTRUSTED_SOURCE_DATA` 区域。

---

## 2. 当前现场：任务 9-12 接管前必须知道

### 2.1 Git 结构与审查基线

```text
C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
branch: feat/bidscope-p0
code baseline: c4442a8 fix: address batch-2 review findings (M1-M6, L1-4)
HEAD: code baseline + this handoff's documentation-only commit
status: clean
```

冻结边界同前一批次（任务 1-8 全部冻结）。本批次会在以下**已允许位置**新增代码，其余位置一律不得改写：

- 任务 9 → 新建 `backend/src/bidscope/llm/*`、`backend/tests/unit/llm/*`
- 任务 10 → 新建 `backend/src/bidscope/graph/*`、`backend/tests/unit/graph/*`
- 任务 11 → 新建 `backend/src/bidscope/evidence/*`、`backend/tests/unit/evidence/*`、`backend/tests/unit/graph/test_report_retry.py`；**可改**已有 `graph/nodes.py`、`graph/builder.py`
- 任务 12 → 新建 `backend/src/bidscope/graph/executor.py`、`backend/tests/integration/test_graph_persistence.py`、`backend/tests/integration/test_run_recovery.py`；**可改**已有 `persistence/repositories.py`

### 2.2 当前依赖、数据库与验证状态

关键锁定版本：

- LangGraph `>=0.2.60,<1`
- langgraph-checkpoint-postgres `>=2,<3`
- langchain-openai `>=0.3,<1`
- pytest `8.4.2`
- mypy `1.20.2`
- Ruff `0.15.22`

两个数据库 URL 必须指向同一 host、port 和 test/e2e 数据库；仅 driver 可不同：

```text
BIDSCOPE_APP_MODE=test
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

扩展集成命令（任务 12 必用）：

```text
uv run bidscope checkpoints setup
```

当前 Alembic 头：`73514f2a...` → 实为 `c7a5e1d3a2f4_retrieval_indexes.py`（trigram GIN + HNSW cosine）。任务 12 checkpoint schema 由 `langgraph-checkpoint-postgres` 管理，**不是** Alembic；`bidscope checkpoints setup` 显式触发。

任务 5-8 放行时的独立门禁（任务 9-12 必须保持绿色）：

```text
pytest:        155 passed, 1 skipped, 1 warning
Ruff:          passed
mypy:          32 source files passed
Alembic check: passed
git diff --check: passed
```

### 2.3 已完成的领域契约（任务 9-12 直接依赖）

新建 LLM/graph 模块前必须熟知以下冻结契约：

| 契约 | 文件 | 任务 9-12 用途 |
|---|---|---|
| `SearchIntent` | `domain/intents.py` | 任务 9 `IntentModel.parse` 返回值；任务 10 `parse_intent` 输出 |
| `RetrievalPlan` | `domain/intents.py` | 任务 10 `build_retrieval_plan` 输出 |
| `Report`, `ReportItem`, `ReportClaim`, `ReportCitation` | `domain/reports.py` | 任务 9 `ReportModel.synthesize` 输入；任务 11 `synthesize_report`/`validate_report` |
| `NoticeView` | `retrieval/deduplication.py` | 任务 9 `DuplicateModel.classify` 输入；任务 11 证据抽取输入 |
| `HashEmbeddingProvider` | `retrieval/embeddings.py` | 任务 9 默认/测试用 provider；任务 10 检索节点 |
| `DuplicateDecision` | `retrieval/deduplication.py` | 任务 9 `DuplicateModel.classify` 输出 |

禁止任何改写、回退或顺手重构上述契约。如需接口扩展，只作当前任务不可避免的**加法**，并在审查包中逐项解释。

### 2.4 前一批次审查修复摘要（c4442a8，本批次必须保持）

| 编号 | 修复 | 位置 |
|---|---|---|
| M1 | `_build_evidence` 扩展为 publish_time/deadline 创建证据 | `snapshots/importer.py` |
| M2 | `inspect_bundle` 解析相对路径：`bundle_path.resolve()` | `snapshots/adapters.py` |
| M3 | 移除 `mark_import_failure` 死代码 + 注释说明完全回滚 | `snapshots/importer.py` |
| M4 | `_build_evidence` 用 `_METADATA_FIELD_KEYS` 白名单排除 `synthetic_channel` | `snapshots/importer.py` |
| M5 | `get_or_create_source_notice` savepoint + IntegrityError 复用 | `persistence/repositories.py` |
| M6 | 集成测试用 `FixedClock` 替代 `datetime.now()` | `integration/test_hybrid_search.py` |
| L1-L4 | project_number 去空白、URL 去 query/fragment、adapter 复用 manifest、`NoticeView` deadline tz-aware 校验 | `retrieval/deduplication.py`, `snapshots/*` |

任务 9-12 不得回退上述行为。

---

## 3. 任务 9-12 的精确推进地图

本节补充能力较弱模型最容易遗漏的进入条件、关键断言、退出条件和禁区。**每一文件与代码仍以详细计划为准。**

---

### 任务 9：Fake 与 DeepSeek 模型端口

**进入条件：** `c4442a8` 是 HEAD 的祖先，工作树干净，任务 1-8 全门禁通过，锁定依赖已核验可用。  
**目标:** 三个 async Protocol 的端口实现，Fake 完全离线确定性。

允许创建：

```text
backend/src/bidscope/llm/ports.py
backend/src/bidscope/llm/fake.py
backend/src/bidscope/llm/deepSeek.py
backend/tests/unit/llm/test_fake.py
backend/tests/unit/llm/test_deepseek_contract.py
```

#### 任务 9 RED

```bash
uv run pytest backend/tests/unit/llm -q
```

有效 RED：测试被收集，因 `IntentModel`/`DuplicateModel`/`ReportModel` 不存在或 `parse`/`classify`/`synthesize` 未实现而失败。

#### 任务 9 三个 Protocol

`backend/src/bidscope/llm/ports.py` 至少公开：

```python
class IntentModel(Protocol):
    async def parse(self, request: str, clock: Clock) -> SearchIntent: ...

class DuplicateModel(Protocol):
    async def classify(self, pair: DuplicatePair) -> DuplicateClassification: ...

class ReportModel(Protocol):
    async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft: ...
```

（协议签名以详细计划与 `domain/*` 契约为准。）

#### 任务 9 Fake 模型

必须满足：

- 完全离线、确定性；**不能偷偷调用外部 API / 网络**。
- 使用显式正则与 fixture 规则解析中文代表查询，断言解析到 topic、地区、7 天窗口、最低 500 万 CNY、每周一 09:00 排程。
- 协议签名与输入以现有 `SearchIntent` / `NoticeView` / `RetrievalPlan` / `Report` 等冻结契约为准；**禁止改写这些契约**适配 fake。
- `ModelUsage` 包含 model、tokens、latency、pricing_snapshot。

#### 任务 9 DeepSeek 模型

- 使用 `ChatOpenAI(base_url=settings.model_base_url, api_key=..., model=...)` + `with_structured_output`。
- prompt 用 `UNTRUSTED_SOURCE_DATA` 区域包裹导入文本，明确不能发工具指令。
- 单元测试必须使用 **stub transport**，证明测试收集期间无网络；**不得使用真实 key**。

#### 任务 9 完整验证与提交

```bash
uv run pytest backend/tests/unit/llm -q
uv run ruff check backend/src/bidscope/llm backend/tests/unit/llm
uv run mypy backend/src/bidscope/llm
git diff --cached --check
```

固定提交：

```text
feat: add deterministic and DeepSeek model ports
```

---

### 任务 10：意图、检索与人工确认 LangGraph

**进入条件：** 任务 9 已独立提交，工作树干净，`InMemorySaver`/`Command`/`interrupt`/`AsyncPostgresSaver` 可用。  
**目标:** 前六节点，结束状态 `candidates_resolved`（不是 `completed`）。

允许创建：

```text
backend/src/bidscope/graph/state.py
backend/src/bidscope/graph/nodes.py
backend/src/bidscope/graph/builder.py
backend/tests/unit/graph/test_confirmation.py
backend/tests/unit/graph/test_routing.py
```

#### 任务 10 RED

```bash
uv run pytest backend/tests/unit/graph/test_confirmation.py backend/tests/unit/graph/test_routing.py -q
```

#### 任务 10 六节点与行为边界

1. `parse_intent` — 调用任务 9 `IntentModel`。
2. `validate_intent` — 确定性校验日期、金额、地区、排程。
3. `confirm_intent` — **周期订阅必 `interrupt()`**；低置信度 / 冲突必必 `interrupt()`。
4. `build_retrieval_plan` — 输出 `RetrievalPlan`。
5. `retrieve_candidates` — 调用任务 7 `HybridSearcher`；Embedding 不可用时降级，`degraded_modes=["vector_unavailable"]`。
6. `resolve_duplicates` — 调用任务 8 `classify_duplicate`；仅 `ambiguous` 对才走任务 9 `DuplicateModel`。

禁止：把全文或所有候选送给模型；检索返回 notice/version ID 和有限评分元数据。

#### 任务 10 结构要求

- `RunState` 是 Pydantic 模型，用 ID 而非源正文。
- 编译用传入的 checkpointer，`recursion_limit=16`。
- 必须用真实契约：`SearchIntent`、`Money`、`Report`（`domain/*`）、`NoticeView`（`retrieval/deduplication.py`）。

#### 任务 10 完整验证与提交

```bash
uv run pytest backend/tests/unit/graph -q
uv run ruff check backend/src/bidscope/graph backend/tests/unit/graph
uv run mypy backend/src/bidscope/graph
git diff --cached --check
```

固定提交：

```text
feat: add confirmable LangGraph query workflow
```

---

### 任务 11：证据抽取、报告生成与事实校验

**进入条件：** 任务 10 已提交，工作树干净。  
**目标:** 后四节点，结束状态 `completed`；严格证据优先。

允许创建：

```text
backend/src/bidscope/evidence/extractor.py
backend/src/bidscope/evidence/validator.py
backend/tests/unit/evidence/test_validator.py
backend/tests/unit/graph/test_report_retry.py
```

可对现有文件作**必要且最小**的扩展：`graph/nodes.py`、`graph/builder.py`。

#### 任务 11 RED

```bash
uv run pytest backend/tests/unit/evidence backend/tests/unit/graph/test_report_retry.py -q
```

#### 任务 11 后四节点与事实校验

7. `verify_evidence` — 校验 evidence 存在、notice version 一致、offset 有效、span hash 一致、source URL 有效、每个 claim 有 citation。
8. `synthesize_report` — 调用任务 9 `ReportModel`。
9. `validate_report` — 失败一次只重试 synthesis，不重新 retrieval；第二次失败返回 `EvidenceInsufficient`，**绝不交付无证据报告**。
10. `persist_and_deliver` — 保存结果，推进订阅 seen set 仅发生在 report commit 之后。

#### 任务 11 retry 边界

- 第一次报告验证失败 → 只重试 synthesis，**不重新 retrieval**。
- 第二次失败 → `EvidenceInsufficient`。
- 检索调用次数恒为 1（retry 测试必证）。

#### 任务 11 完整验证与提交

```bash
uv run pytest backend/tests/unit/evidence backend/tests/unit/graph -q
uv run ruff check backend/src/bidscope/evidence backend/src/bidscope/graph backend/tests/unit/evidence backend/tests/unit/graph
uv run mypy backend/src/bidscope/evidence backend/src/bidscope/graph
git diff --cached --check
```

固定提交：

```text
feat: enforce evidence-backed Agent reports
```

---

### 任务 12：PostgreSQL Checkpoint 与运行恢复

**进入条件：** 任务 11 已提交，工作树干净，Postgres 已启动。  
**目标:** 跨进程恢复，checkpoint 持久化与运行事件。

允许创建：

```text
backend/src/bidscope/graph/executor.py
backend/tests/integration/test_graph_persistence.py
backend/tests/integration/test_run_recovery.py
```

可对现有文件作**必要且最小**的扩展：`persistence/repositories.py`。

#### 任务 12 RED

```bash
BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py -q
```

#### 任务 12 关键要求

- 使用 `AsyncPostgresSaver.from_conn_string(settings.checkpoint_database_url)`；`thread_id = str(run_id)`。
- **`await checkpointer.setup()` 只由 `uv run bidscope checkpoints setup` 触发**，不得隐式调用。
- 跨进程恢复测试：
  1. 图 A 运行到 `interrupt`。
  2. 关闭图 A / checkpointer 上下文。
  3. 创建图 B / checkpointer。
  4. 同 thread_id + `Command(resume=...)`。
  5. 断言上游节点事件不重复。
- 启动时标记 stale `running` rows 为 `retryable`；保留 checkpoint 供显式重试。

#### 任务 12 CLI

使用 Typer 暴露（承接任务 6 `cli.py` 已有 `snapshots` 子命令）：

```text
uv run bidscope checkpoints setup
```

#### 任务 12 完整验证与提交

```bash
uv run bidscope checkpoints setup
BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py -q

uv run ruff check backend/src/bidscope/graph backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py
uv run mypy backend/src/bidscope/graph
git diff --cached --check
```

固定提交：

```text
feat: persist Agent checkpoints and run events
```

完成任务 12 后**立即暂停**。不要执行任务 13。

---

## 4. 前批次通用协议与门禁格式（直接沿用）

以下沿用 `docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md` 第 4–13 节，**全部适用**于任务 9-12：

- **第 4 节** 通用执行协议（RED → GREEN → REFACTOR → COMMIT）
- **第 5 节** 任务 13-20 推进地图（任务 9-12 同批次但不在本手册范围，不要提前实现）
- **第 6 节** Windows、Git Bash、uv、Docker 常见故障
- **第 7 节** 每次任务完成后的强制自审（规格符合性、代码质量、Git 审核）
- **第 8 节** 每轮必须返回的格式（Task N Result 模板）
- **第 9 节** 阻塞处理
- **第 10 节** 分支、提交与最终集成
- **第 11 节** 任务清单（任务 9-12 当前批次）
- **第 12 节** 接管后第一条实际操作
- **第 13 节** 审查包模板（**第三批审查包模板**用本节）

关键纪律重申：

- 第 7.1 节新增一条：**禁止提前实现任务 13**（DOCX），禁止添加任务 13-20 的 API、调度、前端代码。
- 第 8 节格式、第 13 节审查包模板适用于本批次。

---

## 5. 对本批次（任务 9-12）特别容易遗漏的提醒

### 5.1 TDD RED 必须是有效 RED

任务 9-12 很多模块依赖 LangGraph/LangChain 类型（`Command`、`interrupt`、`AsyncPostgresSaver`、`ChatOpenAI`、`with_structured_output`）。提醒：

- `ImportError: No module named 'bidscope.llm'` / `bidscope.graph` / `bidscope.evidence` 是**有效 RED**（目标模块不存在）。
- `ModuleNotFoundError: ChatOpenAI` / `AsyncPostgresSaver` 是**无效 RED**（依赖缺失）。如果锁定依赖 import 失败，停止核验第 0 节预检命令，不要继续。
- 语法错误、路径拼写错误、拼错 import 都是无效 RED。

### 5.2 不得改写冻结契约，即使“顺手”

任务 9-12 大量调用 `domain/*`、`retrieval/*` 契约。常见诱惑：

- 为了让 fake model 更简单而给 `SearchIntent` 加字段 → **禁止**。
- 为了让 graph 节点更省事而把 `NoticeView` 改成非 frozen → **禁止**。
- 为了让 `confirm_intent` 跳过 interrupt → **禁止**。

如需接口扩展，只能作当前任务不可避免的加法，并在审查包中逐项解释。

### 5.3 数据真实性边界（L再提醒）

- 任务 9 的 fake model 测试可继续用 `data/demo/batch-1`、`example.invalid`、`demo-` IDs、`source=synthetic_demo`。
- 任务 9 的 DeepSeek contract 测试使用 stub transport，**不得使用真实 key、不得访问真实网络**。
- 任务 12 的 checkpoint / recovery 测试使用 `bidscope_test` 数据库，**不得用生产库**。

### 5.4 线程安全

LangGraph `thread_id = str(run_id)` 是必选约定。任务 12 跨进程恢复测试图的 A 与图 B 必须同一 `thread_id`。

---

## 6. 第三批审查包模板（任务 9-12 完成后使用）

完成任务 12 后立即暂停。把下面模板填完整后交给审查模型；不得进入任务 13。

```markdown
# BidScope 第三批审查请求

## Batch
Tasks: 9-12
Base SHA: c4442a8
Head SHA: <任务12提交SHA>
Branch: feat/bidscope-p0

## Commits
- Task 9: <SHA> feat: add deterministic and DeepSeek model ports
- Task 10: <SHA> feat: add confirmable LangGraph query workflow
- Task 11: <SHA> feat: enforce evidence-backed Agent reports
- Task 12: <SHA> feat: persist Agent checkpoints and run events

## Task 9 RED/GREEN
- RED command:
- RED exit code:
- RED expected reason:
- GREEN command:
- GREEN pass count:
- Fake model representative-query evidence:
- DeepSeek contract stub evidence:
- Network access performed: no

## Task 10 RED/GREEN
- RED commands:
- GREEN commands:
- Interrupt/resume evidence:
- Confirm-interrupt evidence:
- Vector-failure degradation evidence:

## Task 11 RED/GREEN
- RED commands:
- GREEN commands:
- Evidence-validation coverage:
- Retry-once evidence (retrieval count == 1):
- EvidenceInsufficient path evidence:

## Task 12 RED/GREEN
- RED commands:
- GREEN commands:
- CLI setup evidence:
- Cross-instance resume evidence:
- thread_id = str(run_id) used: yes

## Batch Verification
- Full pytest command/result:
- Ruff command/result:
- mypy command/result:
- Alembic check result:
- git diff --check c4442a8..HEAD:
- git status:

## Data Truthfulness
- Fake IDs use demo-*: yes/demo IDs from fixtures
- Synthetic URLs use example.invalid: yes
- DeepSeek tests use stub transport (no real key/network): yes
- LLM/network calls in tasks 9-12: none (except stubbed DeepSeek)

## Files Changed Outside Planned Scope
- None / list every file and reason

## Deviations
- None / explain each deviation and why it was necessary

## Known Risks
- None / list

## Stop Confirmation
- Task 13 files created: no
- Worktree clean: yes/no
```

---

*本文与 `docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md`（任务 1-8）配套使用；前者通用条款、Windows 排障、通用审查包模板沿用，本文补充任务 9-12 进入条件、契约依赖、推进地图、第三批审查包模板。*
