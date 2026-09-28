EBE · Ebook-Engine v1.0.0 — KacaleSSS

首个公开版本：有界并发采集与恢复、持久化资料库、120篇门槛、稀疏交叉核验及缓存、章节任务与新鲜度绑定的发布审核、离线 HTML/EPUB/PDF、可选图片生成与视觉审核。

下载 `EBE-v1.0.0.zip` 后解压，Windows 运行 `powershell -File install.ps1`，Linux 运行 `sh install.sh`。也可安装附带 wheel 或 README 中的 Git tag。首次安装需下载依赖；本地模型与中文字体不捆绑。

默认图片关闭。严格隐私部署请使用无网络容器，模型必须处于同一隔离边界；云图片是用户显式开启的联网例外。自托管 CLI/MCP/只读状态 API，不是公网多租户 SaaS。

修复明细、验证范围及限制见仓库 `docs/SECURITY-REVIEW.md`、`docs/VALIDATION.md`。`deepseek-v4-flash` 已通过两次仅合成资料的真实接口烟测；Doubao 未做付费真实调用。

安装前可用 SHA256SUMS 检查附带文件。MIT License。
