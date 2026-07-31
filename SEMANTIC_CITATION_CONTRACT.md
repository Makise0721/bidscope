# Semantic Citation Contract

语义引用契约定义确定性引用校验层与语义校验器（Verifier）之间的输入、
状态和输出边界。它验证的是“一个 Claim 是否被可追溯的证据支持”，不是
公告在客观世界中是否真实。

## 1. 分层与前置条件

所有进入语义校验的 Claim 必须先通过 `validate_claim`。确定性校验失败时，
不得调用 Verifier。

### 当前 `validate_claim` 实际保证的内容

- 每个 `citation_id` 都能解析到一条 `NoticeEvidence`。
- Claim 的 `notice_version_id` 与每条被引用证据的 `notice_version_id` 一致。
- `span_hash == sha256(evidence.text)`，因此当前 `evidence.text` 未在写入后被修改。
- `start`、`end` 在当前 `evidence.text` 的坐标系内合法。
- Claim 至少有一条引用。

### 当前 `validate_claim` 不保证的内容

- `evidence.text` 确实是原始公告中对应位置的文本。
- `start`、`end` 是相对于完整原始公告的合法偏移。
- Claim 与被引用 Evidence 存在语义或逻辑支撑关系。

原文绑定是采集/证据准入层的独立职责：该层需要保存不可变原文快照，
记录来源、版本和内容哈希，并用完整原文重新校验 evidence 的文本与定位。

例如，证据为“项目地点：北京市海淀区”，Claim 为“项目预算 680 万元”时，
底层校验可以通过；语义关系必须由 Verifier 判断。

## 2. Verifier 输入边界

Verifier 只能使用该 Claim 显式携带的、已通过确定性校验的 Evidence 集合。
它不得主动读取同一公告的其他文本，不得调用外部知识库，也不得使用互联网
常识。

“断章取义”只能在调用方已把足以判断限定条件的上下文作为独立 Evidence
显式提供并加入 `citation_ids` 时判定。若未提供该上下文，Verifier 必须将
无法确认的情况判为 `UNCERTAIN`，而不能猜测原文还有何种限定。

因此，调用方如需校验可能受上下文影响的 Claim，必须先将相邻段落、限定语
或例外条款提取为具备独立 ID 和定位的 Evidence；这些 Evidence 与直接引用
一样进入 Claim 的引用集合和审计链路。

## 3. 判定状态

```python
class ClaimSupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
```

判定的对象是“当前引用证据集合与当前 Claim 的关系”，不是 Claim 的绝对真伪。

### `SUPPORTED`

引用证据集合充分、合理地蕴含 Claim：允许同义转述和不改变事实强度的合理
概括；多条证据可以联合支撑，但推理链必须完整且自洽。

### `UNSUPPORTED`

引用证据集合包含可明确确认、且与 Claim 关键事实不相容的内容。例如，证据
明确写“预算金额：500 万元”，Claim 写“项目预算为 680 万元”。

`UNSUPPORTED` 表示当前证据对 Claim 构成明确反驳；不以“可能还存在未引用
证据”为由回避已经存在的冲突。

### `UNCERTAIN`

引用证据既不能充分蕴含 Claim，也没有可明确确认的关键事实冲突。包括：

- 引用证据与 Claim 无关或只涉及相邻字段；
- 缺少支持结论所需的关键事实；
- 证据存在合理的多种解读；
- 未提供判断限定条件所需的上下文。

### 状态优先级

1. 存在明确关键事实冲突时，输出 `UNSUPPORTED`。
2. 无明确冲突且证据充分支撑时，输出 `SUPPORTED`。
3. 其余情况输出 `UNCERTAIN`。

## 4. Verifier 输出接口

每次判定必须输出以下字段，以便调试、审计和人工复核：

```python
class ClaimSupportVerification(BaseModel):
    status: ClaimSupportStatus
    rationale: str
    evidence_ids_used: list[str]
    conflict_evidence_ids: list[str]
    verifier_version: str
```

- `rationale`：只基于本次输入 Evidence 写出的简短理由，不得引入外部事实。
- `evidence_ids_used`：实际参与判定的引用 ID 子集。
- `conflict_evidence_ids`：与 Claim 有明确关键事实冲突的 Evidence ID；非
  `UNSUPPORTED` 时必须为空列表。
- `verifier_version`：模型标识与 Prompt 版本号，例如 `gpt-4o-v1.2`。

## 5. 报告聚合策略

| 状态 | 主情报列表 | 审计/复核 | 下游语义 |
| --- | --- | --- | --- |
| `SUPPORTED` | 默认展示，并显示证据溯源 | 保留完整判定记录 | 可作为系统验证通过、可追溯的情报使用 |
| `UNSUPPORTED` | 不展示 | 记录失败原因和冲突证据 | 禁止作为可信情报输出 |
| `UNCERTAIN` | 保守策略下不展示 | 进入复核队列 | 不视作有效可信结论 |

调研模式可以展示 `UNCERTAIN`，但必须与 `SUPPORTED` 分区并带有显著“存疑”
标识；它不得被下游当成已验证情报。系统不自动将 `UNCERTAIN` 升级为
`SUPPORTED`。

## 6. 非目标

- 不裁决公告原文在客观世界中的事实真伪。
- 不消除自然语言和公告文本本身的固有歧义。
- 不保证 LLM Verifier 零误判；系统保留模型版本、理由和证据 ID 以支持审计及
  人工复核。
- 在未显式提供可审计上下文时，不承诺识别跨片段的断章取义或隐含限定条件。

## 7. 判定样例

### SUPPORTED

- Evidence：`预算金额：680 万元。`
- Claim：`项目预算为 680 万元。`
- 输出：`SUPPORTED`。证据明确记载了相同预算金额。

### UNSUPPORTED

- Evidence：`预算金额：500 万元。`
- Claim：`项目预算为 680 万元。`
- 输出：`UNSUPPORTED`，`conflict_evidence_ids=["ev-0"]`。金额直接冲突。

### UNCERTAIN：信息不足

- Evidence：`项目资金来源为财政资金。`
- Claim：`项目预算为 680 万元。`
- 输出：`UNCERTAIN`。证据没有预算金额，也没有与该金额直接冲突的内容。

### UNCERTAIN：缺少上下文

- Evidence：`本项目预算为 680 万元。`
- Claim：`项目预算确定为 680 万元。`
- 输出：`UNCERTAIN`。若未显式提供后续的“暂估金额”或“以批复为准”等
  上下文，Verifier 无法确认“确定”这一限定。
