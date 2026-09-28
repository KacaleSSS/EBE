# EBE 安全边界

## 已采用的边界

1. 私人知识库、数据库、上下文、撰写、排版只在 core。默认安装 Python 回环网络保护；严格部署必须使用 Docker `network_mode: none`。这会保留 loopback，但关闭容器外连接。参考：[Docker none](https://docs.docker.com/engine/network/drivers/none/)。
2. Collector不挂载项目；接收用户检查过的公开搜索词、URL清单，写入独立spool。文献解析在离线core完成；collector不打开PDF、DOCX或脚本。搜索本身会披露检索词，不能承诺匿名。
3. 图片默认禁用，是显式的联网例外。只发送独立提示词与图片，key从终端无回显输入，不存配置，不重用开发者旧密钥；提供商错误正文不进日志。
4. 公网下载验证所有DNS地址并固定实际连接IP，TLS校验仍使用原域名；每跳重新验证，不读取代理环境，无Cookie；API请求拒绝重定向。限定响应大小与解压后大小，字节/超时有界。
5. 不执行文献或模型返回代码。任意命令worker已禁用。唯一原生解析器例外是固定pdftotext，使用最小环境、私有临时目录、固定参数；原生解析器仍需容器资源限制和更新。
6. 本地模型只允许字面127.0.0.1或::1 HTTP端点；不自动启动程序、不允许云端URL、不跟随重定向。模型自身必须在相同OS隔离边界内；在宿主运行一个联网模型进程不能称为全链路离线。
7. 路径限定、symlink/junction拒绝、内容哈希和SQLite持久化；镜像批次合并，失败时不报告镜像成功。镜像白名单排除凭据文件。
8. 在线只读状态API仅绑定回环地址，使用至少32字符Bearer令牌、拒绝Origin和异常Host、不记录请求、无写接口。它不是面向不可信公网的多租户服务；不要直接暴露端口。
9. 本地输出不含外部资源脚本，拒绝外部图片、路径逃逸与缺字PDF。图片receipt是可信本地文件的完整性校验，不是对恶意本机用户的数字签名。

## Linux准备目录

在仓库目录下先创建专属目录；不要把任何HOME或共享资料根目录挂入容器：

```sh
mkdir -p runtime/projects runtime/mirror runtime/spool runtime/image-exchange
sudo chown 10001:10001 runtime/projects runtime/mirror runtime/spool runtime/image-exchange
chmod 700 runtime/projects runtime/mirror runtime/spool runtime/image-exchange
docker compose build core
docker compose run --rm core doctor
```

这只更改上述专用目录的属主，不递归修改已有用户资料。主机加密磁盘、密钥保管、备份权限与管理员账户保护由部署者负责。Docker管理员等同高权限；不允许把docker.sock挂进任何EBE容器。

## 仍然存在的限制

- Python audit hooks不是完整沙箱，不抵御同进程恶意native扩展或恶意主机管理员。普通pip安装提供应用级约束，不等同OS隔离。
- TLS连接固定IP防DNS重绑定；DNS解析仍受系统解析器超时影响，不能保证整个任务严格墙钟上限。容器/作业超时应覆盖此阶段。
- 外网下载只能防止私网请求和限制资源，不能证明文献无恶意、无版权限制或无错误。解析器需保持更新。公开URL路径也可能包含个人信息，导出清单需用户检查。
- 云端MCP客户端会收到工具结果；严格隐私模式使用本地模型/客户端。EBE不控制外部客户端遥测。
- 内容哈希只证明文件未变，不证明事实正确。稀疏未抽到的普通主张明确保持未核验；关键主张不可因预算不足自动通过。
- 同盘镜像不是设备故障备份。文件删除不等于SSD安全擦除；默认不清理知识库，保留/销毁需要显式离线操作和独立备份策略。
- 支持可信单用户工作区，不承诺敌对多租户隔离。知识库内容和元数据本地写入权限属于可信操作者。
- 首次安装、构建镜像及下载本地模型需要网络。运行期隔离应在依赖与模型已准备后启用。

## 报告漏洞

不要在公开Issue粘贴密钥或私人文献。优先使用GitHub私密漏洞报告；如仓库未启用该功能，先提交不包含漏洞利用细节或敏感数据的联系请求。
