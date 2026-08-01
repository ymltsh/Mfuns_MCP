"""Mfuns API 异步客户端：自动登录、401 自动重登、5 QPS 限速。"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any

import httpx

from . import config

logger = logging.getLogger(__name__)

RATE_MIN_INTERVAL = 0.25  # API 限制 5 QPS，全局节流

# WAF 会拦截无浏览器 UA 的请求（实测返回 403）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_USER_ID_RE = re.compile(r"&id&(\d+)")


class MfunsError(Exception):
    """携带 Mfuns 业务错误码的异常。"""

    def __init__(self, code, msg):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


def _decode_user_id(token: str) -> int | None:
    """从登录 token 的 Base64 内容中解析用户 ID（格式: ...&id&17627&...）。"""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4))
        m = _USER_ID_RE.search(raw.decode("utf-8", errors="ignore"))
        return int(m.group(1)) if m else None
    except Exception:
        return None


class MfunsClient:
    def __init__(self) -> None:
        self.base_url = config.get_base_url()
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self._token = config.get_token()
        self._login_lock = asyncio.Lock()
        self._last_ts = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict:
        h = {"User-Agent": USER_AGENT}
        if self._token:
            h["Authorization"] = self._token
        return h

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = RATE_MIN_INTERVAL - (now - self._last_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_ts = time.monotonic()

    async def login(self) -> str:
        account, password = config.get_credentials()
        if not account or not password:
            raise MfunsError(
                401,
                "未配置账号密码：请设置环境变量 MFUNS_ACCOUNT / MFUNS_PASSWORD，"
                "或写入 config.json 的 account / password",
            )
        await self._throttle()
        resp = await self._http.post(
            "/v1/auth/login",
            data={"account": account, "password": password},
            headers=self._headers(),
        )
        try:
            body = resp.json()
        except ValueError:
            raise MfunsError(-1, f"登录接口响应异常 HTTP {resp.status_code}")
        if body.get("code") != 1:
            raise MfunsError(body.get("code") or -1, f"登录失败: {body.get('msg', '')}")
        token = (body.get("data") or {}).get("access_token")
        if not token:
            raise MfunsError(-1, "登录成功但未返回 access_token")
        self._token = token
        config.set_session(token, _decode_user_id(token))
        logger.info("登录成功，token 已缓存")
        return token

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        form: dict | None = None,
        auth: bool = True,
        retry: bool = True,
    ) -> Any:
        """发送请求并解包统一信封（code==1 返回 data）。

        - 已有 token 直接携带；401 视为 token 失效，重新登录后重试一次
        - 无 token 时先匿名请求（浏览/搜索等读接口无需登录）；
          遇 401 且配置了账号密码则自动登录后重试
        """
        if auth and not self._token and await self._need_login():
            async with self._login_lock:
                if not self._token and await self._need_login():
                    await self.login()
        await self._throttle()
        resp = await self._http.request(
            method,
            path,
            params=params,
            json=json_body,
            data=form,
            headers=self._headers(),
        )
        try:
            body = resp.json()
        except ValueError:
            raise MfunsError(-1, f"HTTP {resp.status_code}: 响应不是 JSON")
        code = body.get("code")
        if auth and code == 401 and retry:
            logger.info("token 失效或需登录，重新登录")
            async with self._login_lock:
                await self.login()
            return await self.request(
                method, path, params, json_body, form, auth, retry=False
            )
        if code != 1:
            raise MfunsError(code, body.get("msg") or "请求失败")
        return body.get("data")

    @staticmethod
    async def _need_login() -> bool:
        account, password = config.get_credentials()
        return bool(account and password)

    async def get(self, path: str, **params) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, json_body: dict | None = None, form: dict | None = None
    ) -> Any:
        return await self.request("POST", path, json_body=json_body, form=form)

    async def get_text(self, url: str, params: dict | None = None) -> str:
        """获取第三方服务的纯文本响应（如 mfuns.wgen.top，无 Mfuns 信封、无鉴权）。"""
        await self._throttle()
        resp = await self._http.request(
            "GET",
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            raise MfunsError(-1, f"HTTP {resp.status_code}: {resp.text[:120]}")
        return resp.text

    # ---- 业务辅助 ----

    async def article_area_id(self, article_id: int) -> int:
        """由文章 ID 解析评论区 ID（发评论前必须）。"""
        data = await self.get("/v1/article/get", id=article_id)
        area_id = ((data or {}).get("article") or {}).get("comment_area_id")
        if not area_id:
            raise MfunsError(-1, f"文章 {article_id} 没有评论区")
        return int(area_id)

    async def video_area_id(self, video_id: int) -> int:
        """由视频 ID 解析评论区 ID（发评论前必须）。"""
        data = await self.get("/v1/video/get", id=video_id)
        area_id = (data or {}).get("comment_area_id")
        if not area_id:
            raise MfunsError(-1, f"视频 {video_id} 没有评论区")
        return int(area_id)

    async def default_favorite_list_id(self) -> int:
        """获取当前用户的第一个收藏夹 ID（默认收藏夹）。"""
        me = await self.get("/v1/user/info")
        uid = ((me or {}).get("user") or {}).get("id")
        if not uid:
            raise MfunsError(-1, "无法获取当前用户 ID")
        data = await self.get("/v1/favorite/get_favorite_list", user_id=uid)
        lists = (data or {}).get("list") or []
        if not lists:
            raise MfunsError(-1, "没有可用收藏夹，请先创建收藏夹")
        return int(lists[0]["id"])
