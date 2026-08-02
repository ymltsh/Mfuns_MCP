"""Mfuns API 异步客户端：多账号上下文、自动登录、401 重试、5 QPS 限速。"""

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
    """多账号感知客户端：切换账号后自动加载对应 token/api_key/凭据。"""

    def __init__(self, account_id: str | None = None) -> None:
        self.base_url = config.get_base_url()
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self._login_lock = asyncio.Lock()
        self._last_ts = 0.0
        self._account_id = account_id or config.get_current_account_id()
        self._token = ""
        self._api_key = ""
        self._load_account()

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def account_name(self) -> str:
        acc = config.get_account(self._account_id)
        return ((acc or {}).get("profile") or {}).get("user_name") or self._account_id

    def reset_to_current(self) -> None:
        """移除当前账号后回退到配置的当前账号（无账号则清空身份）。"""
        self._account_id = config.get_current_account_id()
        self._load_account()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _load_account(self) -> None:
        acc = config.get_account(self._account_id) or {}
        auth = acc.get("auth") or {}
        self._token = auth.get("token") or ""
        self._api_key = auth.get("api_key") or ""

    def _credentials(self) -> tuple[str, str]:
        acc = config.get_account(self._account_id) or {}
        auth = acc.get("auth") or {}
        return auth.get("account") or "", auth.get("password") or ""

    async def _need_login(self) -> bool:
        account, password = self._credentials()
        return bool(account and password)

    def _use_api_key(self, path: str) -> bool:
        """该路径是否应使用官方 API KEY（仅白名单内的投稿接口；其余一律用户 token）。"""
        return bool(self._api_key) and path in _API_KEY_PATHS

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
        """当前账号账号密码登录，成功后回写 token 与 profile。"""
        account, password = self._credentials()
        if not account or not password:
            raise MfunsError(
                401,
                f"账号 {self._account_id} 未配置账号密码（auth.account / auth.password）",
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
        uid = _decode_user_id(token)
        config.update_account(self._account_id, auth={"token": token})
        config.update_account(self._account_id, profile={"user_id": uid})
        if self._account_id.startswith("u_unknown") and uid:
            new_id = f"u_{uid}"
            config.rename_account(self._account_id, new_id)
            self._account_id = new_id
        logger.info("账号 %s 登录成功，token 已缓存", self._account_id)
        return token

    async def switch(self, account_id: str) -> str:
        """切换当前账号；存在 token 时校验身份并刷新 profile，防串号。

        校验失败自动回滚到原账号，不影响后续操作。
        """
        acc = config.get_account(account_id)
        if not acc:
            raise MfunsError(-1, f"账号不存在: {account_id}（可用 mfuns_account_list 查看）")
        if not acc.get("enabled"):
            raise MfunsError(-1, f"账号已禁用: {account_id}")
        prev = self._account_id
        self._account_id = account_id
        self._load_account()
        note = ""
        try:
            if self._token:
                info = await self.get("/v1/user/info")
                user = (info or {}).get("user") or {}
                uid = user.get("id")
                if not uid or not (info or {}).get("login"):
                    raise MfunsError(401, f"账号 {account_id} 的 token 已失效，请配置账号密码自动重登")
                expect = acc.get("profile", {}).get("user_id")
                if expect and uid != expect:
                    raise MfunsError(
                        -1,
                        f"身份不匹配: 账号 {account_id} 配置 user_id={expect}，"
                        f"但 token 属于用户 {uid}（防串号校验，请检查 auth.token）",
                    )
                config.update_account(
                    account_id,
                    profile={"user_id": uid, "user_name": user.get("name") or ""},
                )
                if account_id.startswith("u_unknown"):
                    new_id = f"u_{uid}"
                    config.rename_account(account_id, new_id)
                    self._account_id = new_id
                    note = f"（临时 id 已更新为 {new_id}）"
            elif not self._token and not self._api_key:
                raise MfunsError(401, f"账号 {account_id} 未配置 token 且未配置账号密码，无法使用")
        except Exception:
            self._account_id = prev
            self._load_account()
            raise
        config.set_current_account(self._account_id)
        logger.info("已切换账号 -> %s", self._account_id)
        return f"已切换至 {self._account_id}（{self.account_name}）{note}"

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
        """发送请求并解包统一信封（code==1 返回 data），始终使用当前账号身份。

        鉴权来源：
        - 投稿类接口（/v1/contribute/* 白名单）优先使用当前账号 api_key，401 回退其 token
        - 其余接口使用当前账号 token；401 视为失效自动登录后重试一次
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
                raise MfunsError(401, f"账号 {self._account_id} 的 API KEY 无效或已过期")
            logger.info("token 失效或需登录，重新登录")
            async with self._login_lock:
                await self.login()
            return await self.request(
                method, path, params, json_body, form, auth, retry=False, source="token"
            )
        if code != 1:
            raise MfunsError(code, body.get("msg") or "请求失败")
        return body.get("data")

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


# ---- 独立凭证校验（不依赖当前账号上下文，用于 mfuns_account_add）----

async def api_login(account: str, password: str) -> str:
    """账号密码登录，返回 access_token。"""
    async with httpx.AsyncClient(base_url=config.get_base_url(), timeout=30.0) as http:
        resp = await http.post(
            "/v1/auth/login",
            data={"account": account, "password": password},
            headers={"User-Agent": USER_AGENT},
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
        return token


async def api_identity(auth: str) -> tuple[int, str]:
    """用凭证（token 或 api_key）调 /v1/user/info，返回 (user_id, user_name)。"""
    async with httpx.AsyncClient(base_url=config.get_base_url(), timeout=30.0) as http:
        resp = await http.get(
            "/v1/user/info",
            headers={"User-Agent": USER_AGENT, "Authorization": auth},
        )
        try:
            body = resp.json()
        except ValueError:
            raise MfunsError(-1, f"身份校验响应异常 HTTP {resp.status_code}")
        data = body.get("data") or {}
        if body.get("code") != 1 or not data.get("login"):
            raise MfunsError(body.get("code") or -1, body.get("msg") or "身份校验失败")
        user = data.get("user") or {}
        return user.get("id"), user.get("name") or ""
