"""Mfuns API 异步客户端：自动登录、401 自动重登、5 QPS 限速。"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import hashlib
import hmac
import json
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

# 官方开放平台 API KEY 支持白名单（文档标注 + 实测确认；contribute/get、article/delete 等实测 401）
_API_KEY_PATHS = frozenset({
    "/v1/contribute/video/get_upload_auth",
    "/v1/contribute/video/update_upload_auth",
    "/v1/contribute/video/upload_complete",
    "/v1/contribute/video/create",
    "/v1/contribute/video/update",
    "/v1/contribute/article/create",
    "/v1/contribute/article/update",
    "/v1/contribute/list",
    "/v1/media/upload_image",
})


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
        self._api_key = config.get_api_key()
        self._login_lock = asyncio.Lock()
        self._last_ts = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, source: str = "token") -> dict:
        h = {"User-Agent": USER_AGENT}
        auth = self._api_key if source == "api_key" else self._token
        if auth:
            h["Authorization"] = auth
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
        source: str | None = None,
    ) -> Any:
        """发送请求并解包统一信封（code==1 返回 data）。

        鉴权来源：
        - 投稿类接口（/v1/contribute/*）优先使用 config.api_key（官方密钥），
          未配置或 401 时回退用户 token；仍失败则抛错（不自动重登覆盖）
        - 其余接口全局使用用户 token；401 视为失效自动登录后重试一次
        - 无 token 时先匿名请求，遇 401 且配置了账号密码则自动登录
        """
        if source is None:
            source = "api_key" if self._use_api_key(path) else "token"
        if auth and source == "token" and not self._token and await self._need_login():
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
            headers=self._headers(source),
        )
        try:
            body = resp.json()
        except ValueError:
            raise MfunsError(-1, f"HTTP {resp.status_code}: 响应不是 JSON")
        code = body.get("code")
        if auth and code == 401 and retry:
            if source == "api_key":
                if self._token:
                    logger.info("API KEY 无效，回退用户 token")
                    return await self.request(
                        method, path, params, json_body, form, auth, retry=False, source="token"
                    )
                raise MfunsError(401, "API KEY 无效或已过期（config.json 的 api_key 不自动重登）")
            logger.info("token 失效或需登录，重新登录")
            async with self._login_lock:
                await self.login()
            return await self.request(
                method, path, params, json_body, form, auth, retry=False, source="token"
            )
        if code != 1:
            raise MfunsError(code, body.get("msg") or "请求失败")
        return body.get("data")

    @staticmethod
    async def _need_login() -> bool:
        account, password = config.get_credentials()
        return bool(account and password)

    def _use_api_key(self, path: str) -> bool:
        """该路径是否应使用官方 API KEY（仅白名单内的投稿接口；其余一律用户 token）。"""
        return bool(self._api_key) and path in _API_KEY_PATHS

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

    async def feed_area_id(self, feed_id: int) -> int:
        """由动态 ID 解析评论区 ID（发评论前必须）。"""
        data = await self.get("/v1/feeds/get", id=feed_id)
        area_id = (data or {}).get("comment_area_id")
        if not area_id:
            raise MfunsError(-1, f"动态 {feed_id} 没有评论区")
        return int(area_id)

    async def video_upload_auth(self, file_name: str, file_size: int) -> dict:
        """获取阿里云 VOD 上传凭证。"""
        data = await self.post(
            "/v1/contribute/video/get_upload_auth",
            json_body={"file_name": file_name, "file_size": file_size},
        )
        if not isinstance(data, dict) or not data.get("VideoId"):
            raise MfunsError(-1, "未获取到上传凭证")
        return data

    async def video_upload_complete(self, video_id: str, retries: int = 5, delay: float = 3.0) -> dict:
        """通知视频上传完成；服务端异步校验文件，失败自动重试（最终一致性）。"""
        last_err: MfunsError | None = None
        for _ in range(retries):
            try:
                data = await self.post(
                    "/v1/contribute/video/upload_complete",
                    json_body={"videoId": video_id},
                )
            except MfunsError as e:
                last_err = e
            else:
                if isinstance(data, dict) and data.get("status") == 1:
                    return data
                last_err = MfunsError(
                    0, f"上传确认异常 status={data.get('status') if isinstance(data, dict) else data}"
                )
            await asyncio.sleep(delay)
        raise MfunsError(0, f"视频上传完成确认失败（{retries} 次重试后仍未确认: {last_err}）")


def oss_put(addr_b64: str, auth_b64: str, file_obj, content_type: str = "application/octet-stream") -> None:
    """将文件直接 PUT 到阿里云 OSS（VOD 上传地址），纯标准库签名，无 SDK 依赖。"""
    addr = json.loads(base64.b64decode(addr_b64))
    auth = json.loads(base64.b64decode(auth_b64))
    endpoint = (addr.get("Endpoint") or addr.get("endpoint") or "").replace("https://", "").replace("http://", "")
    bucket = addr.get("Bucket") or addr.get("bucket")
    key = addr.get("FileName") or addr.get("fileName") or addr.get("objectKey")
    if not (endpoint and bucket and key):
        raise MfunsError(-1, "上传地址解析失败")
    date = email.utils.formatdate(usegmt=True)
    string_to_sign = (
        f"PUT\n\n{content_type}\n{date}\n"
        f"x-oss-security-token:{auth['SecurityToken']}\n/{bucket}/{key}"
    )
    signature = base64.b64encode(
        hmac.new(
            auth["AccessKeySecret"].encode(), string_to_sign.encode(), hashlib.sha1
        ).digest()
    ).decode()
    headers = {
        "Date": date,
        "Content-Type": content_type,
        "x-oss-security-token": auth["SecurityToken"],
        "Authorization": f"OSS {auth['AccessKeyId']}:{signature}",
    }
    url = f"https://{bucket}.{endpoint}/{key}"
    resp = httpx.put(url, content=file_obj, headers=headers, timeout=600.0)
    if resp.status_code != 200:
        raise MfunsError(-1, f"OSS 上传失败 HTTP {resp.status_code}: {resp.text[:200]}")

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
