"""config.json 读写与凭据管理。

配置优先级：环境变量 > config.json > 默认值。
- 账号密码：环境变量 MFUNS_ACCOUNT / MFUNS_PASSWORD 优先，其次 config.json 的 account / password
- token：登录成功后自动缓存回 config.json（默认 25 天有效，失效自动重登）
- 配置文件路径：环境变量 MFUNS_CONFIG 指定，默认项目根目录 config.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_URL = "https://api.mfuns.net"

_CONFIG_PATH = Path(
    os.environ.get("MFUNS_CONFIG", Path(__file__).resolve().parent.parent / "config.json")
)

_DEFAULTS: dict = {
    "base_url": DEFAULT_BASE_URL,
    "account": "",
    "password": "",
    "token": "",
    "api_key": "",
    "user_id": None,
}


def _ensure_file() -> dict:
    if not _CONFIG_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def get_base_url() -> str:
    return _ensure_file().get("base_url") or DEFAULT_BASE_URL


def get_credentials() -> tuple[str, str]:
    cfg = _ensure_file()
    account = os.environ.get("MFUNS_ACCOUNT") or cfg.get("account") or ""
    password = os.environ.get("MFUNS_PASSWORD") or cfg.get("password") or ""
    return account, password


def get_token() -> str:
    return _ensure_file().get("token") or ""


def get_api_key() -> str:
    """官方开放平台 API KEY（mf_ 前缀），可选；仅用于投稿类接口。"""
    return _ensure_file().get("api_key") or ""


def get_user_id() -> int | None:
    return _ensure_file().get("user_id")


def set_session(token: str, user_id: int | None = None) -> None:
    """登录成功后缓存 token 与 user_id。"""
    data = _ensure_file()
    data["token"] = token
    if user_id is not None:
        data["user_id"] = user_id
    try:
        _CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
