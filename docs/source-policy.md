# BidScope 数据来源与取证政策

**版本：** 2026-07-19
**适用范围：** BidScope P0 快照导入与检索层
**设计依据：** `docs/superpowers/specs/2026-07-18-bidscope-design.md` 第 6、10 节

---

## 1. 核心原则：P0 是 snapshot-only

BidScope P0 **不**实时抓取、爬取、探测任何招投标网站。所有检索与报告都在已经导入、版本化、不可变的数据上进行。

- Web 查询路径不访问公开招投标站点。
- 后台调度器（APScheduler）不访问公开招投标站点。
- 快照的取得必须是显式的 CLI 或管理动作，或由单独授权的流程完成。
- 任何未来的实时抓取适配器都需要单独的书面授权、明确的 HTTPS 白单，并不得削弱 P0 快照路径。

本政策是合同，不是注释。违反本政策的实现不得进入 P0 验收。

---

## 2. 两个官方入口（已核验）

P0 只使用下述两个入口的公开字段构造**人工核验公开摘录**（`curated_public_excerpt`）：

| 来源 | 官方入口 host | 已核验代表 URL |
|---|---|---|
| 中国政府采购网（CCGP） | `www.ccgp.gov.cn`、`search.ccgp.gov.cn` | `https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/t20260718_26961813.htm` |
| 全国公共资源交易平台（GGZY） | `www.ggzy.gov.cn` | 外层：`https://www.ggzy.gov.cn/information/deal/html/a/530000/0101/20260718/005322a8028794fe4b0f97111b6482009bc5.html`<br>子页：`https://www.ggzy.gov.cn/information/deal/html/b/530000/0101/20260718/005322a8028794fe4b0f97111b6482009bc5.html` |

### 2.1 host 白名单（不可由配置扩大）

- CCGP **只允许** `www.ccgp.gov.cn` 与 `search.ccgp.gov.cn`。
- GGZY **只允许** `www.ggzy.gov.cn`。
- 把 CCGP 数据归到 ggzy.gov.cn，或反过来，都被 provenance 校验拒绝。

任何其它 host —— 包括 lookalike、user-info host、redirect 派生 host —— 都被 manifest 校验与 `NormalizedNotice` 的 provenance 校验双重拒绝。

---

## 3. 为什么不是实时抓取（研究期间观察到的事实）

### 3.1 CCGP：WAF / 403 / 频繁访问

研究期间，CCGP 曾返回 Web 应用防火墙响应，包括 HTTP 403 与"频繁访问"提示。继续自动化请求会触发封禁，且没有证据表明存在文档化、版本化的公开 API 或稳定 schema。浏览器可访问 ≠ 可自动化采集。

### 3.2 GGZY：未文档化 POST / 验证码 / 结果上限 / 聚合来源风险

GGZY 的 Vue 页面曾暴露出未文档化的 Web POST 接口，但包含验证码与反自动化错误码、结果上限、以及异构的上游数据。其结构不适合可靠的自动化采集，且 robots 许可不可依赖。

### 3.3 robots 许可未知

两个来源都没有可依赖的 robots 指令或自动化访问授权。公开可见性因此不被视为自动化采集许可。

---

## 4. 三种证据等级（表现要求）

| `capture_kind` | 含义 | URL 要求 | 展示要求 |
|---|---|---|---|
| `raw_response` | 获得授权或合法人工取得的**真实原始响应** | 官方 HTTPS 白名单 | 标注"原始响应"、抓取时间、hash |
| `curated_public_excerpt` | 公开字段经**人工核验后的摘录**，**不是**原始响应 | 官方 HTTPS 白名单 | 标注"公开摘录"，**不得**称为原文快照 |
| `synthetic_demo` | 为主演示、增量、故障、E2E 构造的**明确合成数据** | **仅** `https://example.invalid/` | 每条记录**持续**显示"合成演示数据" |

### 4.1 禁止事项

- **不得**把自造公告写成 `curated_public_excerpt`。
- **不得**让 `synthetic_demo` 使用 `ccgp.gov.cn` 或 `ggzy.gov.cn` URL。
- **不得**让合成数据的 `source` 冒充官方来源（始终是 `synthetic_demo`，ID 以 `demo-` 开头）。
- **不得**让 `.invalid` URL 变成可点击外链。

实现由 `bidscope.domain.provenance.validate_provenance`（官方 host 精确映射、synthetic 前缀与 host）与 `bidscope.snapshots.adapters.inspect_bundle`（manifest 哈希、文件类型、目录穿越、未声明文件）双重保证。

---

## 5. P0 不执行的操作

- 不执行实时抓取。
- 不执行附件批量下载。
- 不执行登录或付费墙后访问。
- 不绕过验证码、WAF、访问控制或反自动化措施。
- 不访问除第 2 节与第 4 节以外的任何网站。

---

## 6. 取证与不可变保证

- `raw_response`：真实原始响应（仅由单独授权流程取得）。
- `curated_public_excerpt`：人工核验的公开字段摘录，保留核验 URL 与抓取尝试结果（`retrieval_outcome`），**不得**声称是原始响应。
- `synthetic_demo`：保留明确的合成标识。
- 原始公告版本与证据**永不覆盖**。
- manifest 中所有 payload hash 都按实际字节计算，任何篡改都会被 `inspect_bundle` 的 SHA-256 校验捕获。
- bundle **不得**保存 cookie、session、验证码 token、验证码图片或下载附件。

---

## 7. 未来实时抓取的门槛

任何未来的实时适配器必须：

1. 具备单独成文的授权与来源合同；
2. 使用配置化的 HTTPS host 白名单；
3. 在认证、验证码、限流、访问拒绝处**立即停止**；
4. 不得削弱 P0 快照路径或本政策的任何条款。

在满足上述门槛之前，BidScope 的所有回答都受本政策约束。
