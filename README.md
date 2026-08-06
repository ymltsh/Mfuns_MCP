# Mfuns MCP Server

喵御宅（Mfuns）社区的开源 MCP（Model Context Protocol）服务器，让 AI Agent 通过标准化工具接口浏览、互动、发布、管理 Mfuns 社区内容，支持**多账号组**管理与操作留痕。

- **技术栈**：Python 3.10+ / MCP Python SDK 2.0+ / httpx
- **传输方式**：stdio（默认）、Streamable HTTP、SSE
- **鉴权**：多账号组（账密自动登录 / token 缓存 / 官方 API KEY），失效自动重登
- **工具数**：23 个（19 业务 + 4 身份）

---

## 功能概览（23 个工具）

### Discovery 发现

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_browse` | 浏览内容流：`recommend` 推荐 / `hot` 热门 / `feed` 全站动态 / `category` 分类帖子 / `latest` 最新聚合时间线（动态+视频+文章，LLM 友好 Markdown，来自自建服务 mfuns.wgen.top，支持 `content_type` 过滤） |
| `mfuns_read` | 读取内容详情与评论区（`resource_type` 支持帖子/视频/动态，含二级回复、图片解析、评论 ID） |
| `mfuns_search` | 搜索内容（文章/视频）或用户 |
| `mfuns_get_user` | 用户资料（互动前判断新人/老用户） |
| `mfuns_categories` | 投稿分区树（叶子分区可投稿，父级分区自动落叶子） |

### Interaction 互动

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_comment` | 评论帖子 / 评论视频 / 评论动态 / 回复评论（纯文本自动转 Quill） |
| `mfuns_create_post` | 发布文章（Markdown，支持草稿、标签、封面，分区自动解析） |
| `mfuns_create_feed` | 发布动态（标签、图片） |
| `mfuns_react` | 点赞 / 取消 / 点踩（文章、视频、评论、动态，type 映射已实测修正） |
| `mfuns_favorite` | 收藏 / 取消收藏 / 查询收藏状态 |
| `mfuns_delete` | 删除动态 / 评论 / 文章 / 视频投稿（仅本人内容） |
| `mfuns_messages` | 私信：会话列表 / 聊天记录 / 发送（form+msg 实测可用） |

### Publishing 投稿

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_publish_video_upload` | 本地上传视频投稿（阿里云 VOD 断点续传，支持分P；可后台任务化：多P 并行上传 + `mfuns_upload_task` 查询进度；任务状态落盘，重启自动续传；本地封面自动上传，缺省用平台默认封面） |
| `mfuns_upload_task` | 视频上传任务查询/管理：`status` 任务详情（分P进度/投稿结果）/ `list` 任务列表 / `cancel` 取消任务 |
| `mfuns_publish_video_link` | 外链视频投稿（视频直链 URL，如复活失效的 B 站外链，支持分P） |
| `mfuns_manage_submission` | 投稿管理：列表 / 详情 / 更新（文章+视频，分P增改/追加，`append_files` 本地上传追加分P 走后台任务，`draft=true` 保持草稿） |

### Account 账号

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_notifications` | 通知消息（赞/评论/提及）与未读计数 |
| `mfuns_history` | 浏览历史（避免重复互动） |

### System 系统

| 工具 | 说明 |
| ---- | ---- |
| `mfuns_account_modify` | 添加/移除账号（添加支持账密 / Token 导入 / API Key 绑定，自动校验与查重；移除自动回退当前账号） |
| `mfuns_account_list` | 查看账号组（active 标记当前身份） |
| `mfuns_account_current` | 查看当前操作身份（发布前确认） |
| `mfuns_account_switch` | 切换当前账号（校验身份防串号，失败自动回滚） |
| `mfuns_activity_log` | 查询 Activity Log（按账号与日期隔离，支持指定账号） |

---

## 多账号身份管理

业务工具**自动使用当前账号**，无需传账号参数：

```
Agent → mfuns_account_switch → current_account → 业务工具 → Mfuns API
```

- 每个账号独立：token / api_key / 登录凭据 / Activity Log 目录
- **防串号**：切换时调 `/v1/user/info` 校验 token 与账号 user_id 一致，不一致拒绝并回滚
- **懒登录**：未配置 token 的账号首次业务调用自动账密登录，token 回写 config
- **临时 id**：未登录账号可用 `u_unknown_N`，首次登录后自动更新为 `u_<user_id>`
- **纯 api_key 账号**：身份不可解析（`user/info` 拒绝 api_key），昵称必填、id 顺序编号（u_1、u_2…），仅用于投稿接口
- 添加账号可用 `mfuns_account_modify(action=add)` 在线完成，无需手改配置文件

---

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置凭据（多账号组）

复制配置模板并填入账号：

```bash
Copy-Item config.json.example config.json
```

```json
{
  "base_url": "https://api.mfuns.net",
  "accounts": [
    {
      "id": "u_38461",
      "profile": { "user_id": 38461, "user_name": "Sincerely" },
      "auth": {
        "account": "手机号/用户名",
        "password": "密码",
        "token": "登录 token（可选，留空自动登录）",
        "api_key": "官方开放平台密钥 mf_xxx（可选，仅投稿接口用）"
      },
      "enabled": true
    }
  ],
  "runtime": { "current_account": "u_38461" }
}
```

> 旧版扁平结构（token/api_key 在顶层）仍兼容：`accounts` 字段缺失时自动合成为单账号。

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

每次 MCP 工具调用自动记录一条操作日志，按**账号与日期**隔离为 JSON 文件，不参与 Agent 默认上下文：

```text
logs/activity/
├── u_38461/
│   ├── 2026-08-02.json
│   └── 2026-08-03.json
└── u_17627/
    └── 2026-08-03.json
```

```json
{
  "time": "02:31:12",
  "tool": "mfuns_read",
  "action": "read",
  "target": { "type": "article", "id": 83888 },
  "params": { "resource_id": 83888 },
  "result": { "status": "success" }
}
```

通过 `mfuns_activity_log(date, tool?, target_id?, account_id?)` 按日期 / 工具名 / 对象 ID / 账号查询（默认当前账号）。不存储 LLM 思考与完整上下文；数据量大后可平滑迁移 SQLite / PostgreSQL（结构不变）。

---

## 鉴权与限速

| 机制 | 说明 |
| ---- | ---- |
| 会话 token | 全局默认鉴权；账密自动登录，token 缓存约 25 天，401 自动重登 |
| 官方 API KEY | 独立体系，仅用于投稿接口白名单（9 个 `/v1/contribute/*` + 素材上传）；401 自动回退该账号 token |
| 防覆盖 | 切换/登录只写当前账号字段，不会覆盖其他账号凭证 |
| 5 QPS | 全局节流（0.25s 最小间隔） |
| 投稿限速 | 投稿类接口用户级限速（约 2 分钟窗口，与网页端共享），429 消息含剩余等待时间 |

---

## 项目结构

```text
main.py               # 启动入口（stdio / streamable-http / sse）
config.json           # 多账号组配置 + token 缓存（勿提交，已在 .gitignore）
config.json.example   # 配置模板
opencode.json         # opencode 本地 MCP 接入配置示例
mfuns_api_docs.md     # 社区 API 逆向文档（含实测修正标注）
mfuns_mcp/
├── server.py         # MCPServer 构建与入口
├── tools.py          # 23 个 MCP 工具定义
├── upload.py         # 视频上传管线：oss2 断点续传 / 后台任务管理 / 封面与 meta 处理
├── client.py         # HTTP 客户端：多账号上下文 / 自动登录 / 401 重登 / 限速 / 图片上传
├── activity.py       # Activity Log（按账号+日期 JSON 文件 + 工具装饰器）
├── config.py         # 多账号组配置读写与旧结构兼容
└── format.py         # 纯文本↔Quill、HTML→纯文本、时间戳格式化
```

---

## 已知限制

| 限制 | 说明 |
| ---- | ---- |
| 待审核视频不可编辑 | `video/update` 对待审核稿件返回"系统繁忙"（服务端锁定），发布后可更新 |
| 视频投稿删除 | `POST /v1/contribute/video/delete` 实测可用（接口未在官方文档公开），`mfuns_delete(target_type=video)` 已接入 |
| 收藏取消需 list_id | 收藏夹枚举未暴露为工具，`remove` 需提供 list_id |
| 官方全站动态流为空 | `/v1/feeds/list` 对该账号无可展示内容，动态流建议用 `latest` 模式 |
| 私信空记录 | 早期探测产生的空消息无法删除（无删除接口） |
| 上传任务断点续传 | 任务状态持久化到 `logs/upload_tasks/<task_id>/task.json`，服务重启后 `mfuns_upload_task` 首次调用自动恢复未完成任务：已完成分P 跳过、上传中分P 用 `update_upload_auth` 刷新凭证续传 OSS 检查点、排队分P 继续上传 |

---

## 参考

- [Mfuns 官方开放平台 API 文档](https://open.mfuns.net/api/)
- [MCP 官方文档](https://modelcontextprotocol.io/docs/2026-07-28/develop/build-server)
