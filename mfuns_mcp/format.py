"""文本 / Quill / HTML 转换与展示辅助。"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime

_IMG_RE = re.compile(r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>', re.I)
_BLOCK_RE = re.compile(
    r"<(p|div|h[1-6]|li|blockquote|pre|tr|br|/p|/div|/h[1-6]|/li|/blockquote|/pre|/ul|/ol)[^>]*>",
    re.I,
)


def text_to_quill(text: str) -> str:
    """纯文本 -> Quill JSON 字符串（评论、视频简介等接口需要）。"""
    lines = (text or "").strip().split("\n")
    ops: list[dict] = []
    for line in lines:
        if line.strip():
            ops.append({"insert": line.rstrip() + "\n"})
        else:
            ops.append({"insert": "\n"})
    if not ops:
        ops.append({"insert": "\n"})
    return json.dumps({"ops": ops}, ensure_ascii=False)


def html_to_text(raw: str) -> str:
    """服务端渲染的 HTML -> 可读纯文本。"""
    if not raw:
        return ""
    s = _IMG_RE.sub(lambda m: f"[图片: {m.group(1)}]", raw)
    s = _BLOCK_RE.sub("\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def quill_to_text(raw: str) -> str:
    """Quill JSON 字符串 -> 纯文本；非 JSON（纯文本）则原样返回。"""
    if not raw:
        return ""
    try:
        ops = json.loads(raw).get("ops", [])
    except (ValueError, AttributeError):
        return raw.strip()
    parts = [op.get("insert") for op in ops if isinstance(op.get("insert"), str)]
    return "".join(parts).rstrip("\n")


def ts_to_str(ts) -> str:
    """秒级时间戳 -> YYYY-MM-DD HH:MM。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def author_name(obj: dict) -> str:
    """从容错结构（user / author / user_info / user_id）提取作者名。"""
    if not isinstance(obj, dict):
        return ""
    user = obj.get("user") or obj.get("author") or obj.get("user_info")
    if isinstance(user, dict) and user.get("name"):
        return str(user["name"])
    if obj.get("name"):
        return str(obj["name"])
    uid = obj.get("user_id")
    return f"用户{uid}" if uid else "未知"
