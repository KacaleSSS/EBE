# v1.0.0 验证记录

## 可复现检查

冻结代码本地回归：Windows / Python 3.12 为 **290 passed、2 skipped、481 subtests passed**；Linux / Python 3.11 为 **291 passed、1 skipped、481 subtests passed**。CI 另覆盖 Python 3.11/3.12/3.13 与 Docker 构建，实际状态见 Actions，不能以本地结果代称 CI 已通过。

```sh
python -m pip install '.[test]'
python -m pytest -q
ebe doctor
```

测试使用合成正文、图片、下载回执与本地临时项目，不包含用户原书、私密知识库或密钥。网络安全单测使用内存 HTTP 数据和 mock，不访问测试中示例地址。

覆盖：采集并发/单写入器/恢复、包完整性、路径与网络限制、来源独立性归并、核验预算与缓存、真实120条合成来源到 cross-review/ebook/QA/final gate、旧报告失效、图片生成及视觉协议 mock、插图到 HTML/EPUB、PDF 字体缺字与减号、只读状态 API、CLI/MCP。

Linux 未安装支持中文的 TTF 时，中文 PDF 布局测试显式跳过；Windows 缺少创建 symlink 权限时，对应实际 symlink 测试跳过，同时有 mock 拦截覆盖。不能把跳过称为通过。

## 真实接口烟测

- 用户明确授权后，指定并收到模型标识 `deepseek-v4-flash`。
- 两次实际请求：合成双来源交叉核验、带来源编号的中文短文撰写，协议检查均通过。原文匹配是机械验证，不是全书语义质量证明。
- 仅使用合成公开风格数据，没有上传用户书稿、私人资料或本地文件。测试 key 未写入仓库、配置和日志。
- 结果见 [live-validation.json](live-validation.json)。usage 为提供商原样报告，其中分项与 total 可能不一致；不将其解释为精确账单或货币金额。
- 此次云请求是获准测试例外；生产 core 仍拒绝远程模型地址。本次没有真实调用 Doubao 生图或视觉模型，相关模块仅完成离线协议及图片处理测试。
- 公共网页 `https://www.python.org/` 的真实下载成功，用于验证 HTTP 连接关闭及压缩正文处理，不代表各种站点均可采集。

## 隔离与性能结论的范围

临时 Linux 容器以 `--network none` 运行时，原生 socket 无法连接公网。目标 Dockerfile 的构建和隔离 smoke 由仓库 CI 单独检查；若构建服务不可达，应记作外部验证未完成，不能替代为构建通过。

并发单测对8份合成响应注入每份100ms等待，检查串行峰值1和四并发峰值4。耗时比仅为该夹具结果，不承诺真实网络固定倍数提速；实际速度受站点限流、带宽、文档解析和存储影响。

## 使用前仍需准备

本地模型运行时、权重与硬件，中文 PDF 的合法 TTF，独立备份设备，以及启用图片时的用户密钥。自动化门槛不代替具体领域的专业责任。
