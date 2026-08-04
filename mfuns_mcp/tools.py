"""MCP 工具定义（中文描述，输出为 LLM 友好的纯文本）。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer

from . import config
from .activity import activity, read_activity, set_account_resolver
from .client import MfunsClient, MfunsError, api_identity, api_login
from .format import author_name, html_to_text, quill_to_text, text_to_quill, ts_to_str
from .upload import TASK_MANAGER, run_publish_task

logger = logging.getLogger(__name__)

# 点赞资源类型（实测：评论与动态同为 4，文档标注的 3 已废弃）
_RESOURCE_TYPE = {"article": 0, "video": 1, "comment": 4, "feed": 4}
_ARTICLE_TYPE = {"article": 0, "video": 1}
_NOTIFY_TYPE = {"like": 1, "comment": 2, "mention": 3}
_STATUS_TEXT = {
    0: "草稿",
    1: "已发布",
    2: "待审核",
    3: "锁定",
    4: "退回修改",
    5: "定时发布",
}


# ---- Activity Log 目标/动作派生（装饰器用）----

def _target_user(kw: dict) -> dict:
    return {"type": "user", "id": kw.get("user_id")}


def _target_comment_obj(kw: dict) -> dict:
    return {"type": kw.get("target_type") or "article", "id": kw.get("target_id")}


def _target_react_obj(kw: dict) -> dict:
    return {"type": kw.get("resource_type") or "article", "id": kw.get("resource_id")}


def _target_browse(kw: dict) -> dict:
    mode = kw.get("mode") or "recommend"
    if mode == "category" and kw.get("category_id"):
        return {"type": "category", "id": kw["category_id"]}
    return {"type": "feed", "id": mode}


def _target_submission(kw: dict) -> dict:
    target: dict = {"type": kw.get("type") or "article"}
    if kw.get("contribute_id"):
        target["id"] = kw["contribute_id"]
    return target


def _target_upload_task(kw: dict) -> dict:
    target: dict = {"type": "upload_task"}
    if kw.get("task_id"):
        target["id"] = kw["task_id"]
    return target


async def _account_remove(client: MfunsClient, account_id: str | None) -> str:
    """移除账号；若移除的是当前账号，回退到配置的当前账号。"""
    if not account_id:
        return "错误: remove 需提供 account_id"
    if not config.get_account(account_id):
        return f"错误: 账号不存在: {account_id}（可用 mfuns_account_list 查看）"
    removed_current = client.account_id == account_id
    config.remove_account(account_id)
    if removed_current:
        client.reset_to_current()
        back = client.account_id or "（无账号）"
        return f"已移除账号 {account_id}（原为当前账号，已回退至 {back}）"
    return f"已移除账号 {account_id}"


async def _resolve_category(client: MfunsClient, category_id: int | None) -> tuple[int, str | None]:
    """解析投稿分区：父级分区自动落到第一个叶子子分区。

    Returns:
        (解析后的叶子分区 ID, 分区名)；category_id 缺省时抛 MfunsError（内联可投稿分区提示）。
    """
    data = await client.get("/v1/category/all")
    cats = data if isinstance(data, list) else (data or {}).get("list") or []

    def find(items, cid):
        for c in items or []:
            if c.get("id") == cid:
                return c
            r = find(c.get("children"), cid)
            if r:
                return r
        return None

    def first_leaf(c: dict):
        children = c.get("children") or []
        if not children:
            return c
        return first_leaf(children[0])

    if category_id is None:
        tops = [c.get("name") for c in cats if isinstance(c, dict) and c.get("name")]
        raise MfunsError(
            -1,
            "请指定 category_id（可投稿的叶子分区；顶级分区有: "
            + "、".join(str(t) for t in tops)
            + "，可用 mfuns_categories 查看完整分区树）",
        )
    cat = find(cats, category_id)
    if not cat:
        raise MfunsError(-1, f"分类 {category_id} 不存在（可用 mfuns_categories 查看分区树）")
    leaf = first_leaf(cat)
    resolved = int(leaf.get("id"))
    if resolved != category_id:
        return resolved, f"{cat.get('name')}为父级分区，已自动选择叶子分区「{leaf.get('name')}」({resolved})"
    return resolved, None


def _fmt_err(e: Exception) -> str:
    if isinstance(e, MfunsError):
        return f"错误({e.code}): {e.msg}"
    if isinstance(e, httpx.HTTPError):
        return f"网络错误: {e}"
    return f"错误: {e}"


def _direct_id(v: Any) -> Any:
    """VOD 视频库记录 ID 转整数（官方网页端提交数字 id；字符串会被审核判定"视频或信息失效"驳回）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _ensure_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        lst = data.get("list") or data.get("items")
        if isinstance(lst, list):
            return lst
    return []


def _item_line(it: dict, base_path: str) -> str:
    aid = it.get("id") or it.get("resource_id") or ""
    title = (it.get("title") or "无标题").strip()
    summary = (it.get("summary") or "").strip()
    line = (
        f"- [{aid}] {title} | 作者: {author_name(it)}"
        f" | 评论: {it.get('comment_count', '?')} | 赞: {it.get('like_count', '?')}"
    )
    if summary:
        line += f"\n  摘要: {summary[:80]}"
    return line + f"\n  链接: https://m.mfuns.net/{base_path}/{aid}"


async def _comment_block(client: MfunsClient, c: dict, with_replies: bool) -> str:
    body = html_to_text(c.get("content") or "")
    if not body and c.get("is_delete"):
        body = "(评论已删除)"
    name = author_name(c)
    line = (
        f"- 楼层{c.get('floor_num', '?')} (评论ID {c.get('id', '?')}) {name}: {body}"
        f" (赞 {c.get('like_count', '?')} | 回复 {c.get('reply_count', '?')}"
        f" | {ts_to_str(c.get('created_at'))})"
    )
    if with_replies and (c.get("reply_count") or 0) > 0:
        try:
            replies = _ensure_list(
                await client.get(
                    "/v1/comment/reply_list", comment_id=c.get("id"), page=1, html=1
                )
            )[:20]
            for r in replies:
                line += (
                    f"\n    └ {author_name(r)}: {html_to_text(r.get('content') or '')}"
                    f" (赞 {r.get('like_count', '?')} | {ts_to_str(r.get('created_at'))})"
                )
        except Exception:
            pass
    return line


def _like_count(data: dict) -> str:
    ls = data.get("like_status")
    if isinstance(ls, dict):
        return (ls.get("like") or {}).get("count", "?")
    return "?"


def _category_name(data: dict) -> str:
    cat = data.get("category")
    if isinstance(cat, dict):
        return cat.get("name") or ""
    return str(cat) if cat else ""


async def _detail_lines(client: MfunsClient, rtype: str, rid: int) -> tuple[list[str], int | None]:
    """读取内容详情（不含评论区），返回 (行列表, 评论区 area_id)。"""
    if rtype == "article":
        data = await client.get("/v1/article/get", id=rid, html=1)
        if not isinstance(data, dict) or not data.get("article"):
            raise MfunsError(-1, "文章不存在或无法访问")
        art = data["article"]
        tags = data.get("tag") or data.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        lines = [
            f"标题: {art.get('title') or '无标题'}",
            f"作者: {author_name(data)} | 发布时间: {ts_to_str(art.get('created_at'))}",
            f"分类: {_category_name(data) or '未知'} | 浏览: {data.get('view_count', '?')}"
            f" | 赞: {_like_count(data)} | 收藏: {data.get('favorite_count', '?')}"
            f" | 打赏: {data.get('reward_count', '?')}",
            f"链接: https://m.mfuns.net/article/{rid}",
        ]
        if tags:
            lines.append("标签: " + "、".join(str(t) for t in tags))
        lines.append("---正文---")
        lines.append(html_to_text(art.get("content") or "") or "(无正文)")
        return lines, art.get("comment_area_id")
    if rtype == "video":
        data = await client.get("/v1/video/get", id=rid, html=1)
        if not isinstance(data, dict) or not data.get("id"):
            raise MfunsError(-1, "视频不存在或无法访问")
        tags = data.get("tags") or data.get("tag") or []
        if isinstance(tags, str):
            tags = [tags]
        dur = data.get("duration") or 0
        cc = data.get("comments")
        if isinstance(cc, dict):
            cc = cc.get("floor_count", "?")
        lines = [
            f"标题: {data.get('title') or '无标题'}",
            f"作者: {author_name(data)} | 发布时间: {ts_to_str(data.get('published_at') or data.get('created_at'))}",
            f"分类: {_category_name(data) or '未知'} | 播放: {data.get('view_count', '?')}"
            f" | 赞: {_like_count(data)} | 评论: {cc}"
            f" | 收藏: {data.get('favorite_count', '?')} | 时长: {int(dur // 60)}分{int(dur % 60)}秒",
            f"链接: https://m.mfuns.net/video/{rid}",
        ]
        if tags:
            lines.append("标签: " + "、".join(str(t) for t in tags))
        vids = data.get("videos") or []
        if isinstance(vids, list) and vids:
            lines.append("分P: " + ", ".join(str(v.get("title") or "?") for v in vids))
        lines.append("---简介---")
        lines.append(html_to_text(data.get("content") or "") or "(无简介)")
        return lines, data.get("comment_area_id")
    # feed
    data = await client.get("/v1/feeds/get", id=rid, html=1)
    if not isinstance(data, dict) or not data.get("id"):
        raise MfunsError(-1, "动态不存在或无法访问")
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    lines = [
        f"动态 #{rid}",
        f"作者: {author_name(data)} | 发布: {ts_to_str(data.get('created_at'))}"
        f" | 阅读: {data.get('views', '?')} | 赞: {_like_count(data)}"
        f" | 评论: {data.get('floor_count', '?')}",
        f"链接: https://m.mfuns.net/feed/{rid}",
    ]
    if tags:
        lines.append("标签: " + "、".join(str(t) for t in tags))
    lines.append("---内容---")
    lines.append(html_to_text(data.get("content") or "") or "(无内容)")
    return lines, data.get("comment_area_id")


def register_tools(mcp: MCPServer) -> None:
    """注册全部 MCP 工具。"""
    client = MfunsClient()
    set_account_resolver(lambda: client.account_id)

    @mcp.tool()
    @activity("browse", _target_browse)
    async def mfuns_browse(
        mode: str = "recommend",
        category_id: int | None = None,
        limit: int = 20,
        content_type: str = "all",
    ) -> str:
        """浏览 Mfuns 社区内容流：推荐、热门、全站动态、分类帖子或最新动态。

        Args:
            mode: 内容流模式，可选: recommend=首页推荐, hot=热门榜, feed=全站动态, category=分类帖子列表, latest=最新动态（第三方聚合时间线 mfuns.wgen.top，返回 LLM 优化的 Markdown）
            category_id: 分类 ID，mode=category 时必填（如 51=交友专区，49=站内互动），其它模式忽略
            limit: 返回条数上限，默认 20，最大 100
            content_type: 内容类型过滤，仅 mode=latest 有效: all=全部（默认）, feed=动态, video=视频, article=文章
        """
        try:
            limit = max(1, min(limit, 100))
            if mode == "latest":
                if content_type not in ("all", "feed", "video", "article"):
                    return "错误: content_type 仅支持 all / feed / video / article"
                text = await client.get_text(
                    "https://mfuns.wgen.top/llm/latest",
                    {"type": content_type, "limit": limit, "format": "markdown"},
                )
                title = "all" if content_type == "all" else content_type
                return f"Mfuns 最新动态（{title}，第三方聚合 mfuns.wgen.top）:\n\n{text.strip()}"
            if mode == "recommend":
                data = await client.get(
                    "/v1/recommend/get",
                    category=category_id if category_id else -1,
                    size=limit,
                )
                base = "article"
            elif mode == "hot":
                data = await client.get("/v1/leaderboards/hot")
                base = "article"
            elif mode == "feed":
                data = await client.get("/v1/feeds/list", start_id=0)
                base = "feed"
            elif mode == "category":
                if not category_id:
                    return "错误: mode=category 时需提供 category_id"
                data = await client.get(
                    "/v1/category/list", cid=category_id, page=1, size=limit
                )
                base = "article"
            else:
                return "错误: mode 仅支持 recommend / hot / feed / category / latest"
            items = _ensure_list(data)[:limit]
            if not items:
                return "没有找到内容"
            lines = [f"Mfuns 内容流（{mode}，显示 {len(items)} 条）:"]
            lines.extend(_item_line(it, base) for it in items)
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("read", lambda kw: {"type": kw.get("resource_type") or "article", "id": kw.get("resource_id")})
    async def mfuns_read(
        resource_id: int,
        resource_type: str = "article",
        comment_depth: int = 1,
        comment_limit: int = 30,
    ) -> str:
        """读取内容详情与评论区（帖子/视频/动态，回复前必读）。

        Args:
            resource_type: 资源类型，可选: article=帖子（默认）, video=视频, feed=动态
            resource_id: 内容 ID（文章/视频/动态 ID）
            comment_depth: 评论层级，1=只看一楼评论（默认），2=一楼评论加回复
            comment_limit: 返回的评论条数上限，默认 30
        """
        try:
            if resource_type not in ("article", "video", "feed"):
                return "错误: resource_type 仅支持 article / video / feed"
            lines, area_id = await _detail_lines(client, resource_type, resource_id)
            if area_id:
                comments = await client.get(
                    "/v1/comment/list", area_id=area_id, page=1, order="desc", html=1
                )
                clist = _ensure_list(comments)[:comment_limit]
                lines.append(f"---评论（显示 {len(clist)} 条）---")
                for c in clist:
                    lines.append(await _comment_block(client, c, comment_depth >= 2))
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("search", lambda kw: {"type": kw.get("type") or "resource", "id": kw.get("query")})
    async def mfuns_search(query: str, type: str = "resource", size: int = 20) -> str:
        """搜索 Mfuns 社区的内容（文章/视频）或用户。

        Args:
            query: 搜索关键词
            type: 搜索类型，可选: resource=内容（默认）, user=用户
            size: 返回条数上限，默认 20，最大 50
        """
        try:
            size = max(1, min(size, 50))
            if type == "user":
                data = await client.get("/v1/search/user", user=query, size=size)
                users = _ensure_list(data)
                if not users:
                    return f"没有找到用户: {query}"
                lines = [
                    f"用户搜索结果（共 {data.get('total', len(users))} 个，显示 {len(users)} 条）:"
                ]
                for u in users:
                    lines.append(
                        f"- [{u.get('id')}] {u.get('name')} | 粉丝: {u.get('fans', '?')}"
                        f" | 简介: {(u.get('info') or u.get('bio') or '')[:60]}"
                    )
                return "\n".join(lines)
            try:
                data = await client.get(
                    "/v1/search/resource",
                    text=query,
                    type=-1,
                    page=1,
                    size=size,
                    sort="all",
                )
            except MfunsError as e:
                if e.code in (401, 4031):
                    return (
                        f"错误({e.code}): {e.msg}（内容搜索需要登录，"
                        "请配置 MFUNS_ACCOUNT / MFUNS_PASSWORD 环境变量或 config.json 账号）"
                    )
                raise
            items = _ensure_list(data)
            if not items:
                return f"没有找到相关内容: {query}"
            lines = [
                f"内容搜索结果（共 {data.get('total', len(items))} 个，显示 {len(items)} 条）:"
            ]
            for it in items:
                base = "video" if it.get("type") == 1 else "article"
                lines.append(_item_line(it, base))
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("get_user", _target_user)
    async def mfuns_get_user(user_id: int) -> str:
        """获取用户资料（互动前了解对象：判断是新人还是老用户）。

        Args:
            user_id: 用户 ID
        """
        try:
            data = await client.get("/v1/user/get_user", id=user_id)
            if not isinstance(data, dict):
                return "错误: 未找到该用户"
            gender = {0: "未知", 1: "男", 2: "女"}.get(data.get("gender"), data.get("gender") or "未知")
            badges = data.get("badges")
            lines = [
                f"用户: {data.get('name') or f'用户{user_id}'} (ID {user_id})",
                f"简介: {data.get('bio') or '(无)'}",
                f"性别: {gender} | 累计被赞: {data.get('total_likes_count', '?')}"
                f" | 累计浏览: {data.get('total_views_count', '?')}",
                f"加入时间: {ts_to_str(data.get('created_at'))}",
            ]
            if isinstance(badges, list) and badges:
                lines.append(
                    "徽章: " + ", ".join(str(b if isinstance(b, int) else b.get("name", b)) for b in badges)
                )
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(
        lambda kw: "create" if kw.get("target_type") != "comment" else "create_reply",
        _target_comment_obj,
    )
    async def mfuns_comment(target_type: str, target_id: int, content: str) -> str:
        """发表评论或回复评论（内容为纯文本，自动转换为论坛格式）。

        Args:
            target_type: 评论对象，可选: article=评论帖子, video=评论视频, feed=评论动态, comment=回复评论
            target_id: 文章/视频/动态 ID（target_type=article/video/feed）或评论 ID（target_type=comment）
            content: 评论/回复内容
        """
        try:
            if not content.strip():
                return "错误: 内容不能为空"
            quill = text_to_quill(content)
            if target_type in ("article", "video", "feed"):
                if target_type == "article":
                    area_id = await client.article_area_id(target_id)
                elif target_type == "video":
                    area_id = await client.video_area_id(target_id)
                else:
                    area_id = await client.feed_area_id(target_id)
                data = await client.post(
                    "/v1/comment/create",
                    json_body={"area_id": area_id, "content": quill, "html": 1},
                )
                floor = data.get("floor_num") if isinstance(data, dict) else None
                return f"评论成功（{target_type} {target_id}，楼层 {floor or '?'}）"
            if target_type == "comment":
                await client.post(
                    "/v1/comment/create_reply",
                    json_body={"comment_id": target_id, "content": quill},
                )
                return f"回复成功（评论 {target_id}）"
            return "错误: target_type 仅支持 article / video / feed / comment"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("categories", lambda kw: {"type": "category"})
    async def mfuns_categories() -> str:
        """查询投稿分区树（发布帖子/视频前用 mfuns_categories 选 category_id）。

        Args:
            无参数：返回完整分区树，叶子分区可投稿，父级分区仅作导航
        """
        try:
            data = await client.get("/v1/category/all")
            cats = data
            if isinstance(cats, dict):
                cats = cats.get("list") or cats.get("children") or []
            if not cats:
                return "暂无分区数据"
            lines = ["投稿分区（叶子分区可投稿，父级分区不可直接投稿）:"]

            def walk(items, parent_name: str, depth: int) -> None:
                for c in items or []:
                    if not isinstance(c, dict):
                        continue
                    name = c.get("name") or "?"
                    children = c.get("children") or []
                    mark = "可投稿" if not children else "父级分区"
                    lines.append(
                        f"{'  ' * depth}[{c.get('id')}] {name}"
                        f"（{parent_name or '顶级'}） - {mark}"
                    )
                    walk(children, name, depth + 1)

            walk(cats, "", 0)
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("create_post", lambda kw: {"type": "post"})
    async def mfuns_create_post(
        title: str,
        content: str,
        category_id: int | None = None,
        tags: list[str] | None = None,
        copyright: int = 2,
        cover: str | None = None,
        draft: bool = False,
    ) -> str:
        """发布文章帖子（纯文本或 Markdown 正文，服务端自动转换）。

        Args:
            title: 标题（最长 30 字）
            content: 正文，支持纯文本或 Markdown
            category_id: 分类 ID（须为叶子分区；传父级分区会自动落到其第一个叶子子分区，缺省则返回可投稿分区提示；如 44=科技综合，51=交友专区）
            tags: 标签列表，最多 10 个
            copyright: 版权，2=原创（默认），1=转载，0=其他
            cover: 封面图 https 外链（可选）
            draft: 是否只存草稿，默认 False 直接投稿
        """
        try:
            cid, note = await _resolve_category(client, category_id)
            payload: dict = {
                "cid": cid,
                "title": title,
                "content": content,
                "content_format": "markdown",
                "copyright": copyright,
                "draft": draft,
            }
            if tags:
                payload["tags"] = ",".join(tags[:10])
            if cover:
                payload["cover"] = cover
            data = await client.post("/v1/contribute/article/create", json_body=payload)
            con = (data or {}).get("contribute") or {}
            msg = f"投稿成功: 投稿ID {con.get('id', '?')}，状态: {_STATUS_TEXT.get(con.get('status'), con.get('status'))}"
            if note:
                msg += f"\n提示: {note}"
            if con.get("resource_id"):
                msg += f"，链接: https://m.mfuns.net/article/{con['resource_id']}"
            return msg
        except MfunsError as e:
            if "分区" in e.msg:
                return f"错误({e.code}): {e.msg}"
            return _fmt_err(e)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("create_feed", lambda kw: {"type": "feed"})
    async def mfuns_create_feed(
        content: str,
        images: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """发布动态（Feed，全站动态流可见）。

        Args:
            content: 动态内容（纯文本，自动转换为论坛格式）
            images: 图片 URL 列表（可选）
            tags: 标签列表（可选，最多 10 个）
        """
        try:
            if not content.strip():
                return "错误: 内容不能为空"
            payload: dict = {
                "content": text_to_quill(content),
                "images": json.dumps(images or [], ensure_ascii=False),
            }
            if tags:
                payload["tags"] = ",".join(tags[:10])
            data = await client.post("/v1/feeds/create", json_body=payload)
            fid = data.get("id") if isinstance(data, dict) else None
            return f"动态发布成功（feed ID {fid or '?'}）"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(
        lambda kw: f"delete_{kw.get('target_type')}",
        lambda kw: {"type": kw.get("target_type"), "id": kw.get("target_id")},
    )
    async def mfuns_delete(target_type: str, target_id: int) -> str:
        """删除内容（动态/评论/文章投稿，仅限本人内容）。

        Args:
            target_type: 删除对象，可选: feed=动态, comment=评论, article=文章投稿
            target_id: 对象 ID（动态 ID / 评论 ID / 投稿 ID）
        """
        try:
            if target_type == "feed":
                await client.post("/v1/feeds/delete", json_body={"id": target_id})
                return f"已删除动态 {target_id}"
            if target_type == "comment":
                await client.post(
                    "/v1/comment/delete", json_body={"comment_id": target_id}
                )
                return f"已删除评论 {target_id}"
            if target_type == "article":
                await client.post(
                    "/v1/contribute/article/delete",
                    json_body={"contribute_id": target_id},
                )
                return f"已删除文章投稿 {target_id}"
            return "错误: target_type 仅支持 feed / comment / article"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(
        lambda kw: f"message_{kw.get('action') or 'list'}",
        lambda kw: {"type": "message", "id": kw.get("user_id") or "list"},
    )
    async def mfuns_messages(
        action: str = "list",
        user_id: int | None = None,
        content: str | None = None,
    ) -> str:
        """查看私信会话列表 / 读取聊天记录 / 发送私信。

        Args:
            action: 操作，可选: list=会话列表（默认）, read=读取聊天记录, send=发送私信
            user_id: 对方用户 ID（action=read/send 时必填）
            content: 私信内容（action=send 时必填，纯文本）
        """
        try:
            if action == "send":
                if not user_id or not content:
                    return "错误: send 需提供 user_id 和 content"
                # 实测：必须 form 编码 + msg 字段（JSON + message 会"发送成功"但内容落空）
                await client.post(
                    "/v1/message/send",
                    form={"to_uid": user_id, "msg": content},
                )
                return f"私信已发送（用户 {user_id}）"
            if action == "read":
                if not user_id:
                    return "错误: action=read 需提供 user_id"
                data = await client.get("/v1/message/record", uid=user_id)
                items = _ensure_list(data)
                me = await client.get("/v1/user/info")
                my_name = ((me or {}).get("user") or {}).get("name") or "我"
                peer = await client.get("/v1/user/get_user", id=user_id)
                peer_name = peer.get("name") if isinstance(peer, dict) else str(user_id)
                if not items:
                    return f"与 {peer_name} 暂无私信记录"
                lines = [f"与 {peer_name} 的私信记录（{len(items)} 条）:"]
                for it in items:
                    d = it.get("data") or {}
                    msg = quill_to_text(d.get("message") or "")
                    who = my_name if it.get("uid") == (me or {}).get("user", {}).get("id") else peer_name
                    lines.append(f"- {ts_to_str(d.get('time'))} {who}: {msg}")
                return "\n".join(lines)
            data = await client.get("/v1/message/list")
            convs = _ensure_list(data)
            if not convs:
                return "暂无私信会话"
            lines = [f"私信会话（{len(convs)} 个）:"]
            for c in convs:
                u = c.get("user") or {}
                last = (c.get("last_msg") or {}).get("data") or {}
                msg = quill_to_text(last.get("message") or "")
                lines.append(
                    f"- [{u.get('id', '?')}] {u.get('name', '未知')}"
                    f" | 未读 {c.get('no_read', 0)}"
                    f" | 最近({ts_to_str(last.get('time'))}): {msg}"
                )
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(lambda kw: kw.get("action") or "like", _target_react_obj)
    async def mfuns_react(
        resource_type: str, resource_id: int, action: str = "like"
    ) -> str:
        """对内容点赞 / 取消点赞 / 点踩。

        Args:
            resource_type: 资源类型，可选: article=文章, video=视频, comment=评论, feed=动态
            resource_id: 资源 ID
            action: 操作，可选: like=点赞（默认）, cancel=取消点赞, dislike=点踩
        """
        try:
            rtype = _RESOURCE_TYPE.get(resource_type)
            if rtype is None:
                return "错误: resource_type 仅支持 article / video / comment / feed"
            if action not in ("like", "cancel", "dislike"):
                return "错误: action 仅支持 like / cancel / dislike"
            await client.post(
                f"/v1/like/{action}", json_body={"id": resource_id, "type": rtype}
            )
            verb = {"like": "点赞", "cancel": "取消点赞", "dislike": "点踩"}[action]
            return f"已{verb}（{resource_type} {resource_id}）"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(lambda kw: f"favorite_{kw.get('action') or 'add'}", _target_react_obj)
    async def mfuns_favorite(
        resource_id: int,
        resource_type: str = "article",
        action: str = "add",
        list_id: int | None = None,
    ) -> str:
        """收藏 / 取消收藏 / 查询收藏状态。

        Args:
            resource_id: 资源 ID
            resource_type: 资源类型，可选: article=文章, video=视频
            action: 操作，可选: add=收藏（默认）, remove=取消收藏, check=查询是否已收藏
            list_id: 收藏夹 ID；add 时不传则使用默认收藏夹，remove 时必填
        """
        try:
            rtype = _ARTICLE_TYPE.get(resource_type)
            if rtype is None:
                return "错误: resource_type 仅支持 article / video"
            if action == "check":
                data = await client.get(
                    "/v1/favorite/is_favorite",
                    resource_id=resource_id,
                    resource_type=rtype,
                )
                state = "已收藏" if (data or {}).get("is_favorite") else "未收藏"
                return f"{resource_type} {resource_id}: {state}（收藏数 {data.get('count', '?')}）"
            if action == "add":
                lid = list_id or await client.default_favorite_list_id()
                await client.post(
                    "/v1/favorite/add_favorite",
                    json_body={
                        "list_id": lid,
                        "resource_id": resource_id,
                        "type": rtype,
                    },
                )
                return f"收藏成功（{resource_type} {resource_id} -> 收藏夹 {lid}）"
            if action == "remove":
                if not list_id:
                    return "错误: remove 需提供 list_id"
                await client.post(
                    "/v1/favorite/remove_favorite_by_resource",
                    json_body={
                        "resource_id": resource_id,
                        "list_id": list_id,
                        "type": rtype,
                    },
                )
                return f"已取消收藏（{resource_type} {resource_id}）"
            return "错误: action 仅支持 add / remove / check"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("notify", lambda kw: {"type": "notification", "id": kw.get("type")})
    async def mfuns_notifications(type: str = "comment", page: int = 1) -> str:
        """获取通知消息（收到的赞/评论/提及）与未读计数。

        Args:
            type: 通知类型，可选: like=收到的赞, comment=收到的评论/回复（默认）, mention=@提及
            page: 页码，默认 1
        """
        try:
            ntype = _NOTIFY_TYPE.get(type)
            if ntype is None:
                return "错误: type 仅支持 like / comment / mention"
            counts = (await client.get("/v1/notify/count")) or {}
            data = await client.get("/v1/notify/get", type=ntype, page=page)
            items = _ensure_list(data)
            lines = [
                f"未读: 赞 {counts.get('like', 0)} | 评论 {counts.get('comment', 0)}"
                f" | 提及 {counts.get('mention', 0)} | 系统 {counts.get('system', 0)}"
            ]
            if not items:
                lines.append("暂无此类通知")
            for n in items:
                p = n.get("notify_params") or {}
                text = p.get("text") or p.get("reply_text") or ""
                cid = p.get("comment_id")
                extra = f"（评论ID {cid}）" if cid is not None else ""
                lines.append(
                    f"- 用户{n.get('sender_user_id', '?')} | {ts_to_str(n.get('created_at'))}:"
                    f" {text}{extra}"
                )
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("history", lambda kw: {"type": "history", "id": kw.get("resource_type") or "all"})
    async def mfuns_history(
        resource_type: str | None = None, limit: int = 50
    ) -> str:
        """获取浏览历史（了解自己看过什么，避免重复互动）。

        Args:
            resource_type: 过滤资源类型，可选: article=文章, video=视频；不传返回全部
            limit: 返回条数上限，默认 50
        """
        try:
            params: dict = {}
            if resource_type:
                rt = _ARTICLE_TYPE.get(resource_type)
                if rt is None:
                    return "错误: resource_type 仅支持 article / video"
                params["resource_type"] = rt
            data = await client.get("/v1/history/get", **params)
            items = _ensure_list(data)[:limit]
            if not items:
                return "暂无浏览历史"
            lines = [f"浏览历史（显示 {len(items)} 条）:"]
            for it in items:
                info = it.get("resource_info") or {}
                t = ts_to_str(it.get("created_at"))
                lines.append(
                    f"- [{info.get('id', '?')}] {info.get('title', '无标题')}"
                    f" | 作者: {author_name(info)}" + (f" | 浏览时间: {t}" if t else "")
                )
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("publish_video_upload", lambda kw: {"type": "video"})
    async def mfuns_publish_video_upload(
        file_path: str,
        title: str,
        content: str = "",
        category_id: int | None = None,
        cover: str = "",
        copyright: int = 0,
        tags: list[str] | None = None,
        extra_files: list[str] | None = None,
        async_mode: bool | None = None,
    ) -> str:
        """本地上传视频并投稿（阿里云 VOD 断点续传，支持分P：extra_files 为 P2 起的本地文件）。

        支持后台任务模式：创建后立即返回 task_id 与任务摘要，用 mfuns_upload_task(task_id=...) 查询进度；
        多分P 并行上传，全部完成并校验后才自动投稿，避免长上传导致发布超时失败。

        Args:
            file_path: 本地视频文件路径（P1，支持 mp4/mov/mkv/flv/avi/wmv/webm/mpeg4/ts/mpg/rm/rmvb/m4v）
            title: 标题（最长 30 字）
            content: 简介（纯文本）
            category_id: 分类 ID（须为叶子分区；传父级分区会自动落到其第一个叶子子分区，缺省默认 1 动画>MMD.3D 请显式指定）
            cover: 封面：https 外链或本地图片路径（本地图自动上传为 /static/xxx），缺省使用平台默认封面
            copyright: 版权，0=其他（默认，适合转载），1=转载，2=原创
            tags: 标签列表，最多 10 个
            extra_files: 额外分P 的本地文件路径列表（P2、P3…）
            async_mode: None=自动（多文件时后台异步）, True=强制后台任务, False=强制同步（单文件默认同步）
        """
        try:
            paths = [Path(file_path)] + [Path(p) for p in (extra_files or [])]
            for p in paths:
                if not p.is_file():
                    return f"错误: 文件不存在 {p}"
                if p.stat().st_size <= 0:
                    return f"错误: 文件为空 {p}"
            cid, note = await _resolve_category(client, category_id)
            background = async_mode if async_mode is not None else len(paths) > 1
            task = TASK_MANAGER.create(
                client.account_id,
                title,
                [p.name for p in paths],
                [p.stat().st_size for p in paths],
            )
            task.files = [str(p) for p in paths]
            task.content = content
            task.cover = cover
            task.copyright_ = copyright
            task.tags = tags or []
            task.cid = cid
            task.note = note or ""
            task.persist()
            handle = asyncio.create_task(run_publish_task(client, task))
            TASK_MANAGER.track(task.task_id, handle)
            if background:
                tip = "后台任务已创建，可用 mfuns_upload_task(task_id=...) 查询进度"
                if task.note:
                    tip += f"（{task.note}）"
                return task.describe() + f"\n提示: {tip}"
            await handle
            if task.status == "done":
                msg = f"视频上传投稿成功: 投稿ID {task.contribute_id}，状态: {_STATUS_TEXT.get(task.contribute_status, task.contribute_status)}（{len(paths)} 分P）"
                if task.note:
                    msg += f"\n提示: {task.note}"
                return msg
            return f"错误: {task.error or '未知错误'}"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(lambda kw: kw.get("action") or "status", _target_upload_task)
    async def mfuns_upload_task(action: str = "status", task_id: str | None = None) -> str:
        """查询后台视频上传任务进度（多文件异步投稿用）。

        服务重启后首次调用会自动恢复磁盘上未完成的任务并继续上传（断点续传）。

        Args:
            action: 操作，可选: status=任务状态（默认）, list=任务列表
            task_id: 任务 ID（action=status 时必填）
        """
        restored = await TASK_MANAGER.ensure_resumed()
        if action == "list":
            tasks = TASK_MANAGER.list()
            if not tasks:
                return "暂无上传任务"
            lines = [f"上传任务列表（最近 {len(tasks)} 个）:"]
            if restored:
                lines.append(f"提示: 已恢复 {restored} 个中断任务并继续上传")
            for t in tasks:
                done = t.uploaded_count()
                lines.append(
                    f"- {t.task_id} [{t.status}] {t.title}（{done}/{len(t.parts)} 分P）"
                )
            return "\n".join(lines)
        if not task_id:
            return "错误: status 需提供 task_id（可用 action=list 查看任务）"
        task = TASK_MANAGER.get(task_id)
        if not task:
            return f"错误: 任务不存在或已过期: {task_id}"
        return task.describe()

    @mcp.tool()
    @activity("publish_video_link", lambda kw: {"type": "video"})
    async def mfuns_publish_video_link(
        title: str,
        video_url: str,
        content: str = "",
        category_id: int | None = None,
        cover: str = "",
        copyright: int = 0,
        tags: list[str] | None = None,
        parts: list[str] | None = None,
    ) -> str:
        """外链视频投稿（视频直链 URL，支持分P：parts 为 P2 起的外链 URL 列表）。

        Args:
            title: 标题（最长 30 字）
            video_url: 视频直链 URL（P1，https）
            content: 简介（纯文本）
            category_id: 分类 ID（须为叶子分区；传父级分区会自动落到其第一个叶子子分区，缺省默认 1 动画>MMD.3D 请显式指定）
            cover: 封面图 https 外链（视频投稿必填）
            copyright: 版权，0=其他（默认，适合转载），1=转载，2=原创
            tags: 标签列表，最多 10 个
            parts: 额外分P 的外链 URL 列表（P2、P3…）
        """
        try:
            if not cover:
                return "错误: 视频投稿需提供封面图 https 外链（cover 参数）"
            videos: list[dict] = [{"type": "link", "content": video_url, "title": title}]
            for i, url in enumerate(parts or [], start=2):
                videos.append({"type": "link", "content": url, "title": f"P{i}"})
            cid, note = await _resolve_category(client, category_id)
            payload: dict = {
                "cid": cid,
                "title": title,
                "content": text_to_quill(content or ""),
                "video": json.dumps(videos, ensure_ascii=False),
                "copyright": copyright,
            }
            if tags:
                payload["tags"] = ",".join(tags[:10])
            if cover:
                payload["cover"] = cover
            data = await client.post("/v1/contribute/video/create", json_body=payload)
            con = (data or {}).get("contribute") or {}
            msg = f"视频投稿成功: 投稿ID {con.get('id', '?')}，状态: {_STATUS_TEXT.get(con.get('status'), con.get('status'))}"
            if note:
                msg += f"\n提示: {note}"
            return msg
        except MfunsError as e:
            if "分区" in e.msg:
                return f"错误({e.code}): {e.msg}"
            return _fmt_err(e)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(lambda kw: kw.get("action") or "list", _target_submission)
    async def mfuns_manage_submission(
        type: str = "article",
        action: str = "list",
        contribute_id: int | None = None,
        page: int = 1,
        size: int = 20,
        status: int | None = None,
        title: str | None = None,
        content: str | None = None,
        category_id: int | None = None,
        tags: list[str] | None = None,
        cover: str | None = None,
        draft: bool | None = None,
        video_url: str | None = None,
        video_id: str | None = None,
        parts: list[str] | None = None,
        parts_meta: list[dict] | None = None,
        copyright: int | None = None,
    ) -> str:
        """管理我的投稿：查看列表/详情 / 更新投稿（文章或视频，支持分P编辑）。

        Args:
            type: 稿件类型，可选: article=文章, video=视频
            action: 操作，可选: list=列表（默认）, get=详情, update=更新
            contribute_id: 投稿 ID（get/update 必填）
            page: 页码，默认 1
            size: 每页数量，默认 20，最大 100
            status: 状态过滤（list 可选）: 0草稿 1已发布 2待审核 3锁定 4退回修改 5定时发布
            title: 新标题（update 必填）
            content: 新正文/简介（update 必填，纯文本或 Markdown）
            category_id: 新分类 ID（update 必填）
            tags: 新标签（update 可选）
            cover: 新封面（update 可选）
            draft: 更新后是否保持草稿（文章 update 可选，不传则进入审核队列；草稿稿件更新建议传 true）
            video_url: P1 外链直链 URL（视频 update 时提供 video_url 或 video_id 之一，作为 P1）
            video_id: P1 本地上传 VOD 库记录 ID（视频 update 时提供 video_url 或 video_id 之一，作为 P1；数字 id 必须为整数语义，工具自动转整数）
            parts: 分P 列表（P2 起）；与 P1 同类型（video_url 时为外链 URL，video_id 时为 VOD 库 ID）；
                   仅提供 parts 且未提供 P1 时，自动追加到现有稿件末尾（作为 link 外链分P）
            parts_meta: 分P meta 列表（可选，与全部分P 一一对应，第 1 项为 P1 的 meta）；仅 video_id 本地上传类型有意义，供编辑页显示大小/时长
            copyright: 新版权（update 可选，文章默认 2，视频默认 0）
        """
        try:
            atype = _ARTICLE_TYPE.get(type)
            if atype is None:
                return "错误: type 仅支持 article / video"
            if action == "list":
                params: dict = {"type": atype, "page": page, "size": min(size, 100)}
                if status is not None:
                    params["status"] = status
                data = await client.get("/v1/contribute/list", **params)
                items = _ensure_list(data)
                if not items:
                    return "暂无投稿"
                lines = [
                    f"我的投稿（{type}，共 {data.get('total', len(items))} 条，显示 {len(items)} 条）:"
                ]
                for it in items:
                    lines.append(
                        f"- 投稿ID {it.get('id')} | 资源ID {it.get('resource_id') or '-'}"
                        f" | {it.get('title')} | 状态: {_STATUS_TEXT.get(it.get('status'), it.get('status'))}"
                        f" | 创建: {ts_to_str(it.get('created_at'))}"
                    )
                return "\n".join(lines)
            if action == "get":
                if not contribute_id:
                    return "错误: get 需提供 contribute_id"
                data = await client.get("/v1/contribute/get", contribute_id=contribute_id)
                con = (data or {}).get("contribute") or {}
                if not con:
                    return "错误: 投稿不存在"
                lines = [
                    f"投稿ID {con.get('id')} | 状态: {_STATUS_TEXT.get(con.get('status'), con.get('status'))}"
                    f" | 资源ID {con.get('resource_id') or '-'}",
                    f"标题: {con.get('title')}",
                    f"分类: {con.get('category_id')} | 标签: {'、'.join(con.get('tags') or []) or '无'}",
                ]
                if con.get("cover"):
                    lines.append(f"封面: {con['cover']}")
                videos = con.get("videos") or []
                if isinstance(videos, list) and videos:
                    lines.append("分P:")
                    lines.extend(
                        f"  P{i} ({v.get('type')}): {v.get('title')} | {v.get('content')}"
                        for i, v in enumerate(videos, start=1)
                    )
                return "\n".join(lines)
            if action == "update":
                if not contribute_id or not title or content is None or not category_id:
                    return "错误: update 需提供 contribute_id / title / content / category_id"
                payload: dict = {
                    "contribute_id": contribute_id,
                    "cid": category_id,
                    "title": title,
                    "copyright": copyright if copyright is not None else (2 if type == "article" else 0),
                }
                if tags:
                    payload["tags"] = ",".join(tags[:10])
                if cover:
                    payload["cover"] = cover
                if type == "article":
                    payload["content"] = content
                    payload["content_format"] = "markdown"
                    if draft is not None:
                        payload["draft"] = draft
                    data = await client.post(
                        "/v1/contribute/article/update", json_body=payload
                    )
                else:
                    if video_url and video_id:
                        return "错误: video_url 与 video_id 不能同时提供（P1 只能选外链或本地上传）"
                    if not video_url and not video_id and not parts:
                        return "错误: 视频 update 需提供 video_url/video_id（P1）或 parts（追加分P）"
                    if not cover:
                        return "错误: 视频 update 需提供 cover 封面图 https 外链"
                    if video_url or video_id:
                        videos: list[dict] = [
                            {
                                "type": "direct" if video_id else "link",
                                "content": _direct_id(video_id) if video_id else video_url,
                                "title": title,
                            }
                        ]
                        for i, p in enumerate(parts or [], start=2):
                            videos.append(
                                {
                                    "type": "direct" if video_id else "link",
                                    "content": _direct_id(p) if video_id else p,
                                    "title": f"P{i}",
                                }
                            )
                        for j, item in enumerate(videos):
                            if parts_meta and j < len(parts_meta) and parts_meta[j]:
                                item["meta"] = parts_meta[j]
                    else:
                        # 仅 parts：读取现有分P，追加为 link 外链分P
                        cur = await client.get(
                            "/v1/contribute/get", contribute_id=contribute_id
                        )
                        cur_videos = ((cur or {}).get("contribute") or {}).get("videos") or []
                        videos = [dict(v) for v in cur_videos if isinstance(v, dict)]
                        base = len(videos)
                        for i, p in enumerate(parts or [], start=base + 1):
                            videos.append({"type": "link", "content": p, "title": f"P{i}"})
                    payload["content"] = text_to_quill(content)
                    payload["video"] = json.dumps(videos, ensure_ascii=False)
                    data = await client.post(
                        "/v1/contribute/video/update", json_body=payload
                    )
                con = (data or {}).get("contribute") or {}
                st = con.get("status")
                st_text = _STATUS_TEXT.get(st) if st is not None else "已提交"
                parts_note = f"（{len(videos)} 分P）" if type == "video" and isinstance(videos, list) else ""
                return f"更新成功: 投稿ID {con.get('id', contribute_id)}，状态: {st_text}{parts_note}"
            return "错误: action 仅支持 list / get / update（删除请用 mfuns_delete）"
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("query", lambda kw: {"type": "activity", "id": kw.get("date")})
    async def mfuns_activity_log(
        date: str,
        tool: str | None = None,
        target_id: int | None = None,
        account_id: str | None = None,
    ) -> str:
        """查询 Activity Log（每次工具调用的操作记录，按账号与日期隔离的 JSON 文件）。

        Args:
            date: 日期，格式 YYYY-MM-DD（如 2026-08-02）
            tool: 可选，按工具名过滤（如 mfuns_comment）
            target_id: 可选，按影响对象 ID 过滤（如帖子/评论 ID 83888）
            account_id: 可选，指定账号（如 u_38461）；不传查询当前账号
        """
        try:
            logs = read_activity(date, account_id)
            if tool:
                logs = [x for x in logs if x.get("tool") == tool]
            if target_id is not None:
                logs = [x for x in logs if (x.get("target") or {}).get("id") == target_id]
            who = account_id or f"{client.account_id}（当前）"
            if not logs:
                return f"{who} {date} 无匹配的 Activity Log"
            lines = [f"Activity Log（{who}，{date}，共 {len(logs)} 条）:"]
            lines.extend(f"- {x.get('time')} {x.get('tool')} | {x.get('action')}" for x in logs)
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(
        "account_modify",
        lambda kw: {"type": "account", "id": kw.get("account_id") or kw.get("action")},
    )
    async def mfuns_account_modify(
        action: str = "add",
        account: str | None = None,
        password: str | None = None,
        token: str | None = None,
        api_key: str | None = None,
        account_id: str | None = None,
        user_id: int | None = None,
        user_name: str | None = None,
    ) -> str:
        """添加或移除 Mfuns 账号身份。

        Args:
            action: 操作，可选: add=添加（默认）, remove=移除
            account: 手机号或用户名（账号密码登录，验证通过自动缓存 token）
            password: 密码（与 account 搭配）
            token: 登录 token 直接导入（自动校验并填充身份信息）
            api_key: 官方开放平台密钥 mf_xxx（独立于账密/token 体系，仅作投稿接口凭证，不解析身份）
            account_id: 添加时可指定账号 ID（如 u_38461，缺省按身份自动生成或顺序编号）；remove 时必填（要移除的账号 ID）
            user_id: 可选，预填用户 ID（与 token 身份不一致会拒绝）
            user_name: 昵称（纯 api_key 添加时必填；token/账密添加时自动解析可留空）
        """
        try:
            if action == "remove":
                return await _account_remove(client, account_id)
            if action != "add":
                return "错误: action 仅支持 add / remove"
            if not (account and password) and not token and not api_key:
                return "错误: 需提供三种凭证之一（account+password / token / api_key）"
            if account and password and not token:
                token = await api_login(account, password)
            uid, name = user_id, user_name
            if token:
                uid, name = await api_identity(token)
                if user_id and uid != user_id:
                    return f"错误: 身份不匹配，token 属于用户 {uid}，与提供的 user_id={user_id} 不一致"
                if not user_name:
                    user_name = name
            # api_key 独立于账密/token 体系，不参与身份解析（user/info 拒绝 api_key），仅作投稿接口凭证
            new_id = account_id
            if not new_id:
                if uid:
                    new_id = f"u_{uid}"
                else:
                    # 纯 api_key：身份无法解析，id 直接顺序编号，昵称必填
                    if not user_name:
                        return "错误: 纯 api_key 添加无法解析身份，请提供昵称 user_name"
                    used = {a["id"] for a in config.get_accounts()}
                    n = 1
                    while f"u_{n}" in used:
                        n += 1
                    new_id = f"u_{n}"
            else:
                m = re.fullmatch(r"u_(\d+)", new_id)
                if m and uid and int(m.group(1)) != uid:
                    return f"错误: account_id {new_id} 与身份（用户 {uid}）不一致"
            if config.get_account(new_id):
                return f"错误: 账号已存在: {new_id}（可用 mfuns_account_list 查看）"
            for a in config.get_accounts():
                a_auth = a.get("auth") or {}
                if uid and (a.get("profile") or {}).get("user_id") == uid:
                    return f"错误: 用户 {uid} 已存在于账号 {a['id']}（勿重复添加）"
                if token and a_auth.get("token") == token:
                    return f"错误: 该 token 已绑定到账号 {a['id']}（勿重复导入）"
                if api_key and a_auth.get("api_key") == api_key:
                    return f"错误: 该 api_key 已绑定到账号 {a['id']}（勿重复绑定）"
                if account and a_auth.get("account") == account:
                    return f"错误: 账号 {account} 已存在于账号 {a['id']}（勿重复添加）"
            config.add_account(
                new_id,
                auth={"account": account or "", "password": password or "", "token": token or "", "api_key": api_key or ""},
                profile={"user_id": uid, "user_name": user_name or ""},
            )
            return (
                f"已添加账号 {new_id} | 用户ID: {uid or '未解析'} | 名称: {user_name or '未知'}"
                f" | token: {'有' if token else '无'} | api_key: {'有' if api_key else '无'}\n"
                f"提示: 切换请用 mfuns_account_switch(account_id={new_id})"
            )
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("account_list", lambda kw: {"type": "account"})
    async def mfuns_account_list() -> str:
        """查看 MCP 当前管理的所有账号。

        Args:
            无参数：返回账号列表，active 标记当前操作身份
        """
        try:
            accounts = config.get_accounts()
            if not accounts:
                return "暂无账号配置（请编辑 config.json 的 accounts）"
            current = client.account_id
            lines = [f"账号列表（{len(accounts)} 个）:"]
            for a in accounts:
                if not a.get("enabled"):
                    continue
                p = a.get("profile") or {}
                mark = " <- 当前" if a.get("id") == current else ""
                lines.append(
                    f"- {a.get('id')} | 用户ID: {p.get('user_id') or '未登录'}"
                    f" | 名称: {p.get('user_name') or '未知'}"
                    f" | token: {'有' if (a.get('auth') or {}).get('token') else '无'}"
                    f" | api_key: {'有' if (a.get('auth') or {}).get('api_key') else '无'}{mark}"
                )
            return "\n".join(lines)
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity("account_current", lambda kw: {"type": "account", "id": "current"})
    async def mfuns_account_current() -> str:
        """查看当前操作身份（发布前确认身份用）。

        Args:
            无参数：返回当前账号 id / user_id / user_name
        """
        try:
            acc = config.get_account(client.account_id) or {}
            p = acc.get("profile") or {}
            return (
                f"当前账号: {client.account_id}"
                f" | 用户ID: {p.get('user_id') or '未登录'}"
                f" | 名称: {p.get('user_name') or '未知'}"
            )
        except Exception as e:
            return _fmt_err(e)

    @mcp.tool()
    @activity(
        "account_switch",
        lambda kw: {"type": "account", "id": kw.get("account_id")},
    )
    async def mfuns_account_switch(account_id: str) -> str:
        """切换当前操作账号（校验身份后生效，后续所有业务工具自动使用该账号）。

        Args:
            account_id: 目标账号 ID（如 u_38461，可用 mfuns_account_list 查看）
        """
        try:
            return await client.switch(account_id)
        except Exception as e:
            return _fmt_err(e)
