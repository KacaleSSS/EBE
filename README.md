# EBE · Ebook Engine

**KacaleSSS** · 面向中文与英文 ebook 的资料采集、证据交叉核验、持久化撰写及离线出版工具。

默认不启用图片，不上传私人知识库，不捆绑任何 API key。交付形式为命令行、stdio MCP 和回环地址只读状态 API；可在自己的服务器上运行，经 SSH 使用。不是托管 SaaS，不含买家池。

## 安装

Python 3.11+：

```sh
python -m pip install "ebe-engine[pdf,images] @ git+https://github.com/KacaleSSS/EBE.git@v1.0.0"
ebe doctor
ebe --help
```

或从 GitHub Release 下载源码压缩包、解压，在目录运行 Windows `powershell -File install.ps1` / Linux `sh install.sh`。安装会联网下载依赖；安装完成后的 core 命令不访问互联网。离线机器可事先在同平台准备 wheelhouse，再用 `pip install --no-index --find-links=wheelhouse ebe-engine`。

核心无第三方 Python 依赖。PDF 解析需另外安装 Poppler (`pdftotext`)；图片与PDF导出需对应 extras。中文PDF需要合法中文 TTF 字体。Docker 镜像包含 Poppler，但不包含中文字体或语言模型权重。

多 Python 环境可用 `EBE_PYTHON` 指定3.11+解释器。安装器会拒绝复用版本过低的现有 `.venv`，不会自动删除它；改用新解压目录即可。

## 隐私模式与网络边界

| 进程 | 允许的数据与网络 |
| --- | --- |
| Core：检索本地库、验证、撰写、排版 | 默认仅回环通信；Docker `network_mode: none` 提供 OS 网络隔离 |
| Collector：搜索与下载 | 只接收显式查询或公开 URL 清单；仅挂载交换目录，不挂载知识库；公网地址校验及连接IP绑定 |
| Images：可选生成与视觉验证 | 显式 `--enable` 后输入本次 API key；只接收独立图片提示词和图片；单独交换目录 |

**云图片调用是隐私例外**：提供商能看到发送的提示词和图片。完全断网时请保持禁用。Python audit guard 是纵深防护，不能替代容器/操作系统沙箱；本地模型自身的网络行为也应由 OS 限制。使用云端 Codex/MCP 客户端会将工具返回内容发送给客户端，不属于严格隐私模式。

## 快速开始

```sh
ebe init ./runtime/projects my-book "你的主题" --mirror-root ./runtime/mirror
ebe status ./runtime/projects/my-book
ebe tools
```

镜像必须放在另一个持久存储设备才提供设备故障保护；同盘副本不是灾备。演示可显式使用 `--single-copy`，不代表有备份。

`ebe tools` 输出完整工具输入 schema。所有核心工具可用 JSON 文件调用：

```sh
ebe call plan_research plan.json
ebe call submit_search_results results.json
ebe request ./runtime/projects/my-book ./runtime/spool/requests.json
ebe fetch ./runtime/spool/requests.json ./runtime/spool/downloads --workers 4
ebe import ./runtime/projects/my-book ./runtime/spool/downloads/bundle.json
```

`plan.json` 例：

```json
{"project_dir":"./runtime/projects/my-book","providers":["codex"]}
```

这里的 `codex` 表示外部提交搜索结果的兼容协议，并不要求或自动连接 Codex。独立联网搜索：

```sh
ebe search "公开检索词" --provider openalex --output search-page.json
```

OpenAlex/Brave 凭据仅从 collector 进程的 `OPENALEX_API_KEY` / `BRAVE_SEARCH_API_KEY` 读取。查询本身会发给搜索服务，请避免个人身份或秘密。查询返回的 candidates 与 query_id、page、has_more 组合成 `submit_search_results` 参数；搜索摘要不计为证据。

采集包已完成部分可断点续跑，默认4并发、最多8、每起始域名2。下载不运行文献解析器；core离线导入时校验哈希、路径、大小、来源身份，再进入 `awaiting-review`。待审核文献不计入120篇门槛。

## 撰写与核验

- 保留 **120篇已审核独立内容**的底线，以及维度覆盖、高风险主张和饱和度门槛。
- 高风险/关键/快速变化主张全检；普通主张按分组确定性抽样20%，每组至少1条。
- 每条最多3个来源包；同研究、转载和同发布机构保守归并；精确引用、矛盾、未检项都保留。
- `review-plan` 用 UTF-8 字节限制审查包开销，是保守预算代理，不是假装精确 token 统计。模型提示词和输出另计。

```sh
ebe review-plan ./runtime/projects/my-book claims.json review-plan.json --budget 12000 --model-version local-model-version
ebe check-review packet.json review-result.json
ebe review-run ./runtime/projects/my-book claims.json --base-url http://127.0.0.1:8080/v1 --model your-local-model
ebe call start_pipeline start.json
ebe step ./runtime/projects/my-book --base-url http://127.0.0.1:8080/v1 --model your-local-model
```

`start.json`：`{"project_dir":"./runtime/projects/my-book","chapter_count":10,"auto_approve":true}`。
本地兼容 OpenAI 的语言模型需要事先安装、下载并运行；EBE 不附带模型权重，也不把远程模型当离线模型。每次 `step` 处理一个有ticket的持久化任务；研究阶段返回待执行动作，不伪造已完成的搜索、审核和饱和度。自动 gate 仍受证据检查限制。更完整协议见 [工作流](docs/WORKFLOW.md)。

来源判读、主题价值与文稿语义质量依赖本地模型或可信审阅者；机械引用校验不是事实真伪证明。不同模型能力与硬件会影响结果。

## 可选插图与导出

```sh
ebe images image-prompts.json ./runtime/image-exchange/assets --enable --vision-model YOUR_DOUBAO_VISION_MODEL
ebe insert-images draft.md ./runtime/image-exchange/assets illustrated.md
ebe export illustrated.md book.epub --title "书名" --assets-dir ./runtime/image-exchange/assets
ebe export draft.md book.pdf --title "书名" --font /path/to/chinese-font.ttf
```

默认生成模型为 Doubao Seedream 3.0；视觉核验必须指定独立的视觉模型，Seedream不是视觉理解模型。其他兼容服务可配置 `--base-url`、`--generation-model`、`--vision-model`，命令交互输入 key，不写入配置/项目/日志。详见 [图片配置](docs/IMAGES.md)。

HTML/EPUB离线导出，PDF为可选依赖。HTML内容转义，不执行原文脚本；图片嵌入前检查路径和文件，字体缺字则停止而非交付方框。

## OS 隔离运行

```sh
docker compose build core
# Linux bind目录需要由uid10001可写，按docs/SECURITY.md准备
docker compose run --rm core doctor
docker compose run --rm core init /data/projects my-book "主题" --mirror-root /data/mirror
docker compose run --rm core request /data/projects/my-book /spool/requests.json
docker compose --profile collect run --rm collector
docker compose run --rm core import /data/projects/my-book /spool/downloads/bundle.json
```

Core 无网络，仅 collector 有公网出口。镜像代理不读取用户库，图片服务默认 profile 不启用。不要给容器 Docker socket、host 网络、特权模式或主机目录根挂载。严格模式运行本地模型时需把模型与core放在同一个无网络环境/网络命名空间内；默认 compose 不替用户下载或启动模型。

## 状态 API 与开发

`ebe serve ./runtime/projects` 只监听127.0.0.1:8765，交互输入至少32字符访问令牌。`GET /health`、`GET /status?project=my-book` 都需要 `Authorization: Bearer ...`；无远程命令执行、无公开绑定、无遥测。远程使用 SSH 隧道。

```sh
python -m pip install '.[test]'
python -m pytest -q
```

见 [安全边界](docs/SECURITY.md)、[证据策略](docs/EVIDENCE.md)、[验证记录](docs/VALIDATION.md)。MIT License。请自行确认文献使用权与各 API 服务条款，不绕过登录或付费墙。
