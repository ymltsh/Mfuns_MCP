"""Mfuns MCP Server 启动入口。

用法:
    uv run main.py                          # 默认以 stdio 方式启动（MCP 客户端用）
    uv run main.py --transport streamable-http [--host 0.0.0.0] [--port 8000] [--path /mcp]
    uv run python -m mfuns_mcp              # 等价于默认 stdio

注意: stdio 协议要求 stdout 只传输 JSON-RPC 消息，
状态与使用说明一律输出到 stderr，不会干扰协议。
"""

from __future__ import annotations

import argparse
import logging
import sys

from mfuns_mcp import __version__, config
from mfuns_mcp.server import build_server


def print_status(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000, path: str = "/mcp") -> None:
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
        f" 传输方式    : {transport}",
    ]
    if transport == "streamable-http":
        lines.append(f" HTTP 地址   : http://{host}:{port}{path}")
    lines += [
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

    parser = argparse.ArgumentParser(description="Mfuns MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="传输方式（默认 stdio）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 监听端口（默认 8000）")
    parser.add_argument("--path", default="/mcp", help="Streamable HTTP 路径（默认 /mcp）")
    args = parser.parse_args()

    mcp = build_server()
    if args.transport == "stdio":
        print_status("stdio")
        mcp.run(transport="stdio")
    elif args.transport == "streamable-http":
        print_status("streamable-http", args.host, args.port, args.path)
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    else:  # sse
        print_status("sse", args.host, args.port, args.path)
        mcp.run(
            transport="sse",
            host=args.host,
            port=args.port,
            sse_path=args.path,
        )


if __name__ == "__main__":
    main()
