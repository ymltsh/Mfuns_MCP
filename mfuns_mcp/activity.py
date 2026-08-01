"""纯 Activity Log：每次 MCP 工具调用记录一条，按日期隔离为 JSON 文件。

目录结构:
    logs/activity/YYYY-MM-DD.json

不存储 LLM 思考/完整上下文，只记录最小操作信息，可随时迁移到数据库。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# 基于项目根目录（与 cwd 无关，兼容 AstrBot 等外部启动方式）
LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "activity"
_MAX_STR = 200
_MAX_LIST = 20

_lock = asyncio.Lock()


def _truncate(value: Any) -> Any:
    """裁剪超长字符串/容器，防止日志体积膨胀。"""
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "..."
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(v) for v in list(value)[:_MAX_LIST]]
    return value


async def write_activity(
    tool: str,
    action: str,
    target: dict | None = None,
    params: dict | None = None,
    result: dict | None = None,
) -> None:
    """追加一条 Activity Log 到当日文件。"""
    now = datetime.now()
    file = LOG_DIR / f"{now:%Y-%m-%d}.json"
    record: dict = {
        "time": now.strftime("%H:%M:%S"),
        "tool": tool,
        "action": action,
        "target": target,
        "params": _truncate(params),
        "result": result,
    }
    async with _lock:  # 串行化读改写，避免并发覆盖
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            logs = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(logs, list):
                logs = []
        except (OSError, json.JSONDecodeError):
            logs = []
        logs.append(record)
        file.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


def read_activity(date: str) -> list[dict]:
    """读取某日 Activity Log（文件不存在或损坏返回空列表）。"""
    file = LOG_DIR / f"{date}.json"
    try:
        logs = json.loads(file.read_text(encoding="utf-8"))
        return logs if isinstance(logs, list) else []
    except (OSError, json.JSONDecodeError):
        return []


# ---- 工具装饰器：不侵入工具逻辑，调用后自动记录 ----

def activity(action: str | Callable[[dict], str], target_fn: Callable[[dict], dict | None] | None = None):
    """工具装饰器：执行后写 Activity Log。

    Args:
        action: 动作名，或从 kwargs 派生动作名的函数
        target_fn: 从 kwargs 派生影响对象 {type, id} 的函数（可选）
    """
    def deco(fn):
        import functools

        name = fn.__name__

        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                out = await fn(*args, **kwargs)
                status = (
                    "success"
                    if isinstance(out, str) and not out.startswith("错误")
                    else "error"
                )
                await _log(name, action, target_fn, kwargs, status, out)
                return out
            except Exception as e:
                out = f"错误: {e}"
                await _log(name, action, target_fn, kwargs, "error", out)
                return out

        return wrapped

    return deco


async def _log(
    tool: str,
    action: str | Callable[[dict], str],
    target_fn: Callable[[dict], dict | None] | None,
    kwargs: dict,
    status: str,
    out: str,
) -> None:
    try:
        act = action(kwargs) if callable(action) else action
        target = target_fn(kwargs) if target_fn else None
        result: dict = {"status": status}
        if status == "error" and out:
            result["message"] = _truncate(out)
        await write_activity(tool, act, target, kwargs, result)
    except Exception:
        pass  # 日志失败不影响工具本身
