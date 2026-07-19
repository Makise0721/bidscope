# BidScope 任务 5-8 专用审查交接

**用途：** 本文只供一个独立窗口审查任务 5-8。
**禁止用途：** 不实施新功能，不直接修复问题，不继续任务 9。
**实现 worktree：** `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0`
**分支：** `feat/bidscope-p0`
**冻结审查范围：** `df73112..1ae53c8`
**代码基线：** `df73112`
**任务 5 提交：** `6d7151c`
**任务 6 提交：** `4b8e48c`
**任务 7 提交：** `6892acd`
**任务 8 提交：** `1ae53c8`
**冻结审查 HEAD：** `1ae53c8`

---

## 0. 可直接交给新审查窗口的提示词

```text
你只负责审查 BidScope 的任务 5-8，不实现功能、不改代码、不继续任务 9。

工作目录：
C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0

先阅读：
1. docs/superpowers/specs/2026-07-18-bidscope-design.md
2. docs/superpowers/plans/2026-07-18-bidscope-implementation.md
3. docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md
4. docs/superpowers/reviews/2026-07-19-tasks-5-8-review-handoff.md

冻结审查范围：df73112..1ae53c8
任务 5：6d7151c
任务 6：4b8e48c
任务 7：6892acd
任务 8：1ae53c8

注意：接管审查文档的提交会在 1ae53c8 之后，所以不要用变化后的 HEAD 替代审查 HEAD。始终审查 df73112..1ae53c8。

审查规则：
- 不相信实现模型提供的测试摘要，必须独立运行验证。
- Findings 优先，按 Critical/High/Medium/Low 排序。
- 每个 finding 必须给出文件与行号、影响、复现证据、修复要求。
- 重点审查正确性、数据真实性、事务/幂等、检索排序和测试漏洞。
- 不为了“看起来完成”降低标准。
- 不修改代码；只输出审查结论与下一轮修复门禁。
- 如果没有阻断项，明确放行任务 9-12。
- 如果有 Critical/High，不得放行任务 9。
```

---

## 1. 审查开始前的固定事实

### 1.1 已通过审查的前置批次

任务 1-4 和三轮修复已通过，不在本轮审查范围：

```text
a7a9c82  任务 1：仓库骨架与健康检查
d5f037b  任务 2：类型化领域契约
766839f  任务 3：PostgreSQL 持久层
5f02c97  任务 4：快照完整性与对象存储
b0129e2  修复 1
ed1d1ce  修复 2
9473946  修复 3
```

已确认的前置边界：

- 专用 test/e2e 数据库 fail-closed。
- Alembic 与 async DB URL 必须指向同一物理测试数据库。
- CCGP/GGZY source-to-host 精确映射。
- synthetic_demo 只能使用 `example.invalid` 和 `demo-*`。
- Manifest 必须通过类型化校验、hash 和文件类型校验。
- claim-to-evidence 是多对多关系。
- 审计记录采用保护性 `NO ACTION`，不能物理删除。

本轮若发现任务 5-8 破坏这些边界，应视为回归问题。

### 1.2 当前任务提交

```text
6d7151c  feat: add audited tender source snapshot adapters
4b8e48c  feat: import versioned tender snapshots
6892acd  feat: add hybrid tender retrieval
1ae53c8  feat: classify duplicates and material changes
```

任务 9 的 `backend/src/bidscope/llm/` 在冻结 HEAD 不应存在。

### 1.3 审查数据库环境

运行集成测试时必须显式设置：

```text
BIDSCOPE_APP_MODE=test
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

禁止用默认开发库运行集成测试。

---

## 2. 冻结审查范围核验

第一步只核验 Git，不运行测试：

```bash
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  merge-base --is-ancestor df73112 1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  log --oneline df73112..1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  diff --name-status df73112..1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  diff --check df73112..1ae53c8
```

必须确认只有四个任务提交，且没有任务 9 文件。

审查期间不要把后续交接文档提交包含进代码 diff。

---

## 3. 任务 5 审查：官方与合成快照适配器

### 3.1 目标文件

```text
backend/src/bidscope/snapshots/_parse.py
backend/src/bidscope/snapshots/ccgp.py
backend/src/bidscope/snapshots/ggzy.py
backend/src/bidscope/snapshots/demo.py
backend/tests/contract/*
data/snapshots/ccgp/2026-07-18-central-open/*
data/snapshots/ggzy/2026-07-18-construction/*
data/demo/batch-1/*
data/demo/batch-2/*
docs/source-policy.md
```

### 3.2 数据真实性检查

逐项核验：

- 官方 bundle 使用 `curated_public_excerpt`，不是 `raw_response`。
- CCGP 只用 CCGP host，GGZY 只用 GGZY host。
- 合成记录 source 为 `synthetic_demo`。
- 合成 external ID 全部以 `demo-` 开头。
- 合成 URL 全部是 `https://example.invalid/...`。
- 官方 expected.json 没有超出已核验字段。
- 不存在 cookie、token、验证码图片、session 或下载附件。
- Adapter 和测试中没有 HTTPX、requests、Playwright、urllib 网络调用。
- `docs/source-policy.md` 明确 snapshot-only 和站点限制。

建议命令：

```bash
rg -n -i "httpx|requests|playwright|urlopen|aiohttp|fetch\(" \
  backend/src/bidscope/snapshots backend/tests/contract

rg -n -i "cookie|captcha|token|authorization|session" data docs/source-policy.md
```

命中不一定是问题，但必须逐项判断是否只是政策说明。

### 3.3 Manifest 与 fixture 完整性

独立调用 `inspect_bundle()` 检查四个 bundle：

```text
data/snapshots/ccgp/2026-07-18-central-open
data/snapshots/ggzy/2026-07-18-construction
data/demo/batch-1
data/demo/batch-2
```

核对每个 manifest 的文件 hash 与实际字节一致。检查 `expected.json` 是否被列为 payload，且未被解析器偷偷信任为输入结果。

### 3.4 Parser 正确性

检查：

- Adapter 第一阶段验证 bundle，失败后不继续解析。
- HTML 解析只处理观察到的字段。
- 缺字段返回 `None`，不编造默认业务值。
- 时间均 timezone-aware。
- 金额使用 minor units，币种明确。
- 未识别标签进入 `raw_fields`。
- 结构漂移是类型化诊断，不是裸异常。
- `load_expected()` 只供测试，不参与生产解析。

### 3.5 Demo Batch 1/2 语义

独立按稳定 ID 比较两个 batch：

- Batch 1 至少 12 条。
- Batch 2 至少 2 条新增。
- Batch 2 至少 2 条实质变更。
- Batch 2 至少 2 条未变化。
- 不能只看数量；要比较同 ID 内容。
- `synthetic_channel` 不能改变 source 身份。

重点检查 expected.json 是否和 parser 输出独立形成，而不是复制同一个函数结果生成。

### 3.6 任务 5 测试命令

```bash
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests/contract -q

uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  ruff check backend/src/bidscope/snapshots backend/tests/contract

uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  mypy backend/src/bidscope/snapshots
```

---

## 4. 任务 6 审查：幂等导入与版本保留

### 4.1 目标文件

```text
backend/src/bidscope/cli.py
backend/src/bidscope/persistence/repositories.py
backend/src/bidscope/snapshots/importer.py
backend/tests/integration/test_snapshot_import.py
pyproject.toml
```

### 4.2 事务边界

核验顺序：

1. bundle 完整性检查发生在写事务前。
2. 无效 bundle 不产生 DB 行和对象。
3. object key 确定且幂等。
4. 创建/复用 SnapshotBundle。
5. 创建/复用 SourceNotice。
6. 相同 hash 不新增 NoticeVersion。
7. 不同 hash 新增 immutable NoticeVersion。
8. evidence 指向正确版本。
9. SnapshotImport 仅在全部成功后为 success。
10. 异常发生时事务 rollback。

### 4.3 对象存储与数据库不一致

对象存储不受 PostgreSQL 事务控制。检查：

- DB 失败后是否遗留语义错误对象。
- 确定性 key 是否使重试安全。
- 相同 payload 是否覆盖相同字节，而不是生成无限孤儿对象。
- 部分对象写入后失败是否有补偿或明确可接受策略。

如果代码声称“无部分记录”，必须区分数据库记录和对象存储对象。

### 4.4 幂等键

检查调用方是否显式提供语义幂等键。不得依赖随机默认。

建议尝试：

- 同一 bundle 连续导入。
- 同一 bundle 并发导入。
- 相同 external ID + 相同 hash。
- 相同 external ID + 不同 hash。
- 不同 source + 相同 external ID。
- 失败后重试。

并发幂等如果未覆盖，应重点关注唯一约束异常是否转成可理解的已有结果，还是直接 500。

### 4.5 证据正确性

检查证据：

- notice version ID 正确。
- start/end 与保存文本一致。
- span hash 可重算。
- 不引用 expected.json。
- 不把 parser 生成摘要误当原文证据。

### 4.6 CLI

独立检查：

```bash
uv run bidscope snapshots inspect <bundle> --json
uv run bidscope snapshots import <bundle> --json
```

JSON stdout 不能混入日志。失败必须非零 exit code。命令不得访问网络。

### 4.7 任务 6 测试命令

```bash
BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests/integration/test_snapshot_import.py -q
```

---

## 5. 任务 7 审查：混合检索

### 5.1 目标文件

```text
backend/src/bidscope/persistence/models.py
backend/src/bidscope/retrieval/embeddings.py
backend/src/bidscope/retrieval/search.py
backend/tests/unit/retrieval/test_embeddings.py
backend/tests/integration/test_hybrid_search.py
migrations/versions/c7a5e1d3a2f4_retrieval_indexes.py
```

### 5.2 HashEmbeddingProvider

核验：

- 不使用 Python `hash()`。
- 相同输入跨进程稳定。
- 1024 维。
- L2 norm 约等于 1。
- 空字符串行为明确。
- 批量顺序稳定。
- 无网络调用。

不能只在同一进程调用两次；至少启动两个 Python 子进程比较结果。

### 5.3 真实 Embedding 端口

检查 OpenAI-compatible provider：

- 测试使用注入 stub，不用真实 key。
- 维度不匹配时 fail closed。
- provider 异常不会导致整个检索失败。
- 不在日志中暴露 key 或全文。

### 5.4 结构化过滤顺序

必须确认 SQL 或调用顺序是：

1. 日期/地区/预算过滤。
2. 词法候选。
3. 向量候选。
4. RRF。

查找 top-K 是否先于结构化过滤。如果先 top-K，再 Python 过滤，是正确性问题。

### 5.5 词法与向量查询

检查：

- `pg_trgm` 操作与索引一致。
- cosine operator 与 vector index operator class 一致。
- NULL embedding 不导致 SQL 异常。
- 同一 notice/version 在融合后只出现一次。
- 排名和 tie-break 稳定。
- RRF 常量命名，不散落魔法数字。

### 5.6 降级

模拟 embedding provider 抛异常：

- 返回词法结果。
- `degraded_modes` 包含 `vector_unavailable`。
- 不错误报告 `retrieval_empty`。
- 结构化条件仍生效。

### 5.7 迁移

检查新增索引 migration：

- upgrade/downgrade 对称。
- 索引目标列与查询实际使用列一致。
- 不改写旧 migration。
- `alembic check` 无漂移。
- 迁移在空库和已有任务 1-6 schema 上都能升级。

### 5.8 任务 7 测试命令

```bash
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests/unit/retrieval/test_embeddings.py -q

BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests/integration/test_hybrid_search.py -q
```

---

## 6. 任务 8 审查：去重与实质变更

### 6.1 目标文件

```text
backend/src/bidscope/retrieval/deduplication.py
backend/tests/unit/retrieval/test_deduplication.py
backend/tests/unit/retrieval/test_material_changes.py
```

### 6.2 三态决策

输出只能是：

```text
exact
ambiguous
distinct
```

检查 exact 是否只使用强确定性证据。仅标题相似、同采购人或相近金额不能直接 exact。

重点反例：

- 两个空项目编号不能 exact。
- 项目编号大小写/空白归一化是否合理。
- 同 hash 但来源对象明显冲突时策略是否明确。
- URL query/fragment 是否影响 canonical URL。
- 相同标题、不同项目编号必须 distinct 或至少不是 exact。
- 跨 synthetic_channel 重复不能改变 source 身份。

### 6.3 ambiguous 边界

检查模糊证据只产生 ambiguous，不调用 LLM。任务 8 不应有模型依赖、网络调用或自由循环。

决策结果应包含稳定、可序列化、可解释的 reason/evidence，而不是只返回裸字符串。

### 6.4 实质变更

只报告：

- deadline
- budget
- region
- purchaser
- procurement scope
- cancellation state
- 支持已报告 claim 的源文本

检查格式归一化是否过度。不能为了忽略标点而抹掉金额、日期或否定词差异。

重点反例：

- `100 万` 与 `1000 万` 必须变化。
- `允许联合体` 与 `不允许联合体` 不能被归一化为相同。
- 时区不同但同一时刻可视为相同。
- CNY 和其他币种不能只比较 minor units。
- `None -> value` 和 `value -> None` 应是变化。
- `raw_fields` 无关字段变化不报告。
- 返回字段顺序稳定。

### 6.5 任务 8 测试命令

```bash
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests/unit/retrieval/test_deduplication.py \
  backend/tests/unit/retrieval/test_material_changes.py -q
```

---

## 7. 批次级独立验证

### 7.1 全量测试

```bash
BIDSCOPE_APP_MODE=test \
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test \
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest backend/tests -q
```

### 7.2 静态检查

```bash
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  ruff check backend migrations

uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  mypy backend/src/bidscope
```

### 7.3 迁移与 Git

```bash
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test \
uv run --directory "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  alembic check

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  diff --check df73112..1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  status --short --branch
```

### 7.4 网络边界

静态搜索不能替代完整审查，但可定位风险：

```bash
rg -n -i "httpx|requests|playwright|urlopen|aiohttp|socket|fetch\(" \
  backend/src/bidscope/snapshots \
  backend/src/bidscope/retrieval \
  backend/tests/contract \
  backend/tests/unit/retrieval
```

任务 5-8 不得出现对公开招投标站点的运行时访问。

---

## 8. 审查方法

### 8.1 不接受的证据

- “实现模型说测试通过”。
- 只运行新增测试，不运行旧测试。
- 只运行 `git diff --check` 对干净工作树，不检查冻结范围。
- 用 mock 证明 SQL 排名正确，却不运行 PostgreSQL 集成测试。
- 用 expected.json 作为生产解析结果。
- 把合成数据写成官方摘录。
- 只检查 happy path，不测试失败、并发和降级。

### 8.2 Findings 严重度

**Critical**：可能破坏真实数据、安全边界、来源真实性，或导致不可恢复错误。

**High**：核心功能错误、事务/幂等错误、检索结果错误、测试掩盖实现缺陷，必须修复后才能进入任务 9。

**Medium**：非主流程缺陷、维护性或测试覆盖问题，可根据影响决定是否阻断。

**Low**：命名、注释、轻微重复，不单独阻断。

### 8.3 每个 Finding 必须包含

```markdown
### [Severity] 简短标题

- Location: `path:line`
- Impact:
- Evidence/Reproduction:
- Why existing tests missed it:
- Required fix:
- Required regression test:
```

不要直接修改代码。审查完成后让实现模型提交修复包，再复审。

---

## 9. 审查输出模板

```markdown
# BidScope Tasks 5-8 Review

## Findings

### Critical
- None / findings

### High
- None / findings

### Medium
- None / findings

### Low
- None / findings

## Independent Verification
- Frozen range: df73112..1ae53c8
- Task 5 contract tests:
- Task 6 integration tests:
- Task 7 retrieval tests:
- Task 8 unit tests:
- Full pytest:
- Ruff:
- mypy:
- Alembic check:
- diff check:
- worktree status:

## Data Truthfulness
- CCGP curated records:
- GGZY curated records:
- Synthetic records:
- Invalid synthetic IDs/hosts:
- Live network paths found:
- Sensitive artifacts found:

## Scope Check
- Files outside plan:
- Task 9 files present:
- Unexplained deviations:

## Decision
- PASS: tasks 9-12 may start
- BLOCKED: fix Critical/High findings first

## Repair Gate
- Exact tests and commands required before re-review
```

Findings 必须置于总结之前。若没有 findings，也要说明审查了哪些高风险反例。

---

## 10. 放行标准

只有同时满足以下条件才能放行任务 9-12：

- 无 Critical/High finding。
- 官方/合成数据边界真实且可验证。
- Snapshot import 幂等、版本化、rollback 行为正确。
- 混合检索结构化过滤顺序正确。
- vector 失败可词法降级。
- 去重不会把模糊相似误判 exact。
- 实质变更不会丢失否定、金额、日期等语义变化。
- 所有独立门禁通过。
- `df73112..1ae53c8` diff clean。
- 任务 9 文件不存在。

若有阻断项，输出精确修复包要求。修复提交必须追加，不改写任务 5-8 历史。

---

## 11. 新窗口第一条实际操作

```bash
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  log --oneline df73112..1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  diff --name-status df73112..1ae53c8

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  diff --check df73112..1ae53c8
```

确认冻结范围后，从任务 5 的数据真实性与 Adapter 行为开始审查。不要先运行全量测试，也不要先写总结。
