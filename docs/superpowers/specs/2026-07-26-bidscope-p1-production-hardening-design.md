# BidScope P1 生产化加固设计

**状态：** 已确认设计基线
**日期：** 2026-07-26
**适用范围：** BidScope P0 之后的单机 Docker Compose 生产化加固
**前置基线：** BidScope P0 snapshot-only、证据闭环、订阅执行、对象存储、Docker 部署、Playwright E2E 与 CI 门禁已完成

## 1. 目标与约束

BidScope P1 的目标是把现有 P0 提升为可由个人或小团队长期运行的单机服务。P1 优先解决安全边界、运行可见性、数据保护和恢复能力，不扩张为新的产品形态。

已确认的运行基线：

- 部署环境：单机 Docker Compose。
- 使用规模：约 1–5 名用户，每天几十次运行。
- 恢复目标：RPO 24 小时，RTO 4 小时。
- 访问模型：单租户管理员，不引入用户账户、组织模型或复杂 RBAC。
- 数据来源：继续 snapshot-only；不在交互路径或 scheduler 中实现实时抓取。
- 模型：fake 模型继续作为默认可复现路径；真实模型仍须显式 opt-in。
- 备份：主机本地备份用于快速恢复，可选复制到外部 S3 用于主机损坏场景。
- 默认保留：每日备份 7 份、每周备份 4 份。

## 2. 范围边界

### 2.1 P1 包含

1. 生产配置 fail-closed 与单租户管理员访问控制。
2. 请求、运行、订阅、快照导入和报告交付的结构化可观测性。
3. readiness 检查、连接池与超时配置、优雅停机和并发上限。
4. 操作审计记录，且不泄露 token、API key、Cookie 或报告正文。
5. PostgreSQL 与对象存储的可验证备份、恢复命令和恢复演练。
6. Compose 发布、迁移兼容、应用回滚和密钥轮换 runbook。
7. 发布前的生产 smoke、备份恢复和关键配置门禁。

### 2.2 P1 不包含

- 多租户、用户注册、密码登录、社交登录、OIDC 或复杂角色体系。
- Kubernetes、Redis、Celery、外部 APM 或必须长期运行的新基础设施。
- 自动化实时抓取、绕过验证码/WAF/限流、未授权来源接入。
- 用合成 fixture 或快照一致性指标宣称真实生产质量。
- 自动数据库 downgrade 或没有兼容性检查的应用回滚。

## 3. 方案决策

比较过三种顺序：运维先行、安全先行和分层加固。选择分层加固，原因是 P0 已有较完整的测试闭环，安全、可观测性和恢复能力可以分别形成可独立验收的门禁，降低一次性发布的风险。

交付顺序固定为：

```text
P1-A 安全与配置基线
        ->
P1-B 可观测性与运行稳定性
        ->
P1-C 备份、恢复与发布运维
```

每一层必须先通过自己的测试和验收，再作为下一层的前置条件。

## 4. 总体架构

P1 保留 P0 的进程和存储拓扑：

```text
浏览器
  -> 反向代理（TLS、Host、可选网络访问控制）
  -> FastAPI API + 同源 React SPA
       |              |
       |              +-> PostgreSQL / pgvector
       |              +-> LangGraph checkpoint
       |              +-> S3/MinIO 对象存储
       |
       +-> 独立 scheduler 进程（单实例）
```

反向代理负责 TLS 终止和外部入口限制；BidScope 自身仍执行 Admin Token、Host、CORS 和业务授权校验，不能把反向代理视为唯一安全边界。API 和 scheduler 继续使用同一镜像、同一配置和同一数据库。P1 不改变现有 run ownership token、heartbeat、advisory lock、checkpoint 和幂等语义。

### 4.1 管理员访问流程

P1 不把管理员 token 编译进前端 bundle，也不把 token 放入 URL、Cookie、日志或报告。

- SPA 首次访问受反向代理保护；操作员在 Workbench 的受控设置入口输入 Admin Token。
- 前端仅将 token 保存在当前浏览器 tab 的 `sessionStorage`，通过同源请求的 `X-Admin-Token` 发送。
- 不使用 `localStorage`，不支持通过 query string 或 hash 传递 token。
- 清除设置或关闭 tab 后 token 消失；401 响应清空内存中的 token 并提示重新输入。
- API 对所有受保护业务路由统一使用 `require_admin_token`；新路由必须通过路由矩阵测试。
- `/healthz`、`/readyz` 和静态 SPA 入口保持公开，但健康响应只返回 bounded 状态，不包含 DSN、主机、凭据或异常堆栈。
- `/metrics` 默认受 Admin Token 保护；若部署者通过反向代理限制来源，也不能取消应用侧保护。

这保持单租户和无账户体系，同时避免把长期凭据暴露到构建产物中。

## 5. P1-A：安全与配置基线

### 5.1 配置 fail-closed

扩展 `Settings` 与应用启动校验，生产模式下必须满足：

- `BIDSCOPE_APP_MODE=production`。
- `BIDSCOPE_ADMIN_TOKEN` 非空，达到配置的最小长度，并且不等于示例、默认或占位值。
- `object_store_type=s3`，显式设置 endpoint、bucket、access key 和 secret key；不接受 ambient credentials 回退。
- `BIDSCOPE_REAL_MODEL_ENABLED=true` 时必须存在 model API key；key 不进入日志、错误、运行事件和审计 JSON。
- CORS 允许来源为显式配置；生产默认不允许通配符来源携带凭据。
- Trusted Host、外部 scheme 和反向代理头策略为显式配置，不能从任意请求头推断安全 URL。
- 生产 Compose 不使用演示用的 `minioadmin` 等默认凭据。

非生产模式可以保留 P0 的 demo/test 便利行为，但通过显式模式判断实现，不能由单个缺失 token 的配置悄悄降级为公开服务。

### 5.2 端点授权矩阵

| 区域 | 默认策略 | 说明 |
|---|---|---|
| `/healthz` | 公开 | 进程存活检查，不执行深依赖检查 |
| `/readyz` | 公开 | 返回每项依赖的 `ok`/`degraded`/`failed`，不返回内部细节 |
| `/assets/*` 与 SPA GET | 公开或由反向代理限制 | 不携带敏感数据 |
| `/api/runs/*`、`events/*`、`reports/*` | Admin Token | 查询、创建、确认、重试和下载均受保护；P1 不新增用户可见的取消端点 |
| `/api/subscriptions/*`、`inbox/*` | Admin Token | 订阅和通知操作受保护 |
| `/api/sources/*`、`evaluations/*` | Admin Token | 数据与评估信息受保护 |
| `/metrics` | Admin Token | bounded 指标，不公开业务标签 |
| `/api/test-controls/*` | 仅 `app_mode=test` 注册 + Test Control Token | 生产和 demo 返回 404 |

### 5.3 审计事件

新增 `audit_event` 持久化模型和迁移。事件字段保持有界：

- `id`、`occurred_at`、`event_type`、`outcome`。
- `request_id`、HTTP 方法、归一化路径。
- `run_id`、`subscription_id`、`report_id`、`snapshot_import_id` 等关联 ID。
- 操作来源摘要、错误代码和有限的安全元数据。
- 版本、模式和操作者标识使用单租户管理员上下文，不引入用户表。

审计分为两类：

- **关键变更审计：** 运行创建/确认/重试、订阅创建/暂停/恢复、快照导入和管理配置变更与业务事务同提交。P1 不新增用户可见的取消端点；内部任务取消保持运行时恢复实现细节。审计写入失败时事务失败并返回 bounded 错误，不能出现“操作成功但没有审计记录”。
- **观察类审计：** 读取、下载、健康检查等使用有界异步/同步记录；写入失败必须记录结构化错误和指标，但不阻断普通读取响应。

禁止写入：Admin Token、Authorization 头、模型 API key、Cookie/session、完整用户请求中的敏感字段、完整报告正文和任意原始请求头。

### 5.4 P1-A 验收

- 生产缺少必需密钥、使用默认密钥或 S3 配置不完整时启动失败。
- 未带 token、错误 token、过短 token和演示占位 token 均稳定得到 401/403。
- 受保护路由矩阵覆盖所有现有和新增 API 路由。
- CORS、Trusted Host、代理 scheme 和敏感字段脱敏有正向/负向测试。
- 关键变更与审计事件同事务；审计不含敏感信息。
- 快照来源白名单、目录穿越、synthetic URL 和 test-control 边界回归通过。

## 6. P1-B：可观测性与运行稳定性

### 6.1 请求上下文

增加请求中间件和上下文工具：

- 接受合法的 `X-Request-ID`，否则生成 UUID；拒绝过长、控制字符或不符合格式的值。
- 响应返回同一 `X-Request-ID`。
- 日志字段固定包含 `request_id`、HTTP 方法、归一化路径、状态码、耗时和异常类型。
- 业务日志按生命周期补充 `run_id`、`subscription_id`、`report_id`、`snapshot_import_id`。
- SSE 日志记录连接建立、断开、最后发送序号、持续时间和断开原因，不记录事件正文。

### 6.2 结构化日志与指标

生产日志使用 JSON 输出到 stdout/stderr，由 Docker 日志驱动负责收集；生产默认不输出 debug 内容。敏感字段通过统一的 allowlist 序列化，不能依赖调用方自行过滤。

指标语义固定为以下 bounded 集合：

- API 请求数、错误数和延迟分位数。
- 图运行按状态、节点、错误代码和重试次数统计。
- SSE 活跃连接数、连接持续时间和断开原因。
- scheduler tick 的 due/ran/skipped/failed 计数及最近成功时间。
- 快照导入成功/失败/警告、记录数和耗时。
- 报告持久化、DOCX 生成和对象存储交付耗时。
- PostgreSQL、checkpoint 和对象存储依赖失败计数。

初版提供受 Admin Token 保护的 `/metrics` 文本输出，并使用 bounded labels；不要求部署 Prometheus/Grafana 或维护新的常驻监控服务。结构化日志仍是没有指标采集器时的主要排障入口。

### 6.3 健康与就绪

- `/healthz` 只证明 API 进程能够响应。
- `/readyz` 在 bounded timeout 内检查：应用配置、主数据库 `SELECT 1`、checkpoint 连接/表可用性、对象存储 bucket 可访问性。
- `/readyz` 响应包括整体状态和各依赖的状态码，不返回连接字符串、bucket secret、异常 trace 或内部 host。
- API Compose healthcheck 使用 `/readyz`。
- scheduler 不伪装成 HTTP 服务；通过进程存活、最近 tick 时间、tick 失败日志和数据库 advisory lock 状态排障。超过配置阈值没有成功 tick 时输出告警级别日志。

### 6.4 稳定性参数

新增配置项并设置适合小团队的明确默认值：

- 异步数据库 pool size、max overflow、pool recycle、connect timeout 和 command timeout。
- S3 connect/read timeout、有限重试和最大对象大小。
- 最大并发运行数、单请求正文大小、SSE 连接上限和报告/导出大小上限。
- 优雅停机等待窗口和 scheduler tick 超时。

启动时输出经过脱敏的生效参数摘要。超出最大并发时返回可重试的 bounded 错误，并记录对应运行事件；不能无限排队或让请求无限挂起。

关闭流程为：停止接收新请求 -> scheduler 停止获取新 tick -> 等待或取消超时运行 -> 按当前 token fence 规则持久化可恢复状态 -> 关闭 checkpoint、数据库和对象存储资源。现有 ownership token、heartbeat 和 advisory lock 语义不变。

### 6.5 P1-B 验收

- 给定或自动生成的 request ID 可贯穿 API、SSE、运行和报告交付日志。
- 日志脱敏测试确认 token、API key、Cookie、Authorization 和报告正文不出现。
- 数据库、checkpoint、对象存储分别不可用时，`/readyz` 返回失败且无敏感信息泄露。
- 连接池、timeout、并发上限和优雅停机有单元与集成测试。
- 对象存储临时失败、scheduler tick 中断、运行任务取消和 ownership loss 可观测且可恢复。
- Docker smoke 能使用 `/readyz` 判断服务是否真正可接收业务请求。

## 7. P1-C：备份、恢复与发布运维

### 7.1 备份对象和格式

备份覆盖：

- PostgreSQL 业务表、运行事件、报告元数据、订阅、审计记录、evaluation 结果和 LangGraph checkpoint。
- 对象存储中的 snapshot payload、DOCX 报告及对象元数据。

使用显式、可验证的备份 manifest：

```json
{
  "backup_version": "p1-v1",
  "created_at": "...",
  "app_version": "...",
  "git_commit": "...",
  "migration_revisions": {"application": "...", "checkpoint": "..."},
  "database_dumps": {"application": {"path": "...", "sha256": "..."}},
  "objects": [{"key": "...", "size": 0, "sha256": "..."}],
  "counts": {"objects": 0},
  "retention_class": "daily"
}
```

数据库使用一致性 dump（计划采用 PostgreSQL custom-format `pg_dump`，恢复使用 `pg_restore`）；当应用库和 checkpoint 库指向同一个 PostgreSQL 数据库时只生成一份 dump 并在 manifest 中映射两个角色，指向不同数据库时分别生成 dump。对象使用 provider 无关的逐对象复制/归档。最终 manifest 和归档均计算 SHA-256。备份日志不得输出任何凭据。

### 7.2 运维 CLI

增加不由 HTTP 请求触发的运维命令：

```text
bidscope ops backup create
bidscope ops backup verify
bidscope ops backup list
bidscope ops backup prune
bidscope ops restore
```

流程要求：

1. 检查数据库、对象存储、应用版本和迁移状态。
2. 生成一致性数据库 dump。
3. 列出并复制对象存储内容，生成对象哈希和数量。
4. 写入 manifest 并验证每个文件可读、大小和哈希匹配。
5. 将备份保存到本地主机目录。
6. 仅当显式设置外部 S3 目标和开关时复制到外部 S3。
7. `prune` 按每日 7 份、每周 4 份的 retention class 清理，不删除最近一次可验证备份。

恢复必须显式指定目标目录和目标数据库/Compose 项目；默认恢复到空目标，不允许静默覆盖在线数据库或对象前缀。恢复完成后再次运行 manifest 校验和应用 smoke。

### 7.3 恢复演练与 RPO/RTO

恢复演练脚本必须能够在干净环境中：

1. 启动全新的 PostgreSQL、MinIO 和 API Compose。
2. 导入 synthetic snapshot，创建运行、报告、DOCX、订阅和审计记录。
3. 执行一次备份并记录时间点。
4. 删除数据库和对象卷，模拟主机数据损失。
5. 恢复数据库、对象和迁移状态。
6. 启动 API 与 scheduler。
7. 验证旧报告仍指向原始不可变证据版本，DOCX 可下载，订阅游标和审计记录存在，checkpoint 可继续执行新运行。
8. 记录恢复耗时和最新可恢复备份时间。

验收必须证明 RPO 不超过 24 小时、RTO 不超过 4 小时。演练结果保存为发布证据，而不是只在文档中声明目标。

### 7.4 发布与回滚

- 镜像、数据库迁移和前端静态资源带有可追踪的应用版本标识。
- 发布前执行 lint、mypy、后端 unit/contract/security、integration、frontend unit/build、deterministic evaluation、Docker smoke 和关键 E2E。
- 发布前执行备份并验证 manifest；迁移兼容检查通过后才运行升级。
- migration 只允许追加，不修改已应用脚本。
- 回滚优先回滚应用镜像；数据库不自动 downgrade。涉及破坏性 schema 变化时必须先采用向后兼容迁移，或阻止发布。
- 提供 Compose runbook：初始化、启动/停止、状态和日志、迁移、升级、回滚、备份、恢复、密钥轮换和 scheduler 检查。
- 提供 `.env.production.example` 和 secret 文件模板；真实凭据不得提交仓库。
- 发布门禁失败时不得生成或标记可发布版本。

### 7.5 P1-C 验收

- 新主机可按 runbook 完成初始化和启动。
- backup manifest 可独立验证，且本地与可选外部 S3 两种目标均有测试。
- 恢复后旧报告、原版本证据、DOCX、订阅、审计和 checkpoint 均可用。
- 恢复后新运行和 scheduler tick 可以执行。
- 至少完成一次主机数据损失恢复演练，并保存实际耗时和结果。
- 发布门禁、迁移兼容检查和备份前置条件都能阻断不安全发布。

## 8. 数据流与错误处理

关键变更的处理顺序为：

```text
鉴权与输入边界
  -> 业务事务
  -> 同事务关键审计
  -> 提交
  -> 结构化结果与指标
```

运行、订阅和报告中的现有错误代码继续作为对外稳定契约；P1 新增的配置、依赖和容量错误必须是 bounded 类型，不返回内部 traceback。依赖故障按以下规则处理：

- 启动配置错误：进程不启动。
- readiness 依赖错误：服务保持进程存活但不接收业务流量。
- 请求期临时依赖错误：返回可重试错误，写入 request/run 关联日志。
- 关键审计写入失败：业务事务回滚。
- 观察类日志/指标写入失败：不阻断业务，但产生告警。
- 备份校验失败：备份标记无效，不允许进入可恢复集合。
- 恢复校验失败：不启动被恢复的生产 Compose 项目，直到问题被修复。

## 9. 测试矩阵

| 层级 | 新增重点 |
|---|---|
| 单元 | 设置 fail-closed、token 校验、脱敏、request ID、readiness 状态映射、连接池/超时、manifest 哈希、保留策略 |
| 安全 | 路由授权矩阵、Host/CORS/代理头、默认凭据、日志/审计敏感信息、测试控制路由隔离 |
| 集成 | 审计同事务、依赖不可用、对象存储超时、并发上限、优雅停机、备份创建/验证/恢复 |
| E2E | token 输入与 401 处理、完整运行关联、报告/DOCX 下载、订阅 tick 和生产 readiness |
| 发布 smoke | production 配置启动、迁移、checkpoint、S3 bucket、`/readyz`、API 基本路径 |
| 恢复演练 | 干净 Compose、数据库和对象销毁、恢复、旧证据复现、新运行和 scheduler tick |

P0 的所有既有测试必须继续通过。P1 测试不得依赖真实招投标网站；真实快照验证如果未来获授权，应作为独立的 P2 评估，不改变 P0/P1 的离线安全边界。

## 10. 分阶段交付门禁

### P1-A 完成条件

生产模式 fail-closed、端点授权矩阵、审计模型和安全回归测试全部通过。

### P1-B 完成条件

结构化日志、request/run 关联、`/readyz`、`/metrics`、稳定性配置和故障恢复测试全部通过。

### P1-C 完成条件

备份/验证/恢复 CLI、Compose runbook、发布门禁和一次实际恢复演练全部通过，并提供 RPO/RTO 证据。

P1 总体验收要求三层均通过，且不降低 P0 的来源政策、证据闭环、幂等执行和恢复语义。
