"""Mfuns MCP Server 启动入口。

用法:
    uv run main.py                 # 以 stdio 方式启动 MCP 服务
    uv run python -m mfuns_mcp     # 等价（模块入口）

注意: stdio 协议要求 stdout 只传输 JSON-RPC 消息，
状态与使用说明一律输出到 stderr，不会干扰协议。
"""

from __future__ import annotations

import logging
import sys

from mfuns_mcp import __version__, config
from mfuns_mcp.server import build_server


def print_status() -> None:
    """向 stderr 打印服务状态与使用说明。"""
    account, _password = config.get_credentials()
    token = config.get_token()
    user_id = config.get_user_id()
    has_token = bool(token)

    lines = [
        "================================================",
        " Mfuns MCP Server 启动",
        "------------------------------------------------",
        f" 版本        : v{__version__}",
        " 传输方式    : stdio",
        f" 接口地址    : {config.get_base_url()}",
        f" 配置文件    : {config._CONFIG_PATH}",
        f" 账号配置    : {'是 (' + account + ')' if account else '否'}",
        f" 登录状态    : " + ("已登录 (user_id=" + str(user_id) + ")" if has_token else "未登录（读接口可匿名使用，写接口将自动登录）"),
        "------------------------------------------------",
        " 使用说明:",
        "  1. 在 MCP 客户端配置本服务（mcpServers）:",
        '     { "command": "uv", "args": ['
        f'"--directory", "{config._CONFIG_PATH.parent}", "run", "main.py"] }}',
        "  2. 未配置账号时，浏览/搜索/读帖等读操作可直接使用;",
        "  3. 评论/点赞/投稿等写操作需账号密码:",
        "     环境变量 MFUNS_ACCOUNT / MFUNS_PASSWORD，或 config.json 的 account / password;",
        "  4. 登录成功后 token 自动缓存到 config.json（约 25 天有效，失效自动重登）。",
        "================================================",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


def main() -> None:
    # 强制 UTF-8：stdout 必须只输出 JSON-RPC；stderr 状态/日志避免被 Windows 控制台 GBK 编码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print_status()
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
