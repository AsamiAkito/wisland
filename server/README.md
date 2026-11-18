# 知识投递口后台：Docker Compose 部署

这个目录是独立部署包。服务器不安装 Wisland、Qt 或 Obsidian，也不需要 GPU。电脑保留 Vault，后台调用 DeepSeek 官方 API 处理主动投递的材料。

## 准备

将整个本目录上传到服务器，例如 `/opt/wisland-knowledge-server`，保留 `app.py`、`processing.py`、Dockerfile 和 compose.yaml 的相对位置。

在服务器复制 `.env.example` 为 `.env`，通过 1Panel 文件编辑器或 SSH 设置：

```dotenv
DEEPSEEK_API_KEY=你的官方API密钥
DEEPSEEK_MODEL=deepseek-flash
KNOWLEDGE_TOKEN=随机生成的长访问令牌
MAX_UPLOAD_MB=256
MAX_MEDIA_MINUTES=60
MAX_STORAGE_GB=10
```

`KNOWLEDGE_TOKEN` 至少 24 字符，建议通过下面命令生成并保存。它用于电脑与后台之间的认证，与 DeepSeek 密钥分开。

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`.env` 留在服务器，权限设置为仅管理员可读；不要把真实密钥放到镜像、仓库或聊天。

## 启动

在上述目录运行：

```sh
docker compose up -d --build
docker compose logs --tail 50
```

也可以通过 1Panel 的容器编排功能管理同一个 Compose 项目，构建上下文需要指向本目录。首次构建会安装 FFmpeg，需要服务器能访问 Docker 镜像源和系统包源。当前 Dockerfile 将 Debian 主仓库切换到清华镜像，保留原安全更新仓库及签名校验，并设置下载超时与重试次数。

如果旧版构建在 `apt-get update` 下载 Debian 包索引时长时间重试，先 Ctrl+C 停止构建，将更新后的 Dockerfile 单独替换到此目录，再运行 `docker compose --progress plain build knowledge` 和 `docker compose up -d`。保留 `.env`、Compose 和数据卷，不使用 `--no-cache`，已完成的基础镜像下载可继续复用。

网络相关构建分三处：Python 基础镜像由 Docker/1Panel 的镜像源或代理配置决定；Debian 软件包由 APT 源决定；Python 依赖由 PyPI 源决定。当前 Dockerfile 的主仓库和 PyPI 默认使用清华镜像，安全更新仓库使用官方 HTTPS 地址。均保留签名/证书验证，并设置超时与重试。

Dockerfile 支持 `PYTHON_IMAGE`、`DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR`、`PIP_INDEX_URL` 构建参数。依赖层在代码层之前，普通代码更新可复用已完成的安装层。仅复制运行所需 Python 文件，不把 `.env` 或备份文件复制进镜像。当前依赖提供 wheel，安装时只使用二进制包，避免意外进入源码编译。

服务监听容器的 `8766` 端口，宿主机只绑定 `127.0.0.1:8766`。数据保存在 Docker 命名卷 `knowledge-data`，重建容器保留任务。

## HTTPS 反向代理

使用你现有的网站/反向代理为服务提供域名和有效 HTTPS 证书，将请求转到宿主机 `127.0.0.1:8766`。

如果反向代理本身在另一个容器内，容器的 `127.0.0.1` 指向它自己，应通过适当的容器网络连接知识服务；不要把业务端口直接暴露到公网。

Nginx/OpenResty 的对应位置可使用以下参数：

```nginx
client_max_body_size 256m;
proxy_read_timeout 300s;
proxy_send_timeout 300s;
proxy_request_buffering off;
proxy_set_header Authorization $http_authorization;
```

不自动改动现有 1Panel 站点、安全组或防火墙。`/health` 同样需要访问令牌，成功返回 `api_version: 3`。这只说明后台连接正常，实际 AI 处理仍需要有效的 DeepSeek API Key、余额和接口权限。

## 电脑端连接

1. 进入灵动岛的“知识”页，点击“设置”。
2. Vault 选择 `C:\Obsidian\MyVault`，目录中应有 `.obsidian`。
3. 填入 HTTPS 服务地址和 `KNOWLEDGE_TOKEN`。
4. 检查连接，再保存。首次连接时旧材料默认仅本地；明确勾选同时上传，或之后逐条选择“上传整理”。

DeepSeek API Key 不填入客户端。每次拖入只收集主动投递的材料，不扫描上传旧 Vault。

客户端可填写自己的标签和整理说明；API v3 随材料接收这些补充信息，交给模型作为总结重点。自己的标签与 AI 主题分开保存。任务去重包含材料内容和说明版本，避免复用其他说明生成的结果。旧后台需要使用新部署包重新构建后才支持此功能。

## 当前支持范围

- 文字、代码、截图和图片，PDF、Office、电子书等受支持文件。
- 普通公开网页：通过 DeepSeek Fetch 提取正文。登录/付费/无法读取的网页保留来源，并标明“仅保存来源”。
- 本地音频/视频：FFmpeg 提取音轨、ASR 分段转写；视频优先使用内嵌文字字幕，最多抽样分析六张画面。时间轴为原字幕时间或片段级定位，不是逐词时间。
- 直接指向受支持媒体/图片/PDF 文件的公开 URL：限制下载大小、逐跳验证公开地址后处理。
- B 站、YouTube 等平台的视频页面链接：当前只保存来源，不自动下载平台视频；拖入本地视频文件可以完整进入媒体处理流程。
- 单文件默认 256 MiB；DeepSeek 文档解析本身有更小的大小限制，超限显示具体失败，原始材料保留。长文提取与总结可能不能覆盖全文，笔记记录截断和提取说明。
- 默认超过 60 分钟的媒体等待用户在任务页点击“继续处理”，明确单次解除该限制。24 小时为硬上限。

接口能力取决于实际官方账号，部署后需要投递少量样本验证。OCR、文件解析、ASR 和模型推理会产生相应 API 用量。

## 维护与数据

- 一个处理 worker，最多 12 个连接线程；容器限制 1 GiB 内存、1.5 CPU。
- 处理失败保留任务。临时网络错误自动重试最多 4 次，之后客户端显示重试入口。
- 支持取消任务并清理服务器副本，保留取消 UUID 防止迟到上传或重试复活任务。正在调用模型时会在当前请求结束后的检查点停止；已交给模型的内容无法撤回。取消后不会再写回笔记。客户端清理状态以服务器确认为准。
- 返回处理阶段、片段和关键帧进度。上传进度由客户端按字节计算，模型推理阶段使用流动条。
- 已取回材料在服务器保留 7 天，未取回材料保留 30 天；清理后保留到期状态。本机原始副本不自动清理，可重新提交。
- 服务器上传到 DeepSeek Files API 的文件设置 1 小时过期，并在提取后尝试提前删除。
- 不使用 `docker compose down -v`，除非你明确想删除服务器任务数据；命名卷应纳入你的备份范围。
- 修改 `.env` 后使用 `docker compose up -d --force-recreate` 使配置生效。

## 本地验证

仓库根目录可运行：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时 Vault、本机 HTTP 后台和模拟 AI，不调用收费接口。

## 部署后先这样验收

1. 配置 `.env`，运行 `docker compose up -d --build`。
2. 执行 `docker compose exec knowledge python -m server.smoke_test`。出现 PASS，表示当前容器内认证、队列、模拟处理和取消逻辑可运行。这项检查使用临时目录，不修改生产任务、不调用真实模型。
3. 配置 HTTPS 反向代理，电脑端“知识 → 设置”检查连接并保存。
4. 投递一小段测试文字，检查“上传 → AI 整理 → 已入库”，确认电脑上的 Obsidian 目录确实出现笔记。这一步才验证真实 Key、模型权限和最终入库。

## 验证范围

自动测试使用临时目录、模拟模型及本机 HTTP 服务。真实部署需按上面的验收步骤检查网络、令牌、模型权限和入库结果。图片、音视频等材料分别验收，文字成功只验证文字链路。

后台负责整理与返回结果，写入 Obsidian 由运行中的电脑端完成。本目录是源码部署包，不包含预构建镜像。

Compose 为知识后台设置 json-file 日志轮转：每个文件10MB、保留3个。修改配置后需通过 Compose 重建对应容器才会应用；重启已有容器不会自动采用新日志配置。不要通过清理数据卷来解决日志占满的问题，先区分容器日志、镜像缓存和业务数据。

## 文字提取优先（2026-11-17）

当前默认已切换为轻量索引：`POST /v1/tasks/<id>?mode=index`。`mode=deep` 保留完整提取流程；不传 mode 时使用 index。服务器会持久化模式并区分去重键，避免深度整理复用索引结果。`/health` 仍返回 api_version=3，并增加 default_mode 与 processing_modes 字段。

轻量结果保留原有字段，同时提供 `index_only=true`、`purpose` 和程序生成的 `coverage`。`original` 为空，不输出全文转写；这不影响客户端保存原件。分类合同集中在 `taxonomy.py`，部署包和 Dockerfile 需一并携带该文件。

默认最多取6000字符、PDF 5页、图片概览与少量片段、视频3帧或音频前45秒。选择扫描页时仍会发生预览文件解析及模型输入计费；轻量不等于文件处理免费。Pillow用于本地缩略预览，不需GPU。需要完整文字时由客户端菜单显式触发 deep。

服务器依赖新增 `pypdf`。DOCX、PPTX、XLSX 优先直接解析已有文字；PDF 在本地逐页读取文字层，仅无文字且有内容的页回退识别。普通图片通过 DeepSeek Flash 视觉输入，其他格式和超出直接处理范围的文件回退文件解析。内嵌图片、复杂版式及表格原始值的解释仍需结合原件。

本机运行完整测试前先安装 `server/requirements.txt`。部署后执行 smoke test，再用少量实际材料验收；测试命令与生产任务隔离。

## 首次配置与 SSH 高级功能

仓库提供空白示例，使用者填写自己的地址、令牌和 Vault。DeepSeek API Key 只放在服务器 `.env` 中，与客户端访问令牌分开。新安装默认仅本机保存；自动上传、SSH 和专题维护均需在知识设置主动开启。已有显式配置继续生效。

普通 HTTPS 连接只需在知识设置填写地址、令牌及 Vault。SSH 为可选的传输和高级维护方式，不要求项目维护者提供公共服务器。

使用 SSH 时，在本机 `config.json` 的 `knowledge` 字段补充以下项目（其他字段结构见 `config.example.json`）：

```json
{
  "ssh_host": "203.0.113.10",
  "ssh_user": "your-user",
  "ssh_port": 22,
  "ssh_key": "C:/Users/YourName/.ssh/wisland_deploy_ed25519",
  "ssh_project_dir": "/opt/wisland-knowledge-server",
  "ssh_service": "knowledge"
}
```

`203.0.113.10` 是文档示例地址。替换为实际主机与账户，预先授权公钥并验证主机指纹。账户需能访问后台；专题维护还需能在该项目执行 Docker Compose。`ssh_key` 指向自己的本机私钥，不将私钥上传到服务器或仓库。

`ssh_project_dir` 是服务器上包含 `compose.yaml` 的绝对目录，`ssh_service` 是其中的服务名。如果使用其他部署目录或服务名，在这里填写实际值。未配置时沿用旧版 `/opt/wisland-knowledge-server` 和 `knowledge`，现有部署无需改名。

SSH 隧道将本机配置的 `ssh_local_port`（默认 18767）转发到服务器回环地址的 8766 端口；修改后台监听端口需要同步调整隧道实现。仅连接 HTTPS 可使用基础 AI 索引和入库，自动专题维护目前仍需 SSH，默认关闭。
