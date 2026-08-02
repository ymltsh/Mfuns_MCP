# Mfuns MCP Server

喵御宅（Mfuns）社区的开源 MCP（Model Context Protocol）服务器，让 AI Agent 通过标准化工具接口浏览、互动、管理 Mfuns 社区内容。

- **技术栈**：Python 3.10+ / MCP Python SDK 2.0+ / httpx
- **传输方式**：stdio（默认）、Streamable HTTP、SSE
- **鉴权**：账号密码自动登录，token 本地缓存，失效自动重登

---

## 功能概览（17 个工具）

### Discovery 发现

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_browse` | 浏览内容流：`recommend` 推荐 / `hot` 热门 / `feed` 全站动态 / `category` 分类帖子 / `latest` 最新聚合时间线（动态+视频+文章，LLM 友好 Markdown，来自自建服务 mfuns.wgen.top，支持 `content_type` 过滤） |
| `mfuns_read` | 读取内容详情与评论区（帖子/视频/动态，含二级回复、图片解析、评论 ID） |
| `mfuns_search` | 搜索内容（文章/视频）或用户 |
| `mfuns_get_user` | 用户资料（互动前判断新人/老用户） |

### Interaction 互动

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_comment` | 评论帖子 / 评论视频 / 回复评论（纯文本自动转 Quill） |
| `mfuns_create_post` | 发布文章（Markdown，支持草稿、标签、封面） |
| `mfuns_create_feed` | 发布动态 |
| `mfuns_react` | 点赞 / 取消 / 点踩（文章、视频、评论、动态） |
| `mfuns_favorite` | 收藏 / 取消收藏 / 查询收藏状态 |
| `mfuns_delete` | 删除动态 / 评论 / 文章投稿（仅本人内容） |
| `mfuns_messages` | 私信会话列表 / 聊天记录 |

### Account 账号

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_notifications` | 通知消息（赞/评论/提及）与未读计数 |
| `mfuns_history` | 浏览历史（避免重复互动） |

### Publishing 投稿

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_publish_video_upload` | 本地上传视频投稿（阿里云 VOD 全流程，支持分P） |
| `mfuns_publish_video_link` | 外链视频投稿（视频直链 URL，如复活失效的 B 站外链，支持分P） |
| `mfuns_manage_submission` | 投稿管理：列表 / 详情 / 更新（文章+视频，分P增改）/ 删除（`draft=true` 保持草稿状态） |

### System 系统

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_activity_log` | 查询 Activity Log（每次工具调用的操作记录，按日期隔离） |

---

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置凭据

复制配置模板并填入账号密码（或使用环境变量）：

```bash
Copy-Item config.json.example config.json
```

```json
{
  "base_url": "https://api.mfuns.net",
  "account": "你的手机号/用户名",
  "password": "你的密码",
  "token": "",
  "user_id": null
}
```

或者环境变量（优先级更高）：

```bash
$env:MFUNS_ACCOUNT = "你的账号"
$env:MFUNS_PASSWORD = "你的密码"
```

首次调用写操作时自动登录，token 缓存回 `config.json`（约 25 天有效，失效自动重登）。未配置凭据时读操作（浏览/读帖/搜索）可匿名使用。

### 3. 启动

```bash
uv run main.py                                # stdio（默认，MCP 客户端用）
uv run main.py --transport streamable-http    # Streamable HTTP，默认 127.0.0.1:8000/mcp
uv run main.py --transport streamable-http --host 0.0.0.0 --port 9000
uv run main.py --transport sse                # SSE
```

启动时向 stderr 打印服务状态与使用说明（stdio 模式下 stdout 仅传输 JSON-RPC 协议）。

### 4. MCP 客户端接入（opencode 示例）

本地 stdio：

```json
{
  "mcp": {
    "mfuns": {
      "type": "local",
      "command": ["uv", "--directory", "D:\\path\\to\\Mfuns MCP", "run", "main.py"],
      "enabled": true
    }
  }
}
```

远程 Streamable HTTP：

```json
{
  "mcp": {
    "mfuns-http": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/mcp",
      "enabled": true
    }
  }
}
```

---

## Activity Log

每次 MCP 工具调用自动记录一条操作日志，按日期隔离为 JSON 文件，不参与 Agent 默认上下文：

```text
logs/activity/
├── 2026-08-02.json
└── ...
```

```json
{
  "time": "02:31:12",
  "tool": "mfuns_read_thread",
  "action": "read",
  "target": { "type": "article", "id": 83888 },
  "params": { "article_id": 83888 },
  "result": { "status": "success" }
}
```

通过 `mfuns_activity_log(date, tool?, target_id?)` 按日期 / 工具名 / 对象 ID 查询。不存储 LLM 思考与完整上下文；数据量大后可平滑迁移 SQLite / PostgreSQL（结构不变）。

---

## 项目结构

```text
main.py               # 启动入口（stdio / streamable-http / sse）
config.json           # 配置 + token 缓存（勿提交，已在 .gitignore）
config.json.example   # 配置模板
opencode.json         # opencode 本地 MCP 接入配置示例
mfuns_mcp/
├── server.py         # MCPServer 构建与入口
├── tools.py          # 17 个 MCP 工具定义
├── client.py         # HTTP 客户端：自动登录 / 401 重登 / 5 QPS 限速
├── activity.py       # Activity Log（日期 JSON 文件 + 工具装饰器）
├── config.py         # config.json 读写与环境变量覆盖
└── format.py         # 纯文本↔Quill、HTML→纯文本、时间戳格式化
```

---

## 已知限制

| 限制 | 说明 |
| ---- | ---- |
| 待审核视频不可编辑 | `video/update` 对待审核稿件返回"系统繁忙"（服务端锁定），发布后可更新 |
| 视频投稿删除 | API 无视频删除接口，需网页端处理 |
| 评论点赞接口 | API 实测 `type=4`（文档标注的 3 已废弃）；type=3 返回 404 |
| 收藏取消需 list_id | 收藏夹枚举未暴露为工具，`remove` 需提供 list_id |
| 官方全站动态流为空 | `/v1/feeds/list` 对该账号无可展示内容，动态流建议用 `latest` 模式 |
| 投稿频率限制 | 投稿类接口有用户级限速（约 2 分钟窗口，与网页端共享额度），429 消息含剩余等待时间 |

---

## 参考

- [Mfuns 官方开放平台 API 文档](https://open.mfuns.net/api/)
- [MCP 官方文档](https://modelcontextprotocol.io/docs/2026-07-28/develop/build-server)
