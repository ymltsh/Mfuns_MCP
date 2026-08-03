"""视频上传管线：阿里云 VOD 断点续传（oss2）+ 后台投稿任务管理（磁盘持久化）。

流程：get_upload_auth 获取 STS 凭证 -> oss2 断点续传 OSS -> upload_complete 校验（12x5s 重试，
容忍 VOD 处理延迟）-> 全部 P 完成后 video/create 投稿。

任务化与断点续传：mfuns_publish_video_upload 创建任务后立即返回 task_id，多P 并行上传在后台进行；
任务状态（含各分P VideoId/视频库ID/元数据）原子写入 logs/upload_tasks/<task_id>/task.json，
服务重启后 mfuns_upload_task 首次轮询自动恢复未完成任务：
- 已完成分P（已存 mfuns_id）直接跳过
- 上传中分P 用 update_upload_auth 刷新同一 VOD 记录凭证，OSS 分片检查点续传
- 排队中分P 正常上传
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import oss2

from . import config
from .client import MfunsClient, MfunsError
from .format import text_to_quill

logger = logging.getLogger(__name__)

UPLOAD_CONCURRENCY = 2  # 并行上传的分P数（API 限速 5 QPS，全局节流会串行化请求）
MAX_TASKS = 50  # 内存/磁盘中保留的最大任务数，超出丢弃最旧的已完成/失败任务

DEFAULT_COVER = "https://resource.mfuns.net/image/default/default_cover.jpg"

_TASK_STATUS = {
    "pending": "排队中",
    "uploading": "上传中",
    "publishing": "投稿中",
    "done": "已完成",
    "failed": "失败",
}
_PART_STATUS = {
    "pending": "排队中",
    "uploading": "上传中",
    "uploaded": "已上传",
    "failed": "失败",
}

TASK_DIR_BASE = Path(__file__).resolve().parent.parent / "logs" / "upload_tasks"
TASK_FILE = "task.json"

_RESUME_STATUSES = ("pending", "uploading", "publishing")


class VodUploadError(MfunsError):
    """VOD 上传阶段错误（含 OSS）。"""


def parse_upload_credentials(data: dict) -> dict:
    """解析 get_upload_auth 返回的 UploadAuth/UploadAddress（Base64 JSON）。"""
    try:
        auth = json.loads(base64.b64decode(data["UploadAuth"]).decode("utf-8"))
        address = json.loads(base64.b64decode(data["UploadAddress"]).decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError) as e:
        raise VodUploadError(-1, f"解析 VOD 上传凭证失败: {e}") from e

    endpoint = address.get("Endpoint") or ""
    if endpoint and not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    bucket_name = address.get("Bucket")
    object_key = (
        address.get("FileName") or address.get("ObjectName")
        or address.get("objectKey") or data.get("FileName")
    )
    for key in ("AccessKeyId", "AccessKeySecret", "SecurityToken"):
        if key not in auth:
            raise VodUploadError(-1, f"VOD 上传凭证缺少字段 {key}")
    if not (endpoint and bucket_name and object_key):
        raise VodUploadError(-1, "VOD 上传地址缺少 Endpoint/Bucket/FileName 字段")
    return {
        "access_key_id": auth["AccessKeyId"],
        "access_key_secret": auth["AccessKeySecret"],
        "security_token": auth["SecurityToken"],
        "endpoint": endpoint,
        "bucket": bucket_name,
        "object_key": object_key,
    }


def upload_video_file(
    credentials: dict,
    file_path: str,
    *,
    checkpoint_dir: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """使用 STS 凭证将本地视频断点续传到 OSS（同步函数，调用方用 to_thread 执行）。"""
    auth = oss2.StsAuth(
        credentials["access_key_id"],
        credentials["access_key_secret"],
        credentials["security_token"],
    )
    try:
        bucket = oss2.Bucket(auth, credentials["endpoint"], credentials["bucket"])
    except oss2.exceptions.OssError as e:
        raise VodUploadError(-1, f"创建 OSS Bucket 失败: {e}") from e

    kwargs: dict[str, Any] = {}
    if progress:
        kwargs["progress_callback"] = progress
    if checkpoint_dir:
        kwargs["store"] = oss2.ResumableStore(root=checkpoint_dir)

    try:
        oss2.resumable_upload(
            bucket,
            credentials["object_key"],
            file_path,
            **kwargs,
        )
    except oss2.exceptions.OssError as e:
        raise VodUploadError(-1, f"OSS 上传失败: {e}") from e


def _build_meta(completed: dict, file_name: str) -> dict:
    """从 upload_complete 返回值构造分P meta（网页端编辑页大小/时长/扩展名读它，缺失显示 0）。"""
    meta: dict = {}
    if completed.get("file_size"):
        meta["size"] = completed["file_size"]
    if "." in file_name:
        meta["ext"] = file_name.rsplit(".", 1)[-1].lower()
    if completed.get("video_duration"):
        meta["duration"] = completed["video_duration"]
    return meta


def _normalize_auth(auth: dict) -> dict:
    """统一上传凭证键名（兼容大小写变体），缺失抛错。"""
    out: dict = {}
    for k, v in auth.items():
        out[k] = v
    if not out.get("VideoId") and out.get("videoId"):
        out["VideoId"] = out["videoId"]
    for key in ("UploadAuth", "UploadAddress"):
        if not out.get(key) and out.get(key.lower()):
            out[key] = out[key.lower()]
    if not (out.get("VideoId") and out.get("UploadAuth") and out.get("UploadAddress")):
        raise VodUploadError(-1, f"上传凭证字段不完整: {list(auth.keys())}")
    return out


@dataclass
class PartInfo:
    name: str
    size: int = 0
    status: str = "pending"  # pending / uploading / uploaded / failed
    percent: int = 0
    video_id: str | None = None  # VOD VideoId
    mfuns_id: int | None = None  # upload_complete 返回的视频库记录 ID
    meta: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "status": self.status,
            "percent": self.percent,
            "video_id": self.video_id,
            "mfuns_id": self.mfuns_id,
            "meta": self.meta,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PartInfo":
        return cls(
            name=d.get("name", ""),
            size=int(d.get("size") or 0),
            status=d.get("status", "pending"),
            percent=int(d.get("percent") or 0),
            video_id=d.get("video_id"),
            mfuns_id=d.get("mfuns_id"),
            meta=d.get("meta") or {},
            error=d.get("error") or "",
        )


@dataclass
class TaskInfo:
    task_id: str
    account_id: str
    title: str
    status: str = "pending"  # pending / uploading / publishing / done / failed
    cid: int | None = None
    note: str = ""
    contribute_id: int | None = None
    contribute_status: int | None = None
    error: str = ""
    files: list[str] = field(default_factory=list)
    content: str = ""
    cover: str = ""
    copyright_: int = 0
    tags: list[str] = field(default_factory=list)
    parts: list[PartInfo] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def dir(self) -> Path:
        return TASK_DIR_BASE / self.task_id

    def uploaded_count(self) -> int:
        return sum(1 for p in self.parts if p.status == "uploaded")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "title": self.title,
            "status": self.status,
            "cid": self.cid,
            "note": self.note,
            "contribute_id": self.contribute_id,
            "contribute_status": self.contribute_status,
            "error": self.error,
            "files": self.files,
            "content": self.content,
            "cover": self.cover,
            "copyright": self.copyright_,
            "tags": self.tags,
            "parts": [p.to_dict() for p in self.parts],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskInfo":
        return cls(
            task_id=d.get("task_id", ""),
            account_id=d.get("account_id", ""),
            title=d.get("title", ""),
            status=d.get("status", "pending"),
            cid=d.get("cid"),
            note=d.get("note") or "",
            contribute_id=d.get("contribute_id"),
            contribute_status=d.get("contribute_status"),
            error=d.get("error") or "",
            files=list(d.get("files") or []),
            content=d.get("content") or "",
            cover=d.get("cover") or "",
            copyright_=int(d.get("copyright") or 0),
            tags=list(d.get("tags") or []),
            parts=[PartInfo.from_dict(p) for p in (d.get("parts") or [])],
            created_at=float(d.get("created_at") or time.time()),
            updated_at=float(d.get("updated_at") or time.time()),
        )

    @classmethod
    def load(cls, task_id: str) -> "TaskInfo | None":
        path = TASK_DIR_BASE / task_id / TASK_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls.from_dict(data)

    def persist(self) -> None:
        """原子写盘：状态/分P进度落盘，供重启恢复。"""
        try:
            self.updated_at = time.time()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / (TASK_FILE + ".tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.dir / TASK_FILE)
        except OSError:
            logger.warning("任务 %s 状态写盘失败", self.task_id)

    def describe(self) -> str:
        lines = [
            f"任务 {self.task_id}（账号 {self.account_id}）: {self.title}",
            f"状态: {_TASK_STATUS.get(self.status, self.status)}"
            f" | 分P: {self.uploaded_count()}/{len(self.parts)}",
        ]
        for i, p in enumerate(self.parts, 1):
            st = _PART_STATUS.get(p.status, p.status)
            if p.status == "uploading" and p.percent >= 0:
                lines.append(f"  P{i} {p.name}: {st} {p.percent}%")
            elif p.status == "failed" and p.error:
                lines.append(f"  P{i} {p.name}: {st}（{p.error[:120]}）")
            else:
                lines.append(f"  P{i} {p.name}: {st}")
        if self.status == "done" and self.contribute_id:
            lines.append(f"投稿成功: 投稿ID {self.contribute_id}")
        elif self.status == "failed":
            lines.append(f"失败原因: {self.error[:300]}")
        if self.note:
            lines.append(f"提示: {self.note}")
        return "\n".join(lines)


class TaskManager:
    """任务注册表：内存 + 磁盘 task.json 双态，重启后自动恢复未完成任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._handles: dict[str, asyncio.Task] = {}
        self._seq = 0
        self._resumed = False

    def create(
        self,
        account_id: str,
        title: str,
        part_names: list[str],
        sizes: list[int],
    ) -> TaskInfo:
        now = time.strftime("%Y%m%d_%H%M%S")
        self._seq += 1
        task_id = f"vt_{now}_{self._seq:03d}"
        task = TaskInfo(
            task_id=task_id,
            account_id=account_id,
            title=title,
            parts=[PartInfo(name=n, size=s) for n, s in zip(part_names, sizes)],
        )
        self._prune()
        self._tasks[task_id] = task
        task.persist()
        return task

    def get(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def list(self, limit: int = 10) -> list[TaskInfo]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]

    def track(self, task_id: str, handle: asyncio.Task) -> None:
        """保存后台任务句柄，完成后自动释放。"""
        self._handles[task_id] = handle
        handle.add_done_callback(lambda _: self._handles.pop(task_id, None))

    async def ensure_resumed(self) -> int:
        """扫描磁盘恢复未完成任务并重启后台执行，返回恢复的任务数。

        无 task.json 的残留目录（旧版崩溃产物，检查点无对应任务）直接清理。
        """
        if self._resumed:
            return 0
        self._resumed = True
        if not TASK_DIR_BASE.is_dir():
            return 0
        restored = 0
        for d in sorted(TASK_DIR_BASE.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            task = TaskInfo.load(d.name)
            if task is None:
                shutil.rmtree(d, ignore_errors=True)
                continue
            self._tasks[task.task_id] = task
            if task.status not in _RESUME_STATUSES:
                continue
            if not config.get_account(task.account_id):
                logger.warning(
                    "任务 %s 的账号 %s 已不存在，跳过恢复", task.task_id, task.account_id
                )
                continue
            restored += 1
            client = MfunsClient(task.account_id)
            handle = asyncio.create_task(run_publish_task(client, task))
            self.track(task.task_id, handle)
        if restored:
            logger.info("已恢复 %d 个未完成上传任务", restored)
        return restored

    def _prune(self) -> None:
        while len(self._tasks) > MAX_TASKS:
            done = [t for t in self._tasks.values() if t.status in ("done", "failed")]
            if not done:
                return
            oldest = min(done, key=lambda t: t.updated_at)
            self._tasks.pop(oldest.task_id, None)
            shutil.rmtree(oldest.dir, ignore_errors=True)


# 全局任务管理器（单例）
TASK_MANAGER = TaskManager()


async def _upload_part(
    client: MfunsClient,
    task: TaskInfo,
    idx: int,
    path: Path,
    checkpoint_dir: str,
) -> None:
    """上传单个分P：凭证 -> OSS 断点续传 -> 完成通知。失败抛异常并标记该分P。

    断点恢复：分P 已有 video_id 时用 update_upload_auth 刷新同一 VOD 记录凭证
    （objectKey 不变，OSS 检查点直接续传）；刷新失败则重新申请新凭证。
    """
    part = task.parts[idx]
    if part.status == "uploaded" and part.mfuns_id:
        return
    part.status = "uploading"
    try:
        if part.video_id:
            try:
                auth = _normalize_auth(await client.video_update_upload_auth(part.video_id))
            except MfunsError:
                auth = None
            if not auth:
                part.video_id = None
        if not part.video_id:
            auth = _normalize_auth(
                await client.video_upload_auth(path.name, path.stat().st_size)
            )
            part.video_id = auth["VideoId"]
            task.persist()

        credentials = parse_upload_credentials(auth)

        def _cb(done: int, total: int) -> None:
            part.percent = int(done * 100 / total) if total else 0

        await asyncio.to_thread(
            upload_video_file, credentials, str(path),
            checkpoint_dir=checkpoint_dir, progress=_cb,
        )
        completed = await client.video_upload_complete(part.video_id)
        mfuns_id = completed.get("id")
        if not mfuns_id:
            raise VodUploadError(-1, f"upload_complete 未返回视频库 ID: {completed}")
        part.mfuns_id = int(mfuns_id)
        part.meta = _build_meta(completed, path.name)
        part.status = "uploaded"
        part.percent = 100
        task.persist()
    except Exception as e:
        part.status = "failed"
        part.error = str(e)
        task.persist()
        raise


async def _cover_url(client: MfunsClient, cover: str) -> str:
    """封面解析：https 外链直接用；本地图片自动 upload_image 转 /static/xxx；缺省用平台默认封面。"""
    if cover.startswith(("https://", "http://")):
        return cover
    if cover:
        media = await client.upload_image(cover)
        file_path = ((media or {}).get("file") or {}).get("file_path") or (media or {}).get("file_path")
        if not file_path:
            raise MfunsError(-1, f"封面上传后未返回 file_path: {media}")
        return str(file_path)
    return DEFAULT_COVER


async def _log_completion(task: TaskInfo) -> None:
    try:
        from .activity import write_activity
        if task.status == "done":
            await write_activity(
                "publish_video_upload", "publish_done",
                target={"type": "video", "id": task.contribute_id},
                params={"task_id": task.task_id, "title": task.title},
                result={"status": "success"},
                account_id=task.account_id,
            )
        else:
            await write_activity(
                "publish_video_upload", "publish_failed",
                params={"task_id": task.task_id, "title": task.title},
                result={"status": "error", "message": (task.error or "")[:200]},
                account_id=task.account_id,
            )
    except Exception:
        pass  # 日志失败不影响结果


async def run_publish_task(client: MfunsClient, task: TaskInfo) -> None:
    """执行完整发布管线（后台任务主体）：并行上传分P -> 全部完成后投稿。

    所有异常都收敛为 task.status = failed + task.error，不向上抛出。
    """
    files = [Path(f) for f in task.files]
    checkpoint_dir = str(task.dir)
    try:
        if not files:
            raise VodUploadError(-1, "任务未配置视频文件列表")
        for p in files:
            if not p.is_file():
                raise VodUploadError(-1, f"文件不存在: {p}")

        task.status = "uploading"
        task.persist()
        sem = asyncio.Semaphore(min(UPLOAD_CONCURRENCY, len(files)))

        async def _worker(idx: int) -> None:
            async with sem:
                await _upload_part(client, task, idx, files[idx], checkpoint_dir)

        results = await asyncio.gather(
            *(_worker(i) for i in range(len(files))), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise errors[0]

        task.status = "publishing"
        task.persist()
        cover_url = await _cover_url(client, task.cover)
        videos: list[dict] = []
        for i, part in enumerate(task.parts):
            item: dict = {
                "type": "direct",
                "content": str(part.mfuns_id),
                # P1 用投稿标题，后续分P 用 P2/P3…
                "title": task.title if i == 0 else f"P{i + 1}",
            }
            if part.meta:
                item["meta"] = part.meta
            videos.append(item)

        payload: dict = {
            "cid": task.cid,
            "title": task.title,
            "content": text_to_quill(task.content),
            "cover": cover_url,
            "video": json.dumps(videos, ensure_ascii=False),
            "copyright": task.copyright_,
        }
        if task.tags:
            payload["tags"] = ",".join(task.tags[:10])

        data = await client.post("/v1/contribute/video/create", json_body=payload)
        con = (data or {}).get("contribute") or {}
        task.contribute_id = con.get("id")
        task.contribute_status = con.get("status")
        task.status = "done"
        task.persist()
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        task.persist()
        logger.warning("上传任务 %s 失败: %s", task.task_id, e)
    finally:
        task.updated_at = time.time()
        task.persist()
        await _log_completion(task)
