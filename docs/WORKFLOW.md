# 从主题到ebook：EBE运行协议

## 三个独立阶段

**联网发现/采集**：显式公开查询 → 候选去重 → URL清单 → 并发下载、内容寻址缓存。

**离线研究/撰写**：包校验 → 解析入库 → source review → 主张台账与稀疏核验 → UVZ → 章节计划 → 每章正文与连续性记录 → QA。

**离线交付**：可选的独立云图片任务返回已验证图片 → 标记插入 → EPUB/HTML/PDF导出。

## 检索示例

先 `ebe call plan_research examples/plan.json`，将返回的query_id填入候选提交文件：

```json
{
  "project_dir":"./runtime/projects/my-book",
  "query_id":"由plan_research返回的ID",
  "page":0,
  "has_more":false,
  "candidates":[{"url":"https://www.python.org/doc/","title":"Python documentation","publisher":"Python Software Foundation","source_type":"primary-research"}]
}
```

提交后执行 request/fetch/import。每次导出新的请求清单路径；同一清单对应同一采集输出目录，恢复可重用。已下载内容哈希正确时不重新下载；失败保留可见状态，用户可再次发起采集。单采集包最多100项，每文件15MiB；离线导入一批累计64MiB，超出应拆批，不设资料总大小门槛。

## 文献审核

用 `read_source_body` 或本地检索得到正文；对每份来源通过 `review_source` 提交relevant、evidence_role、quality_grade、rationale和至少30字符的逐字 supporting_excerpt。只有审核合格内容计数。不能以搜索摘要代替正文。

`ebe review-plan` 接收主张数组：每条包含id、text、risk及source_ids；高风险全检，普通分组抽样，相关正文按有界窗口进入审查包。模型版本、策略和原文哈希绑定缓存；源码变化会失效。详见EVIDENCE.md。

## 状态与模型

`start_pipeline` 创建持久化任务，`run_topic_pipeline` 返回待处理ticket和上下文；`step`通过本地模型执行一个非研究任务并提交。研究阶段由独立采集流程完成，模型不得凭空宣称搜过资料。缺少模型、资料或门槛未过均是明确的未完成状态。

研究报告包含coverage、high_impact_claims及最近两批真实batch_id；批次ID可在 `research_status` 返回值中查看。饱和度应来自实际主张台账比较，不得凭来源数伪造。

章节任务写入版本文件，章节引用、术语与连续性记录都保存在项目。自动审批参数只决定是否等待人工按钮，不能绕过120篇、证据覆盖或QA。

## 导出约束

`export` 是排版工具，不自动给稿件质量背书；使用已完成QA的稿件。MCP提供assemble_ebook和quality_check，保留全文来源编号。

代码示例永不自动执行。对书中代码需要单独受控测试环境，不能运行从文献或模型直接给出的命令。

## 安装完成不等于模型就绪

本地模型的权重、硬件和运行时由使用者准备。软件本身安装即可运行状态、知识库、采集、验证规划与排版；语义分析/撰写仍需本地模型或可信编辑者。软件不购买API额度，不自动下载数GB模型，不把占位文本作为完成稿。
