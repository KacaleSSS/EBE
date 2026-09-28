# EBE 严格研究发布门槛

## 安装与边界

所有 EBE CLI 必须在 core 加载 engine 后、任何 readiness/阶段推断/发布调用前执行：

```python
from ebe.readiness import install
install(engine)
```

安装对同一 engine 幂等，返回 engine。模块导入无副作用；直接调用未安装的 legacy engine 保留兼容行为。`ebe/cli.py` 的 `core_setup()` 在 `modules()` 后调用 `install(engine)`；其他使用发布门槛的入口也必须先安装。

包装结果保留旧字段，新增 `legacy_readiness` 和 `strict_readiness`；顶层 `failures` 合并，`pass` 是两层结果的严格 AND。旧统计仍代表旧算法，严格计数读取 `strict_readiness.qualified_sources/coverage`。结构错误、缺少 acquisition_batches、无正文等都不能放行。

## 来源和独立性

至少 120 个标准化内容唯一的 ingested 来源。每个来源必须有 `metadata_json.evidence_review`，其中 `relevant` 必须是 JSON true，`grade` 为 strong、usable 或既有 usable-with-limits，`excerpt` 去除首尾空白后至少 30 字符且原样出现在正文中。ingest_file 的 ingested 状态本身不构成审核。正文通过连接的 `PRAGMA database_list` 中 main 数据库路径定位项目根，读取相对 `text_path`；拒绝逃逸项目的路径，不依赖当前工作目录，也不使用 metadata 中自报正文替代文件。无文件数据库无法满足本门槛。

正文 NFKC、casefold、折叠空白后计算 SHA-256。每维度至少三个独立 provenance groups，每条 high_impact_claim 至少两个。相同 DOI（去除 doi URL/prefix）、标准化正文 hash、owner/publisher 或 hostname 通过并查集传递归并；未通过审核的桥接来源也参加归并。无机构且无 hostname 的来源保守合为未知组。机构名称不能证明实际所有权；hostname 子域归并采用保守后缀启发式，可能合并实际独立来源，不是完整 PSL 或法人核验。

high、critical、key 风险需要至少一个被声明为 authoritative 且已合格审核的引用来源，其 review.role 为 primary，published_at 为 ISO YYYY-MM-DD 且距离当前 UTC 日期 0–365 天。rapidly-changing 最多 90 天。未来日期、无效日期不通过；检索日期不能替代发布日期。不得通过配置自动降低门槛。

## 饱和批次

报告 `recent_batches` 最后两项必须各自引用数据库按 batch_id 排序的最近两条 acquisition_batches，不能重复或杜撰，source_ids 集合必须与对应记录完全一致。每批至少 20 条合格内容唯一来源，两批标准化正文不能重叠。

每批必须显式提供 `new_material_claims`、`material_claims_before`、`uvz_changed`、`promise_changed`：均为非 bool、非负整数；前序主张数必须大于零，新增比率至多 5%，两个 changed 字段都必须为整数 0。字段缺失绝不能当作零或稳定；JSON false 也不是整数 0。

真实批次关联只证明数据谱系一致，**自报 new_material_claims 仍不能证明 semantic truth 或研究已经语义饱和**。本模块始终报告 semantic_truth_verified=false。元数据、数据库和审核者身份的写入权限仍信任本地操作者；不防止有权限操作者伪造审核或批次。存在并发写入时，调用方应在同一读取事务中完成检查和发布决策，避免两次读取之间状态变化。

## 稀疏审核与发布门槛

`required_reviews_readiness(plan, results, validator)` 接收当前重新生成的 evidence.build_review_plan 结果、审核结果列表和 evidence.validate_review 函数。全部 required=true 或 selected=true 的 claims 都必须有与当前 packet/cache_key 绑定且 machine_valid=true、review_status=support、needs_escalation=false 的结果；缺失、延后、冲突、不足、重复结果或陈旧缓存均失败。普通抽样不能以非 required 为由忽略审核失败。两类均为空时为空集通过。

`install(engine)` 现已自动接入 cross-review，结果位于 strict_readiness.cross_review。ebe/gates.py 读取 run_review 保存的最新 cross-review artifact 的 metadata（plan、results、reviewer），不相信其中自报的 required_readiness/pass。主张从当前 saturation-report.high_impact_claims 读取，必须有 text 或 claim 正文；普通主张可放在同一报告的 claims 数组，ID 不得与高影响条目重复。当前来源正文、审核资格和完整主张 ledger 用于重建 fresh plan；整个计划必须与保存计划一致，然后重验 saved results。高影响列表中的每个主张必须通过，即使风险标签写成 normal 也不能漏审。

source hash、主张内容、模型 binding、策略版本、服务版本或提示词变化会使旧审核失效。可在 project.json 配置 `cross_review_reviewer: {"model": "模型不可变版本", "endpoint": "http://127.0.0.1:端口/v1/chat/completions"}` 固定当前审核模型；配置与报告不符即阻断。未配置时最新报告声明的模型是当前模型依据，无法感知外部服务在同一模型别名后偷偷替换权重。机器引文匹配只检查结构和绑定，不证明陈述真实。

## Final gate 与当前稿件绑定

安装同时幂等包装 quality_check 和 record_gate；未安装的 legacy engine 不变。QA 前后核对当前 assembled ebook、最新 ebook artifact 字节及 SHA-256、全部最新章节与引用清单。组装正文必须等于这些章节和当前来源参考文献的精确重建结果；拒绝旧章节回退、遗漏章节、引用不一致、正文篡改与未重新组装的稿件。

生成的严格 QA artifact 保存 binding：ebook_sha256、ebook_artifact_id、chapters_digest、sources_digest、saturation_digest、cross_review_digest。提交 final 时必须引用最新且绑定当前输入的 QA；即使 metadata.pass=true 也会再次实际执行 quality_check。通过后在 BEGIN IMMEDIATE 事务内重验输入、研究与交叉审核、前置 gates，再写 final decision。返回的 artifact_id 指向此次新 QA，requested_artifact_id 保留原申请的 QA ID。过期或无绑定的旧 QA 不能获批。

SQLite 事务串行化最后决策阶段的数据库写入，并在落决策前再次检查文件。任意非协作文件写入者仍属于可信本地操作者；本实现不声称能原子锁定文件系统或证明审核语义真实。

## Claim 与审核结果示例

### 可读 claim 输入示例

以下是合成示例，不是已验证的事实。传给 `build_review_plan(records, claims)` 的 `claims` 是数组：

```json
[
  {
    "id": "C-storage-001",
    "text": "在相同测试条件下，方案 A 的恢复时间低于方案 B。",
    "risk": "critical",
    "source_ids": ["S0001", "S0002"],
    "group": "恢复能力"
  }
]
```

`id`、`text`、`risk` 为必填非空字符串，`id` 不可重复；`source_ids` 为必填字符串数组，指向 records 中真实来源。未知来源会记入计划的 missing_source_ids，不会自动生成证据。`group` 可省略，默认 risk，用于普通风险的分组抽样。critical/key/high/rapidly-changing 全部 required；medium/normal/low 按风险及 group 分组抽取向上取整的 20%；未知风险拒绝。完整计划必须保留未抽中、证据不足和预算延后的 claim，不能只传有 packet 的子集。

发布报告中的 `high_impact_claims` 使用另一个契约：标识字段为 `claim_id`，不是 `id`；必须有 `text` 或 `claim` 字符串供 fresh plan 重建。上例对应的报告条目可以是：

```json
{
  "claim_id": "C-storage-001",
  "text": "在相同测试条件下，方案 A 的恢复时间低于方案 B。",
  "source_ids": ["S0001", "S0002"],
  "risk": "critical",
  "authoritative_source_ids": ["S0001"]
}
```

这仅是报告的一条主张，不足以通过完整发布门槛；仍需全部维度、120 条合格内容和两条真实最近批次。authoritative_source_ids 只是待检查的声明，S0001 仍须满足已审 primary 和发布日期要求。

### 审核者 result 输入契约

`validate_review(packet, result)` 的 result 是单个对象；`required_reviews_readiness(plan, results, validate_review)` 的 results 是这些原始对象组成的数组，不能传 validator 输出的 verdict 代替。

```json
{
  "claim_id": "C-storage-001",
  "cache_key": "从当前 packet.cache_key 原样复制，勿使用此占位文字",
  "status": "support",
  "citations": [
    {"source_id": "S0001", "quote": "方案 A 恢复耗时 8 分钟，方案 B 为 15 分钟。", "stance": "support"},
    {"source_id": "S0002", "quote": "同一测试环境中，A 的恢复耗时小于 B。", "stance": "support"}
  ]
}
```

示例 quote 只有原样存在于实际 packet 摘录时才有效；不得直接用示例文字充当真实引用。

| result 字段 | 实际校验 |
| --- | --- |
| claim_id | 必须等于当前 packet.claim.id |
| cache_key | 必须等于当前 packet 的规范 JSON 内容哈希；packet 自身的 cache_key 也会重算核对 |
| status | 必填，严格取 support、conflict、insufficient 之一 |
| citations | 必填对象数组；support 至少需要两个独立来源的有效支持引用，并非仅两条引用 |
| citations[].source_id | 非空字符串，必须同时属于 packet.sources 与 claim.source_ids |
| citations[].quote | 非空字符串，必须是该来源某个 excerpts[].text 窗口的逐字子串；不能改写或跨窗口拼接 |
| citations[].stance | 必填，严格取 support、conflict、insufficient 之一 |

出现 conflict/insufficient 引用时不能声明整体 support；conflict 必须有逐字冲突引用。引用不能证明相关语义成立。当前审核范围是摘录，不是全文；cache_key 是内容绑定，不是审核者签名。额外字段不作为放行依据，手写 `verified: true` 或 `pass: true` 无效。

### 返回字段契约：不要混淆三层结果

| 返回接口 | 字段及含义 |
| --- | --- |
| evidence.validate_review | machine_valid: bool，绑定、引文和结构是否有效；review_status: support/conflict/insufficient；needs_escalation: bool，有错误或非 support 时为 true；errors: 字符串数组 |
| evidence.validate_review 固定字段 | verified=false、status="unverified"、review_scope="excerpts_only"、full_document_review=false；即使 support 也保持这些值，没有 pass 字段 |
| required_reviews_readiness | pass: bool，所有 required 或 selected 主张是否结构性支持；failures: 字符串数组；semantic_truth_verified=false。不检查既非 required 又未抽中的主张 |
| 安装后的 engine readiness | pass: bool，旧门槛与严格门槛 AND；failures: 合并错误数组；legacy_readiness: 原结果；strict_readiness: 严格结果；其余旧统计字段保留旧含义 |
| 正常完成的 strict_readiness | pass、failures、qualified_sources: 标准化正文唯一合格数、coverage: 维度到独立组数、provenance_groups: 来源 ID 到组代表 ID、semantic_truth_verified=false；安装后的 wrapper 另附 cross_review 复核结果 |
| wrapper 捕获严格检查异常时 | strict_readiness 仅保证 pass=false 和 failures；其余统计与 semantic_truth_verified 字段可能缺失，缺失不得解释为已验证 |

组代表 ID 是当前集合并查集产生的来源 ID，不是跨数据版本稳定的机构标识。安装后 research_result.pass 已包含 fresh cross-review；不使用 verdict.status 或 verdict.verified 判断通过。只有直接使用独立 API 的调用者才需自行合并门槛。

## 离线验收

在 EBE 目录使用测试环境执行：

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests/test_readiness.py tests/test_review_service.py tests/test_evidence.py -q
.\.venv-test\Scripts\python.exe -m pytest tests/test_readiness.py::FinalGateTest::test_strict_end_to_end -q
```

测试使用临时 SQLite、合成正文及确定性模型 mock，无真实项目或联网调用。覆盖安装幂等、legacy 隔离、来源审核与独立性、真实批次、fresh cross-review、版本变化、普通抽样不足升级、伪造 final pass/binding、旧 QA、来源/稿件/参考文献变化与 missing review。

完整正向验收 `FinalGateTest.test_strict_end_to_end` 覆盖安装 strict 后的 120 个合格唯一来源、120 个 publisher provenance groups、2 条真实批次、完整当前 claim ledger、真实 run_review 计划/保存流程（仅模型生成 mock）、1 章完整组装稿、严格 QA 到 final approval。测试断言最终 decision 引用重新生成的 QA，其 ebook_sha256 等于当前输出文件哈希。该测试在 `.venv-test` 中通过：`1 passed in 1.30s`。模型 smoke 与完整 gate 验收是不同层次的验证，不能相互替代。

接口依据：`ebe/legacy/engine.py` 的研究、QA、组装与审批接口，`ebe/legacy/research.py` 的 acquisition_batches 和 review_source，以及 `ebe/review_service.py` 的 run_review metadata 契约。严格门槛实现位于 `ebe/readiness.py` 和 `ebe/gates.py`，回归测试位于 `tests/test_readiness.py`。发布判定通过当前输入重建、结果重验及稿件哈希绑定完成，不接受可提交的 pass 声明替代检查。
