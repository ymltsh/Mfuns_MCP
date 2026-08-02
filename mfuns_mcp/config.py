"""多账号组配置读写。

config.json 结构（accounts 数组 + 运行时状态）:
    {
      "base_url": "https://api.mfuns.net",
      "accounts": [
        {
          "id": "u_38461",                        # 内部账号标识（未登录账号可用临时 id u_unknown_N，登录后自动更新）
          "profile": {"user_id": 38461, "user_name": "Sincerely"},
          "auth": {"account": "", "password": "", "token": "", "api_key": ""},
          "enabled": true
        }
      ],
      "runtime": {"current_account": "u_38461"}
    }

兼容：若 accounts 缺失/为空，则从旧扁平字段（account/password/token/api_key/user_id）合成单账号。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_URL = "https://api.mfuns.net"

_CONFIG_PATH = Path(
    os.environ.get("MFUNS_CONFIG", Path(__file__).resolve().parent.parent / "config.json")
)

_EMPTY_ACCOUNT = {
    "id": "",
    "profile": {"user_id": None, "user_name": ""},
    "auth": {"account": "", "password": "", "token": "", "api_key": ""},
    "enabled": True,
}


def _load() -> dict:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("base_url", DEFAULT_BASE_URL)
    if "accounts" not in data:
        data["accounts"] = [_legacy_account(data)]
    if not isinstance(data.get("runtime"), dict):
        data["runtime"] = {}
    return data


def _legacy_account(flat: dict) -> dict:
    """旧扁平配置 → 单账号。"""
    acc = json.loads(json.dumps(_EMPTY_ACCOUNT))
    auth = flat.get("auth") or {}
    user_id = flat.get("user_id")
    acc["id"] = f"u_{user_id}" if user_id else "u_unknown_0"
    acc["profile"] = {"user_id": user_id, "user_name": ""}
    acc["auth"] = {
        "account": flat.get("account") or auth.get("account") or "",
        "password": flat.get("password") or auth.get("password") or "",
        "token": flat.get("token") or auth.get("token") or "",
        "api_key": flat.get("api_key") or auth.get("api_key") or "",
    }
    return acc


def _save(data: dict) -> None:
    try:
        _CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def get_base_url() -> str:
    return _load().get("base_url") or DEFAULT_BASE_URL


def get_accounts() -> list[dict]:
    """返回账号列表（含未启用的，便于展示）。"""
    data = _load()
    accounts = data.get("accounts") or []
    out = []
    for i, a in enumerate(accounts):
        acc = json.loads(json.dumps(_EMPTY_ACCOUNT))
        acc.update({k: v for k, v in a.items() if v is not None})
        acc["auth"] = json.loads(json.dumps(_EMPTY_ACCOUNT["auth"]))
        acc["auth"].update({k: v for k, v in (a.get("auth") or {}).items() if v is not None})
        acc["profile"] = json.loads(json.dumps(_EMPTY_ACCOUNT["profile"]))
        acc["profile"].update({k: v for k, v in (a.get("profile") or {}).items() if v is not None})
        if not acc.get("id"):
            acc["id"] = f"u_unknown_{i}"
        out.append(acc)
    return out


def get_account(account_id: str) -> dict | None:
    for a in get_accounts():
        if a["id"] == account_id:
            return a
    return None


def get_current_account_id() -> str:
    data = _load()
    cid = data.get("runtime", {}).get("current_account")
    if cid and get_account(cid):
        return cid
    accounts = get_accounts()
    return accounts[0]["id"] if accounts else ""


def set_current_account(account_id: str) -> None:
    data = _load()
    data.setdefault("runtime", {})["current_account"] = account_id
    _save(data)


def update_account(account_id: str, **fields) -> None:
    """更新账号字段（auth/profile/id 均可，按键写入对应分组）。"""
    data = _load()
    for a in data.get("accounts") or []:
        if a.get("id") != account_id:
            continue
        for key in ("auth", "profile"):
            if key in fields and isinstance(fields[key], dict):
                a.setdefault(key, {})
                a[key].update({k: v for k, v in fields[key].items() if v is not None})
        if "auth" not in fields and "profile" not in fields:
            for k, v in fields.items():
                if v is not None:
                    a[k] = v
        _save(data)
        return


def rename_account(old_id: str, new_id: str) -> None:
    """账号 id 重命名（临时 id → 真实 user_id），runtime.current_account 同步。"""
    data = _load()
    renamed = False
    for a in data.get("accounts") or []:
        if a.get("id") == old_id:
            a["id"] = new_id
            renamed = True
            break
    if renamed and data.get("runtime", {}).get("current_account") == old_id:
        data.setdefault("runtime", {})["current_account"] = new_id
    if renamed:
        _save(data)


def add_account(account_id: str, auth: dict, profile: dict | None = None, enabled: bool = True) -> None:
    """新增账号（id 查重由调用方负责）。"""
    data = _load()
    data.setdefault("accounts", []).append({
        "id": account_id,
        "profile": profile or {"user_id": None, "user_name": ""},
        "auth": {
            "account": auth.get("account") or "",
            "password": auth.get("password") or "",
            "token": auth.get("token") or "",
            "api_key": auth.get("api_key") or "",
        },
        "enabled": enabled,
    })
    _save(data)


def remove_account(account_id: str) -> bool:
    """移除账号；若移除的是当前账号，runtime.current_account 回退到剩余第一个。"""
    data = _load()
    accounts = data.get("accounts") or []
    before = len(accounts)
    data["accounts"] = [a for a in accounts if a.get("id") != account_id]
    if len(data["accounts"]) == before:
        return False
    if data.get("runtime", {}).get("current_account") == account_id:
        data.setdefault("runtime", {})["current_account"] = (
            data["accounts"][0]["id"] if data["accounts"] else ""
        )
    _save(data)
    return True
