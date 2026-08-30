# AI 科研论文工作台 (ResearchBench)

面向研究生 / 科研工作者的个人论文管理工作台。闭环：**发现最新论文 → 一键收录 → 分类管理 → 阅读 → 记录笔记 → TeX 中文版生成飞书阅读文档 → 阅读统计与长期复盘**。

值得注意的是，本工作台 **90%** 的核心代码，都由 **workbuddy** 完成

## 界面预览

| 看板Dashboard | 论文库 |
|:---:|:---:|
| ![看板](./images/image1.png) | ![论文库](./images/image2.png) |

| 资讯库 | Research Radar | 设置 |
|:---:|:---:|:---:|
| ![资讯库](./images/image3.png) | ![Research Radar](./images/image4.png) | ![设置](./images/image5.png) |

## 设计理念：可独立使用，也支持 AI 增强

ResearchBench 的核心数据流（论文发现、收录、分类、阅读、笔记、TeX→飞书、统计）**无需任何外部 AI 服务即可完整运行**。arXiv 检索直连官方/镜像 API，资讯检索聚合 Google News 与国内 RSS，TeX 解析与飞书文档生成均为本地规则实现；API Key 仅用于可选的 AI 增强功能。同时，系统提供统一的 OpenAI 兼容接口配置，接入大模型或智能体后可用于论文总结、库内问答等场景，进一步提升信息处理的上限。

## 架构

## 五大模块

1. **论文库 (Paper Library)** — 分类字段管理、论文卡片 → **独立详情页**（渲染 Markdown 笔记/心得）、**编辑模式**与详情页分离、CRUD、搜索/筛选（关键词/领域/状态/标签）、URL 自动抓取元数据（按钮内联于「原文链接」行）、**笔记/心得支持 Markdown 编辑 + 实时预览 + 上传 .md 解析 + 粘贴飞书云文档链接导入**、**TeX 仓库改为文件夹上传**（`<input webkitdirectory>` 上传到 `data/tex_repos/{id}`，生成飞书文档时自动复用）。
2. **TeX → 飞书云文档工具** — 独立脚本 `tex2feishu.py`。不依赖 LLM：定位主 TeX 文件 → 展开 `\input`/`\include` → 解析章节/列表/公式/表格/图片 → 上传图片并按位置插入 → 写入同名飞书文档 → 返回链接并回写论文卡片。飞书凭证来自 `test_feishu_final_version.py` 的默认应用，也可在「设置-飞书」覆盖。
3. **Research Radar** — 手动/定时发现最新论文(arXiv)与 AI 资讯(Google News)。关键词+领域+时间范围配置、去重、标记「已在库中」、一键收录。**检索数据源为写死的 arXiv API + Google News RSS（不接 WorkBuddy AI）**；论文结果「加入论文库」，资讯结果「加入资讯库」。已内置 7 套默认模板（VLA/WAM、World Model、Multimodal、Agent、RAG、AI 产业、具身智能落地）。
4. **资讯库 (News Library)** — 独立 Tab，沉淀 Research Radar 检索到的 AI 资讯。仅做**只读沉淀**：卡片展示标题(链接)/来源/时间，支持搜索、按来源筛选、删除；**无修改/编辑功能**。数据来自 Radar 资讯检索「加入资讯库」或「全部加入资讯库」。
5. **阅读统计与督促** — 今日/本周/本月收录数、未读/在读/已读、连续阅读天数、领域分布、14 天趋势、每周目标进度（Chart.js 可视化）。
6. **AI 与自定义 API** — 统一的 API 配置页（Provider / Base URL / API Key / Model），后端 Fernet 加密存储，OpenAI 兼容。AI 能力（总结/库问答）在此配置后启用。

## 运行方式

```bash
# 1. 安装依赖（建议使用隔离 venv）
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 启动后端（默认 127.0.0.1:8765）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. 浏览器打开
http://127.0.0.1:8765
```

## 首次使用提示

- **飞书授权**：使用「TeX → 飞书」功能前，先在「设置-飞书」页点「授权」，浏览器完成 OAuth 后自动保存 token（位于 `data/feishu_token.json`，可自动刷新）。飞书应用默认用个人测试应用，建议在「设置-飞书」覆盖为自己的 App ID / Secret。
- **论文库**：先建「研究领域」分类，再录入论文；粘贴 arXiv / 网页链接可自动抓取标题摘要。
- **Research Radar**：首次检索需可访问外网（arXiv / Google News）。网络异常时接口返回 502 并给出明确提示，不会崩溃。
- **TeX → 飞书**：在论文卡片填入「TeX 仓库目录」本地路径，点「生成飞书文档」即可。

## 说明

- 后端 SQLite 数据库位于 `data/research.db`，首次启动自动建表并写入 7 套默认 Radar 模板。
- API Key 等敏感信息使用 Fernet 对称加密，密钥文件为 `data/.secret_key`（首次启动自动生成）。
- 前端为纯静态 SPA，无构建步骤；Chart.js 已本地化（`app/static/chart.umd.min.js`）以支持离线。

---

# 部署到服务器 / 本地 HTML 调用远程服务

把这套工作台跑在一台长期在线的服务器上，并让**本地 HTML 文件（双击打开）**也能调用它的检索能力。

## 1. 一键部署

```bash
# 1) 生成配置（按需修改 ADMIN_PASSWORD / HOST_PORT / RADAR_PROXY）
cp .env.example .env

# 2) 本机 / 服务器上直接一键部署（构建镜像 → 启动容器 → 探活 → 打印可访问 URL）
bash deploy.sh
```

脚本会：检查 docker 与 docker compose 是否存在、守护进程是否运行、端口是否被占用 →
`docker compose up -d --build` → 轮询 `/api/health` 直到就绪 → 打印
`http://127.0.0.1:8765` 与自动探测到的**局域网 URL**。构建失败会直接报错退出并提示看日志
（`docker compose logs -f`）。

**部署到远程服务器**（脚本会通过 SSH 在远端执行同样动作）：

```bash
SERVER_HOST=1.2.3.4 SERVER_USER=root SERVER_PORT=22 bash deploy.sh
# 可选：SERVER_DIR=~/researchbench（远端目录，默认 ~/researchbench）
#       SERVER_HOST_PORT=8765（远端对外端口，默认同 HOST_PORT）
```

同步代码优先用 `rsync`，没有则自动退化为 `tar + ssh`。远端会保留已有的 `.env`（不会覆盖你的配置）。
脚本结束后打印 `http://<SERVER_HOST>:<端口>`。

其它常用命令：

```bash
docker compose logs -f        # 看日志
docker compose down           # 停止
docker compose up -d --build  # 改完代码重新构建
```

### 端口与防火墙

- 默认端口：容器内 `8765`，宿主 `8765`（改 `.env` 里的 `HOST_PORT`，或 `HOST_PORT=8080 bash deploy.sh`）。
- 云服务器必须放行 TCP 端口：**云厂商安全组 + 系统防火墙**两处都要开，例如
  `ufw allow 8765/tcp`（Ubuntu）或 `firewall-cmd --add-port=8765/tcp --permanent && firewall-cmd --reload`（CentOS）。
- 仅本机/内网使用时不要映射公网端口；公网部署**务必设置 `ADMIN_PASSWORD`**。
- 生产环境建议用 Nginx/Caddy 做反向代理并配置 HTTPS（Caddy 可自动签发证书）。

### 数据持久化与文件属主

- `docker-compose.yml` 把 `./data` 挂载进容器，数据库、飞书 token、密钥、TeX 仓库都在里面。
- 容器默认以**当前宿主用户的 UID/GID** 运行（`user: "${UID:-10001}:${GID:-10001}"`），
  避免 Linux 上出现 `data/` 被改成 root 属主、宿主机改不动的情况。
- 若已经产生 root 属主文件：`sudo chown -R $USER ./data` 后重启容器。
- 备份即备份 `data/` 目录（`data/workbench.db` 是全部业务数据）。

### 服务器访问不了 arXiv / Google News 怎么办

在 `.env` 里配置 `RADAR_PROXY`（支持 `http://user:pass@host:port`）。
如果代理跑在**宿主**上（例如本机 `127.0.0.1:52129`），容器里这样写：

```bash
RADAR_PROXY=http://host.docker.internal:52129
```

`docker-compose.yml` 已加 `extra_hosts: host.docker.internal:host-gateway` 做映射。

## 2. 管理员密码（只保护"改数据"，不影响搜索）

| 场景 | 行为 |
| --- | --- |
| 未设置 `ADMIN_PASSWORD` | **完全放行**，与以前一样，不做任何校验 |
| 已设置 `ADMIN_PASSWORD` | `POST / PUT / PATCH / DELETE` 必须带正确密码，否则 `401` |
| `GET` | 一律放行（看板 / 列表 / 搜索 / 统计 / 详情页都正常） |
| 检索类接口 | `POST /api/radar/run`、`/api/radar/run_template/*`、`/api/papers/fetch_metadata`、`/api/services/*` 与 `/api/health` **放行**（它们只读、不改数据） |

- 传密码：请求头 `X-Admin-Password: 你的密码`。
- 网页端：任何写操作收到 `401` 时会自动 `prompt()` 让你输入密码，存进 `sessionStorage`，
  带上请求头**重试一次**（改在 `app/static/app.js` 的 `requestWithAdmin()`，对现有代码零侵入）。
  密码输错会清掉缓存，下次继续弹窗。关闭浏览器标签即失效。
- 命令行示例：`curl -X POST -H "X-Admin-Password: 你的密码" ...`
- 改密码：修改 `.env` 后 `docker compose up -d` 重启（密码在启动时读取）。

> 这只是防误操作的轻保护，不是账号体系：**公网部署请务必设置**，并配合 HTTPS 使用。

## 3. 对外服务接口（无状态、不落库）

`app/routers/services.py`，全部用 **Pydantic model 接 JSON body**（避免原项目"前端发 JSON、后端按 query 收"
的传输层不匹配问题）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/services/health` | 健康检查（deploy.sh 与本地客户端都用它探活） |
| GET | `/api/services/capabilities` | 能力清单：哪些已服务化、哪些没实现及原因 |
| POST | `/api/services/radar/run` | 论文/资讯检索。body：`{type, keywords, field, days, max_results, lang, channel}`；返回 `{type, count, results, sources}`。**不查 SQLite**，因此 `in_library` 由调用方自己标记 |
| POST | `/api/services/metadata` | 按链接抓取公开元信息（arXiv 走官方 API，其余抓 title/description）。body：`{url}` |
| POST | `/api/services/ai/chat` | **默认关闭**（返回 501）。需服务端 `ENABLE_AI_PROXY=1`，且调用方自备 `base_url/model/api_key`，服务端转发后立即丢弃，不落盘、不记录 |

设计原则：这些接口**无状态、不落库、不写文件**，所以即使服务公开，调用方也只是借用检索能力，
个人论文库不会外泄。检索算法仍由 `app/services/arxiv.py`、`app/services/news.py` 执行（未改动）。

CORS 通过 `ALLOW_ORIGINS` 配置（默认 `*`）。**本地 HTML 双击打开时 Origin 是 `null`**，
按 CORS 规范 `allow_origins=["*"]` 时不能同时 `allow_credentials=True`，
代码里已在通配模式下强制 `allow_credentials=False`（见 `app/main.py` 注释）。
若需更强的隔离，把 `ALLOW_ORIGINS` 配成你的静态站域名白名单（逗号分隔）。

## 4. 本地 HTML 客户端（`static_client/`）

**直接双击 `static_client/index.html`** 即可使用（`file://` 协议，无需任何服务器）。

- 首次打开会提示填写**远程服务地址**（存 `localStorage`，页面顶部可随时修改 / 一键测试连接）。
- 填好关键词后点「运行 Radar」，走上面的 `/api/services/radar/run`；结果可「加入我的论文库」，
  已在库的条目显示「已在库」徽章。
- 「按链接抓取元数据」用 `/api/services/metadata`，抓到后直接入库。
- 论文库存在浏览器 `localStorage`（键名 `researchbench.papers`）。
  **清缓存 / 换浏览器会清空，请务必定期点「导出 JSON」备份**，换机器时用「导入 JSON」恢复。
- 实现上用**普通 `<script src>`**（非 `type="module"`）、导入走 `<input type="file">`，
  因为 `file://` 下 ES module 与 `fetch()` 读本地文件都会被 CORS 拦截。

### 暂未实现（有意留空，不做伪造）

| 功能 | 原因 |
| --- | --- |
| TeX → 飞书云文档 | 需要读取调用方**本地磁盘**的 TeX 目录 + 飞书用户 OAuth 授权。浏览器沙箱禁止访问本地文件系统，托管服务器也没有你的授权凭据。请用完整版主应用在本机跑 |
| 遍历本地文件夹 / 上传 TeX 仓库 | 同上，网页无法枚举本地目录 |
| AI 总结 / 论文库问答 | 需要你的模型 API Key。为避免由托管服务器代持密钥，本地客户端不调用 AI；服务端 `/api/services/ai/chat` 默认关闭。请在自己的环境用主应用「设置 - API」配置 |
| 多设备同步 | 论文库只在当前浏览器；用导出/导入 JSON 手工迁移 |
