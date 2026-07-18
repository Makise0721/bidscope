# BidScope 超详细实施执行手册

**用途：** 本文供能力较弱、上下文较短或容易越界的编码模型接管 BidScope 实现。  
**项目根仓库：** `C:\Users\29913\zcode_workspace\bidscope`  
**实现 worktree：** `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0`  
**实现分支：** `feat/bidscope-p0`  
**任务 1 草稿起点：** `198ba54 chore: ignore local worktrees`（手册提交后分支 HEAD 会前进，但任务 1 仍未提交）  
**设计规格：** `docs/superpowers/specs/2026-07-18-bidscope-design.md`  
**详细计划：** `docs/superpowers/plans/2026-07-18-bidscope-implementation.md`  
**本文版本：** 2026-07-18

---

## 0. 可以直接交给接管模型的启动提示词

将下面整段原样交给接管模型。第一次只让它执行任务 1，不要一次要求完成全部 20 个任务。

```text
你正在实现 BidScope。请严格遵守以下规则：

1. 只在这个目录工作：
   C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
2. 当前分支必须是 feat/bidscope-p0；禁止在 main 上实现。
3. 先阅读：
   - docs/superpowers/specs/2026-07-18-bidscope-design.md
   - docs/superpowers/plans/2026-07-18-bidscope-implementation.md
   - docs/superpowers/handoffs/2026-07-18-bidscope-execution-guide.md
4. 当前只执行执行手册指定的一个任务。不要提前创建后续任务的文件。
5. 严格执行 TDD：
   - 先写测试；
   - 运行测试并看见它因功能缺失而失败；
   - 保存 RED 命令与关键输出；
   - 再写最小生产代码；
   - 运行目标测试得到 GREEN；
   - 最后运行该任务规定的完整验证。
6. 不得把路径错误、依赖缺失、语法错误当作有效 RED。有效 RED 必须说明：测试已经被 pytest/Vitest 收集，失败原因是目标行为尚未实现。
7. 不得绕过登录、验证码、WAF、付费墙或反爬措施。P0 运行时不访问任何招投标网站。
8. 官方来源数据、公开摘录、合成 Demo 数据必须严格区分：
   - raw_response
   - curated_public_excerpt
   - synthetic_demo
9. 合成数据只能使用 example.invalid URL、demo-* 或 eval-* ID，并在 UI/报告中明确标注。
10. 每个任务完成后：
    - 运行 git diff --check；
    - 检查 git status；
    - 提交且只提交该任务；
    - 返回 RED、GREEN、完整验证、提交 SHA、遗留问题。
11. 遇到不确定行为时，不要猜。先查设计规格和详细计划；仍不清楚才停止并报告。
12. 禁止删除或覆盖不属于当前任务的文件，禁止重置 main，禁止 git reset --hard。

当前从任务 1 开始。先执行“第 3 节：任务 1 现场恢复”，不要实现任务 2。
```

---

## 1. 项目目标与不能改变的决策

### 1.1 项目目标

BidScope 是一个证据优先的招投标情报 Agent。用户输入自然语言查询，例如：

> 每周一上午 9 点，汇总近 7 天四川和重庆与“智算中心、服务器”有关、预算 500 万以上的招标信息。

系统要完成：

1. 解析主题、地区、时间、预算与频率。
2. 让用户确认结构化意图。
3. 在已经导入、版本化的数据中检索。
4. 去重并识别公告变化。
5. 让每条事实绑定不可变证据片段。
6. 生成在线报告与 DOCX。
7. 保存订阅并只报告新增或实质变更。
8. 保存 LangGraph checkpoint、节点事件、错误、耗时和成本。
9. 提供可复现评测。

### 1.2 不可改变的 P0 决策

接管模型不得擅自改变以下决策：

- P0 是 **snapshot-only**，不是实时爬虫。
- Web 查询路径和后台调度器都不访问公开招投标网站。
- P0 使用一个有界 LangGraph，不为了简历标签拆成多个 Agent。
- LLM 只负责语义解析、模糊去重和有证据摘要。
- 日期、预算、过滤、精确去重、引用完整性、调度、幂等由确定性代码负责。
- PostgreSQL 是唯一关系数据库；不要增加 SQLite 分支。
- pgvector 用于向量检索；Embedding 不可用时必须降级到词法检索。
- APScheduler 3.x + PostgreSQL advisory lock；不要增加 Celery/Redis。
- 在线报告与 DOCX 使用同一个类型化 `Report` 模型。
- 公开 Demo 默认使用 deterministic fake model 和 hash embeddings。
- DeepSeek 只在显式配置、服务端授权后使用。
- 所有时间通过 `Clock` 注入；业务代码不得直接 `datetime.now()`。
- 原始公告版本和证据永不覆盖。
- 任务目标指标是验收目标，不是已经实现的简历数据。

### 1.3 三种证据等级

| `capture_kind` | 含义 | 允许 URL | 展示要求 |
|---|---|---|---|
| `raw_response` | 获得授权或合法人工取得的真实原始响应 | 官方 HTTPS 白名单 | 标注原始响应、抓取时间、hash |
| `curated_public_excerpt` | 公开字段经人工核验后的摘录，不是原始响应 | 官方 HTTPS 白名单 | 标注公开摘录，不得称为原文快照 |
| `synthetic_demo` | 为主演示、增量、故障和 E2E 构造的数据 | 仅 `https://example.invalid/` | 每条记录持续显示“合成演示数据” |

禁止：

- 把自造公告写成 `curated_public_excerpt`。
- 让 `synthetic_demo` 使用 `ccgp.gov.cn` 或 `ggzy.gov.cn` URL。
- 让合成数据的 `source` 冒充官方来源。
- 让 `.invalid` URL 变成可点击外链。

---

## 2. 当前现场：接管前必须知道

### 2.1 Git 结构

主仓库：

```text
C:\Users\29913\zcode_workspace\bidscope
branch: main
HEAD: 198ba54 chore: ignore local worktrees
status: clean
```

实现 worktree：

```text
C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-p0
branch: feat/bidscope-p0
HEAD: 198ba54 chore: ignore local worktrees
```

禁止在主仓库目录实施。所有代码命令都应明确使用实现 worktree。

### 2.2 任务 1 当前未提交文件

实现 worktree 当前预期状态：

```text
 M .gitignore
?? .env.example
?? backend/
?? compose.yaml
?? pyproject.toml
?? uv.lock
```

这些是一次未完成的任务 1 尝试。不要直接提交，也不要假定它们正确。

现有测试文件：

- `backend/tests/unit/test_clock.py`
- `backend/tests/unit/test_health.py`

现有项目配置草稿：

- `pyproject.toml`
- `uv.lock`
- `.env.example`
- `compose.yaml`
- `.gitignore`

生产模块已经为了恢复 TDD 顺序而删除，预期不存在：

- `backend/src/bidscope/clock.py`
- `backend/src/bidscope/config.py`
- `backend/src/bidscope/main.py`

保留的空包文件：

- `backend/src/bidscope/__init__.py`

### 2.3 虚拟环境状态

存在：

```text
.worktrees\bidscope-p0\.venv\Scripts\python.exe
Python 3.12.10
```

但在最后一次核验时：

```text
fastapi=missing
pytest=missing
```

`uv.lock` 已生成，但 `uv sync --all-groups --frozen` 多次被终端任务中断，不能认为依赖安装完成。

锁文件中关键解析版本包括：

- FastAPI `0.139.2`
- LangGraph `0.6.11`
- langgraph-checkpoint-postgres `2.0.25`
- pytest `8.4.2`
- mypy `1.20.2`
- Ruff `0.15.22`

注意：详细计划曾参考更新版 LangGraph 官方示例。实际锁定的是 LangGraph 0.6.11。实现任务 10/12 前必须针对这个锁定版本核验 `interrupt`、`Command`、stream 和 `AsyncPostgresSaver` API，不能机械照抄其他版本示例。

### 2.4 进程状态

最后一个残留 `uv` Windows 进程已被终止。接管模型仍应先运行：

```bash
ps -W | rg -i 'uv|pytest|python' || true
```

如果看到与本项目相关的残留进程，先根据 Windows PID 终止。`ps -W` 输出中通常：

```text
POSIX_PID PPID WIN_PID ... command
```

`taskkill.exe /PID` 要使用 `WIN_PID`，并在 Git Bash 中关闭路径转换：

```bash
MSYS_NO_PATHCONV=1 taskkill.exe /PID <WIN_PID> /T /F
```

---

## 3. 任务 1 现场恢复：必须从这里开始

任务 1 目标：建立 Python/uv 后端、可注入时钟、健康检查、PostgreSQL/MinIO 开发服务和第一条完整测试链。

### 3.1 第一步：进入正确目录并核验状态

Git Bash：

```bash
cd /c/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0
git branch --show-current
git status --short --branch
git log -3 --oneline
```

必须看到：

```text
feat/bidscope-p0
```

如果当前分支是 `main`，立即停止，不要写文件。

### 3.2 第二步：检查生产模块确实不存在

```bash
for f in clock.py config.py main.py; do
  if [ -e "backend/src/bidscope/$f" ]; then
    printf '%s=present\n' "$f"
  else
    printf '%s=absent\n' "$f"
  fi
done
```

继续前必须得到：

```text
clock.py=absent
config.py=absent
main.py=absent
```

若文件重新出现，说明另一个进程修改了 worktree。先读取并确认来源，不要覆盖。

### 3.3 第三步：完成依赖同步

优先使用 worktree 内项目参数：

```bash
uv sync --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" --all-groups --frozen
```

不要在命令中依赖 `cd` 的隐式目录。

同步可能需要较长时间。必须等待明确 exit code `0`。如果交互工具会取消长命令，可使用后台方式，但最终必须验证：

```bash
"C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/.venv/Scripts/python.exe" -c "import fastapi, pytest; print('deps=ready')"
```

有效成功输出：

```text
deps=ready
```

#### 依赖同步失败处理

1. 先检查是否有残留 `uv`：

```bash
ps -W | rg -i 'uv|pytest|python' || true
```

2. 若 `.venv` 被半安装状态锁住，先确认没有进程，再删除 **仅这个 worktree 的 `.venv`**：

```bash
rm -rf "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/.venv"
```

3. 重新使用已有锁文件：

```bash
uv sync --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" --all-groups --frozen
```

4. 如果 `--frozen` 报锁文件与项目配置不一致：

```bash
git diff -- pyproject.toml uv.lock
```

不要直接删除 `uv.lock`。只有确认 `pyproject.toml` 需要修正时，修正后运行：

```bash
uv lock --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0"
uv sync --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" --all-groups --frozen
```

5. 如果是单个包下载超时，保留锁文件并重试同步；不要临时改成全局 `pip install`。

### 3.4 第四步：取得有效 RED

依赖完成后运行：

```bash
"C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/.venv/Scripts/python.exe" \
  -m pytest \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_clock.py" \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_health.py" \
  -q
```

有效 RED：

- pytest 能启动；
- 测试文件能找到；
- 收集时报 `ModuleNotFoundError: No module named 'bidscope.clock'` 或 `bidscope.main`；
- 失败原因是目标模块尚未实现。

无效 RED：

- `No module named pytest`
- `file or directory not found`
- Python 语法错误
- 拼错 import
- 运行了主仓库而不是 worktree

把命令和关键错误写进任务交接记录。

### 3.5 第五步：实现最小 GREEN

创建 `backend/src/bidscope/clock.py`：

```python
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("fixed clock requires a timezone-aware value")
        self.value = value

    def now(self) -> datetime:
        return self.value
```

创建 `backend/src/bidscope/config.py`：

```python
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIDSCOPE_",
        env_file=".env",
        extra="ignore",
    )

    app_mode: Literal["demo", "development", "production", "test"] = "demo"
    database_url: str = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"
    checkpoint_database_url: str = "postgresql://bidscope:bidscope@localhost:5432/bidscope"
    real_model_enabled: bool = False
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

创建 `backend/src/bidscope/main.py`：

```python
from fastapi import FastAPI

from bidscope.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(title="BidScope", version="0.1.0")

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": resolved_settings.app_mode}

    return application


app = create_app()
```

说明：未完成尝试中的 `healthz` 是同步函数。计划要求 FastAPI API，使用 `async def` 更符合后续异步生命周期，但当前测试两者都能通过。这里固定使用 `async def`，不要引入数据库或 lifespan。

### 3.6 第六步：运行 GREEN

```bash
"C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/.venv/Scripts/python.exe" \
  -m pytest \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_clock.py" \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_health.py" \
  -q
```

预期：

```text
4 passed
```

如果不是 4 个测试，先查看测试文件，不要改断言来迎合实现。

### 3.7 第七步：审计任务 1 配置

#### `pyproject.toml`

必须满足：

- Python `>=3.12,<3.13`
- package path `backend/src/bidscope`
- runtime dependencies 与详细计划任务 1 一致
- dev dependencies 含 pytest、pytest-asyncio、pytest-cov、Ruff、mypy、testcontainers
- pytest `asyncio_mode = "auto"`
- pytest path `backend/tests`
- Ruff line length 100、target Python 3.12
- mypy strict、package `bidscope`

#### `.gitignore`

必须保留：

```text
.worktrees/
```

不得忽略：

- `uv.lock`
- `package-lock.json`
- 顶层 `data/`
- 顶层 `eval/data/`

必须忽略：

- `.venv/`
- `.env`，但不忽略 `.env.example`
- Python cache、pytest/mypy/Ruff cache
- `.data/`、`.minio/`、`.postgres/`
- `node_modules/`

#### `.env.example`

不得有真实密钥。允许：

```dotenv
BIDSCOPE_APP_MODE=demo
BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope
BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql://bidscope:bidscope@localhost:5432/bidscope
BIDSCOPE_REAL_MODEL_ENABLED=false
BIDSCOPE_ADMIN_TOKEN=
```

#### `compose.yaml`

任务 1 只允许 postgres 与 minio：

- PostgreSQL image `pgvector/pgvector:pg17`
- DB/user/password 均为 `bidscope`
- healthcheck 使用 `pg_isready`
- MinIO 使用固定、真实存在的 image tag
- MinIO healthcheck 可用容器中实际存在的命令
- 持久卷
- 不添加 API、scheduler、frontend

注意：不能只看 YAML 语法。任务 1 结束前运行：

```bash
docker compose -f "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/compose.yaml" config
```

如果 MinIO 固定 tag 不存在，改为一个已确认存在的版本；不要用不存在的猜测 tag。

### 3.8 第八步：任务 1 完整验证

从任意目录都可运行：

```bash
uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  ruff check "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend"

uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  mypy "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/src/bidscope"

uv run --project "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" \
  pytest \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_clock.py" \
  "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/backend/tests/unit/test_health.py" \
  -q

docker compose -f "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0/compose.yaml" config
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" diff --check
```

所有命令必须 exit 0。

### 3.9 第九步：任务 1 提交

先检查只包含任务 1：

```bash
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" status --short
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" diff --stat
```

然后：

```bash
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" add \
  pyproject.toml uv.lock .env.example .gitignore compose.yaml backend

git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" commit \
  -m "chore: bootstrap BidScope backend"
```

任务 1 完成后必须工作树干净：

```bash
git -C "C:/Users/29913/zcode_workspace/bidscope/.worktrees/bidscope-p0" status --short --branch
```

---

## 4. 所有任务通用执行协议

每个任务严格执行以下 12 步。任何一步失败都不要跳过。

### 4.1 进入任务

1. 确认分支：`feat/bidscope-p0`。
2. 确认工作树干净。
3. 从详细计划中只读取当前任务完整段落。
4. 列出当前任务允许创建/修改的文件。
5. 写一个任务内 checklist。

### 4.2 RED

6. 先创建或修改测试。
7. 运行最小测试命令。
8. 验证失败原因是目标行为缺失。

必须保存：

```text
RED command:
RED exit code:
RED expected failure:
RED key output:
```

### 4.3 GREEN

9. 只写使当前测试通过的最小代码。
10. 重跑最小测试，再跑任务完整验证。

必须保存：

```text
GREEN command:
GREEN exit code:
GREEN pass count:
Full verification commands:
```

### 4.4 REFACTOR 与提交

11. 只做局部重构，保持测试绿色；运行 `git diff --check`。
12. 自审后提交。

### 4.5 每个任务禁止事项

- 不提前创建下一个任务的文件。
- 不“顺便”重构其他模块。
- 不用 `# type: ignore` 掩盖新类型问题，除非有具体第三方库原因并写窄范围说明。
- 不用 `pragma: no cover` 隐藏核心分支。
- 不删除失败测试。
- 不把集成测试改成纯 mock 来绕过数据库。
- 不为了测试通过降低设计要求。
- 不把网络失败测试写成真实访问公共网站。
- 不在测试中使用真实 DeepSeek key。
- 不提交 `.env`、数据库数据卷、对象文件或生成的评测结果。

---

## 5. 任务 2 到任务 20 的精确推进地图

本节不是完整代码清单。每个任务的所有文件和代码片段仍以详细计划为准：

`docs/superpowers/plans/2026-07-18-bidscope-implementation.md`

本节补充能力较弱模型最容易遗漏的进入条件、关键断言、退出条件和禁区。

### 任务 2：类型化领域契约

**进入条件：** 任务 1 已提交，Ruff/mypy/4 个测试通过。  
**只允许：** `backend/src/bidscope/domain/*` 和 `backend/tests/unit/domain/*`。

必须先测：

- `SnapshotManifest` 拒绝 HTTP URL。
- 三种 capture kind 均存在。
- `SourceName.SYNTHETIC_DEMO` 与官方来源不可混淆。
- `synthetic_demo` 只能配 `example.invalid`。
- 所有时间必须 timezone-aware。
- `Money.minor_units` 为整数。
- `SearchIntent` 拒绝开始时间晚于结束时间。
- `SearchIntent` 拒绝最低预算高于最高预算。
- `ReportClaim` 至少一个 citation ID。
- 错误类型是可序列化、有限集合。

关键禁区：

- 不创建 SQLAlchemy model。
- 不访问数据库。
- 不写解析器。
- 不把大段公告正文放进 `RunState`。

退出验证：

```bash
uv run pytest backend/tests/unit/domain/test_contracts.py -q
uv run ruff check backend/src/bidscope/domain backend/tests/unit/domain
uv run mypy backend/src/bidscope/domain
```

提交：`feat: define BidScope domain contracts`

### 任务 3：PostgreSQL 模型、迁移与事务边界

**进入条件：** 任务 2 已提交。  
**依赖：** Docker 可运行。

先启动：

```bash
docker compose up -d postgres
docker compose ps
```

必须实现：

- `vector` 和 `pg_trgm` extension。
- 设计中的全部核心表。
- UUID PK。
- JSONB 保存有界源字段。
- `VECTOR(1024)`。
- 唯一约束：来源外部 ID、版本 hash、导入幂等 key、run key、subscription trigger key、report export key。
- UoW 正常退出 commit，异常 rollback。

测试不得只检查 ORM metadata；必须真的执行 Alembic 并 inspect PostgreSQL。

退出：

```bash
uv run alembic upgrade head
uv run pytest backend/tests/integration/test_migrations.py backend/tests/integration/test_unit_of_work.py -q
```

提交：`feat: add PostgreSQL persistence foundation`

### 任务 4：快照完整性与对象存储

必须先测：

- 修改 payload 后 hash 失败。
- manifest 外路径穿越失败。
- manifest 未声明的 payload 失败。
- 官方 capture kind 只允许官方白名单。
- synthetic_demo 只允许 `example.invalid`。
- synthetic_demo 强制 `source=synthetic_demo`。
- LocalObjectStore 原子写入。
- Local 与 S3 实现共享同一行为契约。

不要：

- 访问官方 URL。
- 在 manifest 中保存 cookie、token、验证码图片。
- 自动下载附件。

提交：`feat: validate snapshot bundles and object storage`

### 任务 5：官方与合成快照适配器

数据目录必须分开：

```text
data/snapshots/ccgp/2026-07-18-central-open/
data/snapshots/ggzy/2026-07-18-construction/
data/demo/batch-1/
data/demo/batch-2/
```

官方摘录：每个来源只使用已经核验的样例和字段，不凑数量。  
合成 Demo：至少 12 条 Batch 1；Batch 2 至少 2 新增、2 实质变更、2 未变化。

Demo 约束：

- ID `demo-*`
- URL `https://example.invalid/...`
- source `synthetic_demo`
- 可有 `synthetic_channel` 演示跨渠道重复
- UI/报告始终标合成

所有 Adapter 是纯离线解析器，不得包含 HTTPX、requests 或 Playwright 调用。

提交：`feat: add audited tender source snapshot adapters`

### 任务 6：幂等快照导入与版本保留

关键事务顺序：

1. 在打开写事务前检查 bundle 完整性。
2. 保存 payload 对象。
3. 创建或复用 source notice。
4. 仅 content hash 变化时新增 immutable version。
5. 创建证据。
6. 全部成功后 import 状态成功。

必须测：

- 同 bundle 导入两次不增加记录。
- Batch 2 的变更只增加版本，不增加逻辑 source notice。
- 中途失败无部分数据库记录。
- CLI `--json` 可机器读取。

提交：`feat: import versioned tender snapshots`

### 任务 7：结构化、词法与向量混合检索

先实现 deterministic `HashEmbeddingProvider`。真实 Embedding 只是端口实现。

正确顺序：

1. 地区、预算、日期等结构化过滤。
2. pg_trgm 词法候选。
3. pgvector 候选。
4. Reciprocal Rank Fusion。

必须测：

- Hash embedding 稳定、1024 维、归一化。
- 四川/重庆、500 万、时间窗在融合前生效。
- Embedding 失败时返回词法结果并记录 `vector_unavailable`。

禁止：把整库正文塞进模型上下文。

提交：`feat: add hybrid tender retrieval`

### 任务 8：确定性去重与实质变更

输出只有：

- `exact`
- `distinct`
- `ambiguous`

仅 `ambiguous` 可进模型。

实质变更字段仅：

- deadline
- budget
- region
- purchaser
- scope
- cancellation state
- 支持已报告 claim 的源文本

格式变化不是实质变更。

提交：`feat: classify duplicates and material changes`

### 任务 9：Fake 与 DeepSeek 模型端口

三个 async Protocol：

- `IntentModel.parse`
- `DuplicateModel.classify`
- `ReportModel.synthesize`

Fake 必须完全离线、确定性。DeepSeek 使用 OpenAI-compatible client 和 Pydantic structured output。

必须防 prompt injection：导入文本只能放在 `UNTRUSTED_SOURCE_DATA` 区域，明确不能发工具指令。

测试不得要求真实 key。使用 stub transport，证明测试收集期间无网络。

提交：`feat: add deterministic and DeepSeek model ports`

### 任务 10：意图、检索与人工确认 LangGraph

开始前先针对锁定版本核验：

```bash
uv run python -c "import langgraph; print(langgraph.__file__)"
uv run python -c "from langgraph.types import Command, interrupt; print(Command, interrupt)"
uv run python -c "from langgraph.checkpoint.memory import InMemorySaver; print(InMemorySaver)"
```

前六节点：

1. parse_intent
2. validate_intent
3. confirm_intent
4. build_retrieval_plan
5. retrieve_candidates
6. resolve_duplicates

周期订阅必确认；低置信度/冲突字段必 interrupt。

注意：本任务结束状态是 `candidates_resolved`，不是 `completed`。后四节点在任务 11 才加入。

提交：`feat: add confirmable LangGraph query workflow`

### 任务 11：证据抽取、报告生成与事实校验

后四节点：

7. verify_evidence
8. synthesize_report
9. validate_report
10. persist_and_deliver

必须校验：

- evidence 存在
- notice version 一致
- offset 有效
- span hash 一致
- source URL 有效
- 每个 claim 有 citation

第一次报告验证失败只重试 synthesis，不重新 retrieval。第二次失败返回 `EvidenceInsufficient`，绝不交付无证据报告。

提交：`feat: enforce evidence-backed Agent reports`

### 任务 12：PostgreSQL Checkpoint 与运行恢复

针对锁定版本先核验：

```bash
uv run python -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; print(AsyncPostgresSaver)"
```

必须显式命令初始化 checkpoint schema：

```bash
uv run bidscope checkpoints setup
```

`thread_id = str(run_id)`。跨进程恢复测试要：

1. 图 A 运行到 interrupt。
2. 关闭图 A/checkpointer 上下文。
3. 创建图 B/checkpointer。
4. 同 thread_id + `Command(resume=...)`。
5. 断言上游节点事件不重复。

提交：`feat: persist Agent checkpoints and run events`

### 任务 13：幂等 DOCX

DOCX 只接受类型化 `Report`，不得再调用模型。

必须包含：

- 查询条件
- 每项标题
- 未知字段标记
- 来源或合成 URL
- 证据编号
- 完整性警告
- source/version appendix

文件名固定从 report ID 和安全字符生成。重复导出只一个逻辑 export record 和 object key。

提交：`feat: render idempotent evidence reports`

### 任务 14：运行、报告与 SSE API

路由：

- `POST /api/runs`
- `GET /api/runs/{id}`
- `POST /api/runs/{id}/confirm`
- `POST /api/runs/{id}/retry`
- `GET /api/runs/{id}/events`
- `GET /api/reports/{id}`
- `GET /api/reports/{id}/docx`

SSE 必须：

- event ID 顺序
- `Last-Event-ID` 重连
- 15 秒 heartbeat
- terminal event 后结束

测试控制路由：

- 只在 `app_mode=test` 注册
- 单独 test token
- 只允许一次性节点失败和 Batch 2 导入
- demo/development/production 必须 404

不要把真实 model key 发到浏览器。

提交：`feat: expose Agent runs and reports over API`

### 任务 15：订阅、advisory lock、站内通知

同一 subscription + scheduled timestamp 只能一个 worker 成功。seen set 仅在 report commit 后推进。

三次连续失败暂停订阅并创建 inbox event。

时区使用 IANA 名称。所有当前时间来自 `Clock`。

提交：`feat: schedule incremental tender subscriptions`

### 任务 16：React 工作台

先写 MSW/Vitest 主流程测试，再实现。

桌面：三列。  
小于 1050px：轨迹/证据变 drawer。

必须有：

- loading
- empty
- partial
- error
- awaiting confirmation
- completed

合成数据：持续标签，“URL”仅纯文本不可点击。图标使用 lucide，陌生图标有 tooltip。不要嵌套 cards，不做营销 landing page。

提交：`feat: add BidScope evidence workbench`

### 任务 17：运营视图

页面：

- 运行历史
- 订阅与 inbox
- 数据来源/快照状态
- 评测

API 不返回原始 HTML 和完整 prompts。评测 UI 区分“目标”和“实测”。

提交：`feat: add BidScope operational views`

### 任务 18：版本化评测集与 runner

所有评测记录是独立 synthetic corpus：

- `eval-*` IDs
- `example.invalid` URLs
- 不与 `data/snapshots` 混用

最低数量：

- 100 intent
- 30 retrieval
- 100 dedup pairs
- 50 claims
- 30 E2E scenarios

runner 结果必须记录：

- git commit
- dataset hash
- model/provider
- pricing date
- fixture version
- environment
- 全部 metrics
- P50/P95
- tokens/cost

目标未达标不应伪装成功；命令只在 schema/执行故障时非零。

提交：`feat: add reproducible BidScope evaluation`

### 任务 19：安全、降级和恢复

安全矩阵至少覆盖：

- HTTP URL
- lookalike/user-info host
- 未声明文件
- path traversal
- hash 修改
- CAPTCHA/session artifacts
- 不安全 DOCX filename
- raw HTML rendering
- 任意 tool name
- SQL-like plan
- prompt injection 源文本

降级矩阵至少覆盖：

- 一来源 stale/invalid
- vector provider 失败
- model transient 两次重试
- evidence validation 一次 synthesis 重试
- DOCX 失败但 Web 报告成功
- stale running job 启动恢复

提交：`test: harden BidScope trust and recovery boundaries`

### 任务 20：容器化、E2E、文档与全量验证

Compose 最终包含：

- postgres
- minio
- api
- exactly one scheduler

创建独立 `bidscope_e2e` DB。Playwright 使用端口 8001 的 `app_mode=test` API，不复用公开 Demo。

六条 E2E：

1. 新查询 + 结构化意图
2. 人工确认
3. 报告 + 证据
4. 注入一次失败后重试
5. 创建订阅 + Batch 2
6. DOCX 下载

视口：

- 1440x900
- 390x844

完整命令以实施计划 Task 20 Step 4 为准。必须从干净 checkout 验证后才允许写简历实测指标。

提交：`chore: package and verify BidScope P0`

---

## 6. Windows、Git Bash、uv、Docker 常见故障

### 6.1 Bash 工作目录不会跨工具调用保持

不要依赖：

```bash
cd some/path
```

下一条工具命令可能回到主目录。优先：

```bash
git -C "C:/absolute/path" status
uv run --project "C:/absolute/path" ...
docker compose -f "C:/absolute/path/compose.yaml" ...
```

### 6.2 Git Bash 参数被转换成路径

Windows 命令的 `/PID` 等参数会被 Git Bash 转成路径。使用：

```bash
MSYS_NO_PATHCONV=1 taskkill.exe /PID 1234 /T /F
```

### 6.3 `ps -W` PID 列混淆

不要使用第一列 POSIX PID 调 `taskkill`。使用 Windows PID 列。若不确定，用：

```bash
powershell.exe -NoProfile -Command "Get-Process uv,python -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path"
```

### 6.4 `uv run` 长时间无输出

先检查：

```bash
ps -W | rg -i 'uv|python' || true
```

不要同时启动第二个 sync。验证安装完成的唯一依据：命令 exit 0，且 worktree `.venv` 可导入目标包。

### 6.5 不能用 `pytest` 全局命令

使用 worktree venv：

```bash
uv run --project "C:/.../bidscope-p0" pytest ...
```

或：

```bash
"C:/.../bidscope-p0/.venv/Scripts/python.exe" -m pytest ...
```

### 6.6 Docker 端口占用

检查：

```bash
docker compose ps
netstat -ano | rg ':5432|:9000|:9001|:8000|:8001'
```

如果是本项目已有容器，复用或干净停止；不要杀不明用户进程。计划固定端口前先确认来源。

### 6.7 CRLF 警告

Git 可能提示 LF 将被转换为 CRLF。这不是失败。真正检查：

```bash
git diff --check
```

不要为了消除警告批量改全仓库换行。

### 6.8 `apply_patch` 不存在

当前 Git Bash 曾返回 `apply_patch: command not found`。接管模型应使用自己的专用文件编辑工具；若只能用 shell，避免用 `cat`/heredoc 大规模覆盖已有文件。新建小文件可用可靠工具，修改已有文件先读取并精确替换。

### 6.9 站点访问限制

不要“测试一下爬虫”访问 CCGP/GGZY：

- CCGP 研究期间已出现 HTTP 403、WAF 和频繁访问提示。
- GGZY 页面接口未文档化，包含 CAPTCHA、反自动化错误码和结果上限。
- robots 指令不可依赖。

P0 所有测试离线运行。

---

## 7. 每次任务完成后的强制自审

### 7.1 规格符合性

逐项回答：

```text
[ ] 当前任务计划中的每个文件是否创建/修改？
[ ] 每个要求是否有测试或明确验证？
[ ] 是否遗漏错误路径？
[ ] 是否提前实现了后续任务？
[ ] 是否违反 snapshot-only？
[ ] 是否混淆 official / curated / synthetic？
[ ] 是否引入未经批准的依赖或服务？
```

### 7.2 代码质量

```text
[ ] 文件是否单一职责？
[ ] 名称是否与领域契约一致？
[ ] 是否有重复、魔法字符串、过宽异常捕获？
[ ] async/sync 边界是否清晰？
[ ] 事务和幂等是否明确？
[ ] 时间是否通过 Clock？
[ ] 大正文是否只存 ID/对象引用而非图状态/日志？
[ ] 测试是否真实测试行为，而非只测试 mock 调用次数？
[ ] Ruff/mypy 是否干净？
```

### 7.3 Git 审核

```bash
git status --short
git diff --stat
git diff --check
git diff --name-only
```

确认没有：

- `.env`
- `.venv`
- 数据卷
- 真实 key
- 与当前任务无关的 docs 修改
- 未解释的 lockfile 大变更

---

## 8. 接管模型每轮必须返回的格式

```markdown
## Task N Result

Status: DONE | BLOCKED | NEEDS_CONTEXT

### Scope
- Implemented:
- Explicitly not implemented:

### RED Evidence
- Command:
- Exit code:
- Expected failure:
- Key output:

### GREEN Evidence
- Command:
- Exit code:
- Pass count:
- Key output:

### Full Verification
- `<command>` -> PASS/FAIL
- `<command>` -> PASS/FAIL

### Files Changed
- `path`: purpose

### Commit
- SHA:
- Message:

### Self-review
- Spec gaps: none / list
- Quality concerns: none / list
- Residual risks:

### Next Task
- Ready for Task N+1: yes/no
- Required context:
```

如果没有真实 RED/GREEN 输出，不得写 `DONE`。

---

## 9. 阻塞时如何处理

### 9.1 可以自行解决

- 格式/lint/type 小问题。
- 锁定版本 API 差异：读本地安装包签名和官方对应版本文档，写测试确认。
- Windows 路径与命令调用。
- Docker 健康检查命令差异。
- Fixture parser 小结构变化。

### 9.2 必须停止报告

- 需要访问或绕过 CAPTCHA/WAF 才能继续。
- 需要把合成数据冒充官方数据。
- 需要删除用户已有提交或主分支历史。
- 设计规格与计划在核心行为上冲突，无法选择。
- 需要真实外部密钥但用户未提供。
- 任务要求发生不可逆外部操作或发布。
- 测试在干净基线已失败且与当前任务无关。

阻塞报告必须包含：

```text
BLOCKED at:
Exact command:
Exit code:
Error excerpt:
What was tried:
Why retrying verbatim will not help:
Smallest user decision needed:
```

---

## 10. 分支、提交与最终集成

每个任务一个提交，提交消息已在详细计划固定。禁止 squash 中间任务，除非用户最终明确要求。

完成 20 个任务后：

1. 在 `feat/bidscope-p0` 运行 Task 20 全门禁。
2. 运行最终代码审查。
3. 记录全部实测指标与环境。
4. 保持 `main` 不变，先向用户报告。
5. 由用户选择 merge、PR 或保留分支。

当前没有 remote/PR 授权。禁止自行 push 或发布线上服务。

---

## 11. 当前任务清单

```text
[ ] 1. 仓库骨架与健康检查纵向切片（已留下未提交草稿，尚未完成有效 RED/GREEN）
[ ] 2. 类型化领域契约
[ ] 3. PostgreSQL 模型、迁移与事务边界
[ ] 4. 快照完整性与对象存储
[ ] 5. 官方与合成快照适配器
[ ] 6. 幂等快照导入与版本保留
[ ] 7. 结构化、词法与向量混合检索
[ ] 8. 确定性去重与实质变更检测
[ ] 9. Fake 与 DeepSeek 模型端口
[ ] 10. 意图、检索与人工确认 LangGraph
[ ] 11. 证据抽取、报告生成与事实校验
[ ] 12. PostgreSQL Checkpoint 与运行恢复
[ ] 13. 幂等 DOCX 报告交付
[ ] 14. 运行、报告与 SSE API
[ ] 15. 订阅、调度锁与站内通知
[ ] 16. React 工作台主流程
[ ] 17. 运行、订阅、来源与评测运营视图
[ ] 18. 版本化评测集与评测运行器
[ ] 19. 安全、降级与故障恢复回归
[ ] 20. 容器化、E2E、文档与全量验证
```

更新原则：任务只有在提交存在、工作树干净、该任务完整验证通过时才能勾选。

---

## 12. 接管的第一条实际行动

接管模型的第一条实际操作应是：

```bash
ps -W | rg -i 'uv|pytest|python' || true
```

然后执行第 3.1 至 3.4 节，取得任务 1 的有效 RED。不要先写生产代码，不要运行任务 2，不要访问任何招投标站点。
