# 可选云图片模块

此模块只接受用户单独提供的图片 prompt 清单，不读取整书、Markdown 原文、知识库或环境变量中的凭据。默认不联网、不创建目录。没有 CLI/UI 自动接入，不会自动插入图片。

## 调用

```python
from getpass import getpass
from ebe.images import generate_images, insert_images
from ebe.export import export_book

manifest = [{"slug": "forest", "prompt": "一片绿色森林，不包含文字", "alt": "绿色森林"}]
config = {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "generation_model": "doubao-seedream-3-0-t2i-250415",
    "vision_model": "用户填写实际支持图片输入的独立视觉模型或部署 ID",
    "allow_image_hosts": [],
    "timeout": 60,
}
# 必须先向用户说明：prompt、生成图片将发送到配置的云服务，可能收费。
# 仅在用户明确同意本次调用后传 enabled=True；默认 False。
key = getpass("本次调用的 API key: ")
try:
    report = generate_images(manifest, "assets", config, key, enabled=True)
finally:
    del key
# 检查 report 后，由调用方明确执行插入；不会写入 Markdown 文件。
markdown = insert_images("正文\n\n[[IMAGE:forest]]\n", "assets")
# 得到 ![绿色森林](forest.png)，图片引用相对 assets_dir。
export_book(markdown, "book.epub", title="书名", assets_dir="assets")
```

`enabled` 必须是布尔值 `True`，`1`、字符串 `"true"` 均不启用。key 必须经参数临时传入；调用方负责真实授权和凭据生命周期。Python 字符串不能保证内存安全擦除，`del` 只是解除引用。不要把 key 写入 config、prompt、alt、日志或源码。

清单必须是 1–10 个字典组成的 list，每项恰好包含 `slug`、`prompt`、`alt`。prompt 非空且不超过 2000 字符；alt 可空且不超过 200 字符；两者不允许控制字符（包括换行）。slug 为小写 ASCII 字母开头、至多 64 字符，其余允许小写字母、数字、下划线、连字符；拒绝 Windows 保留名称、重复 slug 和已有同名产物。整个清单在任何付费请求前检查。

`base_url` 和 `generation_model` 可省略，默认值如上。其他兼容 OpenAI images/generations 和 chat/completions 的 HTTPS 服务可配置。要求标准 HTTPS 443 端口，无 URL 凭据、查询参数或 fragment。视觉模型必须显式提供，不能与生成模型同名，也不能包含 `seedream`（不区分大小写）。无法仅凭不透明部署 ID 识别实际模型，调用方必须确保其指向独立视觉模型；错误部署或不兼容响应会关闭插入资格。

## 网络与图片验证

每张图片至多一次生成、一次视觉验证。生成请求指定 `n=1`、`response_format=b64_json`。API 请求只使用 `ebe.network.request_json`，分别限制响应为 22 MiB 和 16 KiB。当前网络层拒绝带凭据重定向，限制公网目标并固定解析后的连接地址。

优先使用 `b64_json`；该字段存在但无效时直接失败，不回退 URL。URL 图片只允许 HTTPS、完全匹配 `allow_image_hosts` 的域名（无通配符/子域自动放行），且下载不传 key。当前 `network.fetch` 会自动重定向，因此下载复用 `network._request(..., redirects=0)`；任何重定向均失败。这是对当前网络层私有接口的明确依赖，后续接口变更时需同步验证。图片响应最大 15 MiB。

Pillow 为必需运行依赖，启用前检查可用性；支持静态 PNG/JPEG/WebP，拒绝动画、其他格式、破损数据、单边超过 8192、像素超过 1600 万及重新编码后超过 15 MiB 的文件。验证解码后将像素复制到全新图片并保存 PNG，清除 EXIF、ICC、文本元数据；仅清理后的图片 base64 和本项有限 prompt 发送给视觉模型。

视觉响应必须是没有代码围栏的严格 JSON 对象，字段恰好为 `pass`、`text_present`、`relevant`、`reason`，前三项必须是 JSON 布尔值，reason 不超过 500 字符且无控制字符。拒绝重复 JSON 键、额外字段、模糊格式。仅 `pass=true, text_present=false, relevant=true` 通过。模型是概率性判断，不能保证图片一定正确，建议人工检查成品。

## 产物、失败和插入

验证通过后保存 `<slug>.png` 和 `<slug>.receipt.json`。凭据包含图片 SHA-256、prompt SHA-256、生成/视觉模型、尺寸、alt、通过状态和凭据内容 SHA-256。不保存 prompt 原文、API key、原始 API 响应或模型 reason；后者可能回显不可信信息，因此凭据 reason 固定为空。

禁用返回 `{"status": "disabled", "images": []}`。参数错误在联网前抛出 `ValueError`。运行中失败返回 `status=failed`（用户中断则 `aborted`），包含失败 slug、阶段代码以及剩余 `not_attempted` 条目；不保存上游异常原文。已完成条目保留 `verified` 状态。没有自动重试，包括限流、超时、不支持 base64、视觉拒绝或中断；失败也可能已经计费。写入中断可能留下未配套凭据的图片，该图片不能插入；检查后由用户清理或换 slug，模块不会覆盖现有文件。

`insert_images(markdown, assets_dir)` 返回新文本，不读写书稿。标记必须独占一行，例如 `[[IMAGE:forest]]`，不允许重复、内联或路径形式 slug。图片引用统一为相对 `assets_dir` 的安全 ASCII basename：`![绿色森林](forest.png)`，不包含 assets_dir 前缀。导出时必须给 `export_book(..., assets_dir=...)` 或 CLI `--assets-dir` 传入同一个资产目录，由导出器解析图片引用。assets_dir 自身可为相对当前工作目录的路径，也可为绝对路径，不要求位于当前工作目录内；切换工作目录时使用绝对路径或重新计算目录参数。拒绝 `..`、目录/文件 symlink 或 junction、硬链接文件、缺失凭据、非法凭据、摘要或尺寸不匹配。先检查全部标记，再返回结果，失败不返回部分插入文本。alt 会进行 HTML 和 Markdown 转义；不会把模型回复直接插入 HTML。通用 Markdown 预览器未必使用此资产基准，应以 EBE 导出结果为准。

`examples/image-prompts.json` 提供纯合成、无文字的装饰插图清单，不含书稿、知识库内容或事实性图示。可将其作为 README 命令中的清单路径；创建或读取这个 JSON 不会调用 API，只有显式启用生成才可能收费。

凭据摘要提供完整性检查，并非数字签名：资产目录必须由可信本地用户控制，不能接收外部伪造凭据。如果攻击者可以同时重写图片和凭据，摘要不能证明云端验证来源。目录检查也不防御具有并发修改本地文件系统能力的攻击者。模块不清理原有 Markdown 中的任意 HTML；HTML 渲染器仍应独立禁用或清理不可信 HTML。

## 离线验证

在 EBE 目录执行 `python -m pytest tests/test_images.py -q -p no:cacheprovider`。测试覆盖授权门槛、输入限制、网络边界、失败即关闭、停止后不重试、凭据/图片篡改、路径与 alt 注入和元数据清理。串联测试使用 mock 生成与视觉响应，实际执行 `insert_images → export_book`，检查 HTML 内嵌图片和 EPUB 打包图片的字节内容；覆盖嵌套目录、特殊字符目录、相对路径、绝对路径和资产目录位于当前工作目录之外的情况。示例清单也经过 mock 生成流程验证。所有网络请求都 mock，不调用真实 API、不收费。尚未做真实模型互通验收。
